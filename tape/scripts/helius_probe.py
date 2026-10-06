"""helius_probe -- Phase 1 of verifying Helius's `getTransfersByAddress` as a
possible backfill source (2026-09-21, see docs/DECISIONS.md D25).

WHY THIS EXISTS: BigQuery's free query quota (1 TiB/month, shared between
sandbox and paid mode) is exhausted for this billing cycle -- likely by the
D22/D23 exports run before this project knew to be careful about it. The
Bitquery `archive` add-on ($100/mo) was explicitly declined for now.
Self-hosting Old Faithful (the free, self-hosted historical-ledger project)
was evaluated and confirmed to need real infra -- its `getSignaturesForAddress`
index "can currently not be used remotely" per its own docs, so it is a
multi-day build, not a quick swap-in.

Helius is different: it is ALREADY in this project's budget ($49/mo,
docs/DATA.md), and its `getTransfersByAddress` endpoint is included in that
plan at ~10 credits/call (10M credits/month included) -- cheap enough that a
real 3-month, whole-universe pull should fit inside credits already being
paid for, if the schema holds up.

TWO THINGS ARE GENUINELY UNVERIFIED AND THIS SCRIPT EXISTS TO CHECK THEM
BEFORE ONE MORE LINE OF PRODUCTION CODE IS WRITTEN (D3's discipline, same as
`probe.py` and `parse_bigquery_export.py` before it):

  1. `getTransfersByAddress`'s `address` parameter must be a WALLET OWNER
     address (Helius's own docs: "not an associated token account (ATA)" --
     and not a mint or pool address either). There is no direct "give me
     every trade for mint X" call. The workaround: query the POOL's vault
     owner address instead of a trader's wallet -- every trade is, from the
     pool's side, a transfer between the pool and a trader, so the pool's
     own transfer history *is* the mint's trade history. This script derives
     that pool-owner address itself (`resolve_pool_owner`, via the plain,
     decoder-free `getTokenLargestAccounts` + `getAccountInfo` RPC calls --
     no vendor-specific parsing, no instruction decoding), rather than
     assuming you already know it.
  2. The endpoint's documented example response only shows an SPL-token
     transfer ("TokenTransfer" objects). Whether a NATIVE SOL leg of a swap
     (the counter-leg of every pump.fun/PumpSwap trade) is even returned by
     this endpoint at all -- and if so, under what `mint` value, native vs
     wrapped -- is not confirmed. A `solMode` request param ("merged" vs
     "separate") is mentioned in Helius's own materials; this script passes
     `solMode: "merged"` as a first guess, but that is a GUESS, not a
     confirmed behaviour.

Because of (2), this script deliberately does NOT attempt to pair transfers
into buy/sell swaps or bucket them into windows yet -- writing that logic
before seeing one real response would repeat the exact mistake `probe.py`'s
docstring warns against (a schema you have not verified is the same risk as
a decoder you have not verified). Instead it fetches real data and prints:
  - the resolved pool-owner address (so you can sanity-check it against a
    block explorer if you want),
    - the raw first transfer record, verbatim,
  - a breakdown of `type` values seen and every distinct `mint` value seen
    (this alone answers question 2 above: if a SOL-like mint value never
    appears, native SOL legs are not coming through this endpoint and a
    different call is needed for the SOL side of each trade).

Run it, paste the output back, and THEN the pairing/parsing logic (phase 2)
gets written against what actually came back -- not against what the docs
implied.

Usage:
    python -m tape.scripts.helius_probe \\
        --mint 3tUn8dGmDceJ9pCwFLUVo2jkD6FLWaZ78YEpAa3bpump \\
        --tape ../v3/data_deep/2026-09-20/3tUn8dGmDceJ9pCwFLUVo2jkD6FLWaZ78YEpAa3bpump.jsonl

Needs `HELIUS_API_KEY` in `tape/.env` or the shell environment (same pattern
as `BITQUERY_API_KEY` -- see `tape/env.py`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from typing import List, Optional, Sequence

from ..env import load_project_dotenv
from .probe import load_tape

HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/"

# Native SOL's mint address -- if this (or its wrapped form, same address)
# never shows up in the `mint` field of what comes back, that is this
# script's answer to open question (2) in the module docstring.
SOL_MINT = "So11111111111111111111111111111111111111112"

# Pagination safety valve. NOT a guess at "how much is too much" -- this
# project's own D22/D23 BigQuery run already measured 18,160 real
# transactions for the exact mint/window this probe is run against, so a
# few thousand transfer records back from a busy pool is expected, not
# suspicious. The real stuck-loop detector is `paginationToken` repeating
# across two consecutive pages (see fetch_helius_transfers); MAX_PAGES is
# only the outer safety net in case that never happens for some other
# reason not yet seen.
MAX_PAGES = 1000

# 429 retry budget. Confirmed live (2026-09-21): a free-tier key hit
# "429 Too Many Requests" after ~70 back-to-back getTransfersByAddress
# calls with no delay between them -- not a pagination bug, just no
# backoff. The fix is respecting the rate limit, never spinning up
# multiple accounts to route around one account's limit (very likely a
# ToS violation for most API vendors, Helius included, and it risks the
# account(s) you actually need getting flagged for abuse). The paid
# Developer plan this project already budgets for (docs/DATA.md, 50 RPS)
# should not need anywhere near this many retries for real use -- this
# budget exists for whichever key you're actually running with.
MAX_RETRIES = 8
RETRY_BACKOFF_CAP_S = 30.0
# Small pause between successful pages, so a long pull doesn't lean on
# retry/backoff to stay under the limit in the first place.
INTER_PAGE_DELAY_S = 0.15


def _rpc_call(method: str, params: list, api_key: str) -> dict:
    """One JSON-RPC 2.0 call against Helius's RPC endpoint. Raises loud on
    a transport error or a JSON-RPC `error` field -- never returns a
    partial/guessed result.

    Retries on 429 with exponential backoff (honouring a `Retry-After`
    header if Helius sends one, since that is an authoritative wait time
    and a guess would either wait too long or get rate-limited again).
    Any other HTTP error status is NOT retried -- a 4xx/5xx that isn't a
    rate limit is a real problem, not something backing off fixes.
    """
    import httpx

    url = f"{HELIUS_RPC_URL}?api-key={api_key}"
    payload = {"jsonrpc": "2.0", "id": "helius_probe", "method": method, "params": params}
    for attempt in range(1, MAX_RETRIES + 1):
        resp = httpx.post(url, json=payload, timeout=30.0)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait_s = float(retry_after) if retry_after else min(2 ** attempt, RETRY_BACKOFF_CAP_S)
            print(f"  (429 rate limited on {method}, waiting {wait_s:.1f}s -- "
                  f"retry {attempt}/{MAX_RETRIES})", file=sys.stderr)
            time.sleep(wait_s)
            continue
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise RuntimeError(f"Helius RPC error calling {method}: {body['error']}")
        return body
    raise RuntimeError(
        f"Still getting 429 from {method} after {MAX_RETRIES} retries with backoff -- "
        f"this is a real, sustained rate limit (likely a free-tier key), not a "
        f"transient blip. Use a paid-plan key, or slow down further."
    )


def resolve_pool_owner(mint: str, api_key: str) -> Optional[str]:
    """Finds the address that OWNS the largest token account holding `mint`
    -- for an actively-traded pump.fun/PumpSwap pool, that is overwhelmingly
    likely to be the pool's own vault, and the vault's owner is the pool's
    program-derived authority. Both RPC calls used here (`getTokenLargestAccounts`,
    `getAccountInfo`) are plain, documented Solana RPC methods -- not
    Helius-specific, not a program decoder, consistent with D3's "buy the
    parsing, write zero decoders" (this is address-graph lookup, not
    instruction decoding).

    Returns None -- never a guess -- if the mint has no token accounts, or
    if the largest account's `owner` field is not where expected. A None
    here means "inspect this by hand," not "assume it's fine and keep going."
    """
    largest = _rpc_call("getTokenLargestAccounts", [mint], api_key)
    accounts = (largest.get("result") or {}).get("value") or []
    if not accounts:
        print(f"getTokenLargestAccounts returned no accounts for {mint} -- "
              f"full response:\n{json.dumps(largest, indent=2)}", file=sys.stderr)
        return None
    top_account = accounts[0]["address"]
    print(f"Largest token account for {mint}: {top_account} "
          f"(amount={accounts[0].get('amount')}) -- treating this as the pool vault.")

    info = _rpc_call("getAccountInfo", [top_account, {"encoding": "jsonParsed"}], api_key)
    try:
        owner = info["result"]["value"]["data"]["parsed"]["info"]["owner"]
    except (KeyError, TypeError):
        print(f"getAccountInfo for {top_account} did not have the expected "
              f"jsonParsed SPL-token-account shape -- full response:\n"
              f"{json.dumps(info, indent=2)}", file=sys.stderr)
        return None
    return owner


def fetch_helius_transfers(owner: str, mint: str, since_ms: int, until_ms: int,
                            api_key: str) -> List[dict]:
    """Paginates `getTransfersByAddress` for `owner`, filtered to `mint` and
    the given time window (Helius's `blockTime` filter is Unix SECONDS, not
    ms -- converted here). `solMode: "merged"` is passed per the module
    docstring's open question (2) -- unconfirmed, check the raw output.

    The real stuck-loop detector is `paginationToken` REPEATING across two
    consecutive pages -- that is unambiguous evidence pagination isn't
    advancing, unlike a fixed page count, which can't tell "a genuinely busy
    pool" apart from "actually stuck" (this bit a first version of this
    function: MAX_PAGES=50 tripped on a pool this project already knows, via
    D22/D23's BigQuery ground truth, had 18,160 real transactions in this
    exact window -- that was real data, not a bug). Progress is printed every
    10 pages so a long-running pull is never silent.
    """
    since_s, until_s = since_ms // 1000, until_ms // 1000
    all_transfers: List[dict] = []
    pagination_token = None
    for page in range(1, MAX_PAGES + 1):
        params_obj = {
            "mint": mint,
            "limit": 100,
            "sortOrder": "asc",
            "solMode": "merged",
            "filters": {"blockTime": {"gte": since_s, "lt": until_s}},
        }
        if pagination_token:
            params_obj["paginationToken"] = pagination_token
        body = _rpc_call("getTransfersByAddress", [owner, params_obj], api_key)
        result = body.get("result") or {}
        page_data = result.get("data") or []
        all_transfers.extend(page_data)
        new_token = result.get("paginationToken")

        if new_token is not None and new_token == pagination_token:
            raise RuntimeError(
                f"paginationToken did not advance across two consecutive pages "
                f"(stuck at {new_token!r}) after {page} pages / "
                f"{len(all_transfers)} transfers -- this IS a pagination bug, "
                f"not just a busy pool (a busy pool still advances the token)."
            )
        if page % 10 == 0 or new_token is None:
            print(f"  ... page {page}: {len(all_transfers)} transfers so far "
                  f"(paginationToken={new_token!r})")
        if new_token is None:
            break
        pagination_token = new_token
        time.sleep(INTER_PAGE_DELAY_S)
    else:
        raise RuntimeError(
            f"Hit MAX_PAGES={MAX_PAGES} ({len(all_transfers)} transfers) without "
            f"paginationToken going null, and it kept advancing the whole way -- "
            f"either this window/mint genuinely has more than {MAX_PAGES * 100} "
            f"transfer records (raise MAX_PAGES), or something else is wrong. "
            f"Last paginationToken: {pagination_token!r}."
        )
    return all_transfers


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mint", required=True)
    ap.add_argument("--tape", required=True, help="path to a v3 data_deep/<date>/<mint>.jsonl file, "
                                                    "used only to derive the since/until window")
    args = ap.parse_args(argv)

    api_key = os.environ.get("HELIUS_API_KEY")
    if not api_key:
        print("HELIUS_API_KEY not set (checked the shell environment and tape/.env). "
              "Nothing to probe against.", file=sys.stderr)
        return 2

    rows = load_tape(args.tape)
    if not rows:
        print(f"No rows parsed from {args.tape} -- wrong path, or the file is empty.",
              file=sys.stderr)
        return 2
    since_ms, until_ms = rows[0]["timestamp"], rows[-1]["timestamp"]
    print(f"Window from tape: {since_ms} .. {until_ms} ms "
          f"({(until_ms - since_ms) / 1000:.0f}s)")

    print(f"\nStep 1: resolving the pool-vault owner for {args.mint} ...")
    owner = resolve_pool_owner(args.mint, api_key)
    if owner is None:
        print("Could not resolve a pool owner -- see stderr above. Stopping "
              "here; nothing downstream of this can mean anything without it.",
              file=sys.stderr)
        return 2
    print(f"Resolved pool owner: {owner}")

    print(f"\nStep 2: fetching getTransfersByAddress for owner={owner}, "
          f"mint={args.mint} ...")
    transfers = fetch_helius_transfers(owner, args.mint, since_ms, until_ms, api_key)
    print(f"Got {len(transfers)} transfer records.")

    if not transfers:
        print("Nothing came back -- either the window/mint/owner is wrong, or "
              "this pool had no activity Helius's index covers for this window. "
              "Do not conclude the endpoint doesn't work from this alone -- "
              "try a narrower, more recent window first to isolate which.",
              file=sys.stderr)
        return 1

    print("\nFirst transfer record, verbatim, for manual inspection:")
    print(json.dumps(transfers[0], indent=2))

    type_counts = Counter(t.get("type") for t in transfers)
    mint_counts = Counter(t.get("mint") for t in transfers)
    print(f"\n`type` breakdown: {dict(type_counts)}")
    print(f"`mint` values seen: {dict(mint_counts)}")
    if SOL_MINT not in mint_counts:
        print(f"\nNOTE: {SOL_MINT} never appears in `mint` above -- but this call "
              f"explicitly filtered on `mint={args.mint}`, which by construction "
              f"excludes any record whose mint is something else, SOL included. "
              f"This is NOT yet evidence the endpoint can't return the SOL leg -- "
              f"it may just mean this specific query filtered it out. Step 3 below "
              f"checks the SOL leg a different, vendor-independent way instead of "
              f"guessing at getTransfersByAddress's parameters further.")

    print(f"\nStep 3: fetching plain getTransaction for the first "
          f"{min(3, len(transfers))} signatures already found, to check whether "
          f"the SOL leg is available the same way it was from BigQuery (D22) -- "
          f"via meta.preBalances/postBalances -- rather than depending on "
          f"getTransfersByAddress giving it to us directly.")
    for i, t in enumerate(transfers[:3]):
        sig = t["signature"]
        tx = _rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                        api_key)
        print(f"\n--- getTransaction({sig[:16]}...) ---")
        meta = (tx.get("result") or {}).get("meta") or {}
        print(f"  meta.preBalances/postBalances present: "
              f"{'preBalances' in meta and 'postBalances' in meta}")
        print(f"  meta.preTokenBalances/postTokenBalances present: "
              f"{'preTokenBalances' in meta and 'postTokenBalances' in meta}")
        if i == 0:
            # Presence alone doesn't tell us WHERE the SOL leg of this trade
            # actually lives -- PumpSwap-style AMMs often hold their quote
            # side as wrapped SOL (an SPL token entry in preTokenBalances/
            # postTokenBalances with mint=SOL_MINT), not as a native lamport
            # move in preBalances/postBalances. Reasoning about "how AMMs
            # usually work" here would repeat the exact mistake this project
            # has already been burned by more than once (D19-D21) -- print
            # the real thing and look, instead of assuming.
            print(f"  meta.preTokenBalances (mint/owner/amount only, decimals dropped for brevity):")
            for tb in meta.get("preTokenBalances") or []:
                ui = tb.get("uiTokenAmount") or {}
                print(f"    accountIndex={tb.get('accountIndex')} mint={tb.get('mint')} "
                      f"owner={tb.get('owner')} amount={ui.get('amount')}")
            print(f"  meta.postTokenBalances:")
            for tb in meta.get("postTokenBalances") or []:
                ui = tb.get("uiTokenAmount") or {}
                print(f"    accountIndex={tb.get('accountIndex')} mint={tb.get('mint')} "
                      f"owner={tb.get('owner')} amount={ui.get('amount')}")
            print(f"  meta.preBalances: {meta.get('preBalances')}")
            print(f"  meta.postBalances: {meta.get('postBalances')}")
            account_keys = ((tx.get("result") or {}).get("transaction") or {}).get("message", {}).get("accountKeys") or []
            print(f"  accountKeys (index -> pubkey, for mapping preBalances/postBalances by position):")
            for idx, ak in enumerate(account_keys):
                pubkey = ak.get("pubkey") if isinstance(ak, dict) else ak
                print(f"    [{idx}] {pubkey}")
            loaded = meta.get("loadedAddresses")
            if loaded:
                print(f"  meta.loadedAddresses (versioned tx -- these extend accountKeys "
                      f"beyond the static list above, writable then readonly): {loaded}")
        if "preBalances" not in meta:
            print(f"  Full response for manual inspection:\n{json.dumps(tx, indent=2)}")

    print("\nDo not write swap-pairing logic from this output alone without "
          "sharing it back -- phase 2 (parsing) gets written against what "
          "actually came back, same discipline as probe.py and "
          "parse_bigquery_export.py before it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
