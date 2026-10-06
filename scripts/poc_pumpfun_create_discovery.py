#!/usr/bin/env python3
"""PROOF OF CONCEPT, not production code -- same status as poc_gtfa_reach.py,
poc_idl_classify.py, poc_real_creation_times.py.

Question this answers (the D56 follow-up the user explicitly chose over a
bigger Bitquery-realtime backfill or a "stop here" call): can genuinely NEW
pump.fun launches be discovered directly from the on-chain `create`
instruction, with their REAL creation time coming straight from the
transaction's own `blockTime` -- instead of (a) Bitquery `discover()`,
which surfaces ANY mint with recent trade activity, old survivors included
(D55's confound), or (b) pump.fun's unofficial `/coins/{mint}` frontend API
(D55's `fetch_real_creation_times.py`), which has a ~35% error/miss rate
and is a third-party dependency with no SLA at all.

If this works, it solves D54 and D55 AT ONCE, and strictly better than
either prior attempt: no reliance on an unofficial API for timestamps (the
blockTime IS the real creation time, straight from the ledger, the same
kind of ground truth D22/D23/D25 already trust elsewhere in this project),
and no re-filtering of an already-narrow window -- gTFA's confirmed
>=168h reach (D54) means a discovery window can be as wide as a week,
calendar-diverse by construction, not squeezed into one Bitquery
`realtime`-retention burst.

WHY THIS IS A POC, NOT A COMMITMENT TO BUILD THE FULL PIPELINE YET:
PUMPFUN_PROGRAM is invoked by every buy/sell on the platform, not just
`create` -- almost certainly several orders of magnitude more traffic than
genuine new-token creations. Naively decoding EVERY signature touching it
over a multi-day window via `getTransaction` could be a very large number
of paid RPC calls. This script does NOT commit to that; it (1) samples a
SHORT, cheap window to get a REAL per-hour signature-volume number (not a
guess) so the cost of a wider pull can be judged from evidence, and (2)
decodes that same short sample to prove the `create`-decoding logic itself
is correct BEFORE anyone pays for a week-long pull.

Reuses, rather than re-derives:
  - `PUMPFUN_PROGRAM` (tape/sources/bitquery.py, already verified).
  - `_rpc_call` (tape/sources/helius.py, already has 429 retry/backoff).
  - The exact `getTransactionsForAddress` "signatures" request shape D54
    confirmed live (filters.blockTime nested, not a sibling of limit/sortOrder).
  - The exact Anchor-discriminator classification method D47/D48/D49 already
    confirmed live: fetch pump.fun's OWN official IDL fresh (never hardcoded),
    discriminator = sha256(f"global:{name}")[:8], check both top-level AND
    innerInstructions (D48 found real pump.fun calls are very often CPI'd,
    not top-level) for a `programId` in {PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM}.

NOT YET VERIFIED, same discipline as every other adapter in this project --
this is exactly what this run is for:
  1. Which account, by IDL-declared position, is the new mint on `create`.
     Printed explicitly from the LIVE IDL's own `accounts` list (name +
     index) below -- never assumed from memory.
  2. Whether jsonParsed `getTransaction` renders an unrecognized custom
     Anchor instruction's `accounts` as a flat array of pubkey STRINGS in
     IDL-declared order (true for every other program this project has
     decoded, per Solana's documented jsonParsed behavior for
     non-natively-parsed programs) -- if not, this will show up as a
     mismatch between the printed account list and what comes back, not a
     silent wrong answer, because both are printed side by side per hit.
  3. Real signature-per-hour volume for PUMPFUN_PROGRAM -- unknown, printed
     from THIS run's own sample, not guessed.
  4. Whether pump.fun's `/coins/{mint}` `created_timestamp` genuinely
     matches (to within a few seconds) the on-chain blockTime this script
     derives -- cross-checked live against a small sample of hits, same
     source D55 already partially trusts, used here only as an
     independent sanity check, not as the primary source of truth anymore.

Writes nothing to the Store or to any cache file. Read-only.

    python scripts/poc_pumpfun_create_discovery.py --minutes-back 15
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.env import load_project_dotenv
from tape.sources.bitquery import PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM
from tape.sources.helius import MAX_SUPPORTED_TX_VERSION, _rpc_call

PUMP_IDL_URL = "https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump.json"
PUMPFUN_API_BASE = "https://frontend-api-v3.pump.fun"

MAX_WORKERS = 10  # conservative -- same reasoning as tape/sources/helius.py's
                   # DEFAULT_MAX_WORKERS, this is a PoC not a tuned pipeline


def _b58_decode(s: str) -> bytes:
    """Same tiny, dependency-free implementation as poc_idl_classify.py."""
    alphabet = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for ch in s.encode():
        n = n * 58 + alphabet.index(ch)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    n_pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_pad + full


def anchor_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def fetch_pump_idl() -> dict:
    resp = httpx.get(PUMP_IDL_URL, timeout=30.0)
    resp.raise_for_status()
    idl = resp.json()
    if not idl.get("instructions"):
        raise RuntimeError(
            f"{PUMP_IDL_URL} has no 'instructions' array -- IDL shape may "
            f"have changed; do not trust a guessed fallback."
        )
    return idl


def find_create_instruction(idl: dict) -> dict:
    for ix in idl["instructions"]:
        if ix.get("name") == "create":
            return ix
    raise RuntimeError(
        "No instruction literally named 'create' in the live IDL -- "
        f"names present: {sorted(i.get('name') for i in idl['instructions'])}. "
        f"Do not guess a substitute; re-check the IDL by hand."
    )


def fetch_signatures_window(address: str, since_ts: int, until_ts: int, api_key: str) -> list:
    """Every signature touching `address` with blockTime in [since_ts, until_ts)
    (unix seconds), via gTFA's "signatures" mode -- the exact request shape
    D54 (poc_gtfa_reach.py) already confirmed live. Paginated with the same
    stuck-token guard as tape/sources/helius.py::_fetch_signatures."""
    out = []
    pagination_token = None
    page_num = 0
    while True:
        page_num += 1
        params_obj = {
            "sortOrder": "asc",
            "limit": 1000,
            "transactionDetails": "signatures",
            "filters": {"blockTime": {"gte": since_ts, "lt": until_ts}},
        }
        if pagination_token:
            params_obj["paginationToken"] = pagination_token
        body = _rpc_call("getTransactionsForAddress", [address, params_obj], api_key)
        result = body.get("result")
        if isinstance(result, list):
            txs, new_token = result, None
        else:
            result = result or {}
            txs = result.get("transactions") or result.get("data") or []
            new_token = result.get("paginationToken")
        for tx in txs:
            sig = tx.get("signature") or tx.get("transactionSignature")
            bt = tx.get("blockTime")
            if sig:
                out.append((sig, bt))
        print(f"  [gTFA] page {page_num}: {len(txs)} entries this page, "
              f"{len(out)} total so far", file=sys.stderr)
        if new_token is not None and new_token == pagination_token:
            raise RuntimeError(
                f"paginationToken stuck at {new_token!r} after {page_num} pages -- "
                f"a real pagination bug, not just a busy window."
            )
        if not new_token:
            break
        pagination_token = new_token
        time.sleep(0.1)
    return out


def decode_tx_for_create(sig: str, api_key: str, disc_by_program: dict,
                          create_disc: bytes, mint_account_index: int) -> dict | None:
    """One getTransaction call -> a create-event dict if this signature
    invoked pump.fun's `create` (top-level or CPI'd, D48), else None. Never
    guesses; returns None on anything unparsable rather than fabricate."""
    tx = _rpc_call(
        "getTransaction",
        [sig, {"encoding": "jsonParsed",
               "maxSupportedTransactionVersion": MAX_SUPPORTED_TX_VERSION}],
        api_key,
    )
    result = tx.get("result") or {}
    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return None
    message = (result.get("transaction") or {}).get("message") or {}
    top_level = message.get("instructions") or []
    inner = [ix for grp in (meta.get("innerInstructions") or [])
             for ix in (grp.get("instructions") or [])]
    block_time = result.get("blockTime")

    for ix in list(top_level) + inner:
        if not isinstance(ix, dict):
            continue
        prog = ix.get("programId")
        if prog not in disc_by_program:
            continue
        data = ix.get("data")
        if not data:
            continue
        try:
            raw = _b58_decode(data)
        except Exception:
            continue
        if raw[:8] != create_disc:
            continue
        accounts = ix.get("accounts") or []
        if mint_account_index >= len(accounts):
            return {"sig": sig, "block_time": block_time, "mint": None,
                     "accounts": accounts, "note": "accounts array shorter than "
                     "expected IDL index -- print raw accounts, don't guess"}
        return {"sig": sig, "block_time": block_time,
                "mint": accounts[mint_account_index], "accounts": accounts}
    return None


def cross_check_pumpfun_api(mint: str) -> dict:
    try:
        resp = httpx.get(f"{PUMPFUN_API_BASE}/coins/{mint}", timeout=15.0,
                         headers={"accept": "application/json"})
        if resp.status_code != 200:
            return {"status": "error", "created_timestamp": None, "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        return {"status": "ok", "created_timestamp": data.get("created_timestamp")}
    except Exception as e:
        return {"status": "error", "created_timestamp": None, "error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes-back", type=float, default=15.0,
                     help="how wide a window, ending now, to sample -- kept "
                          "small by default since real volume is unknown")
    ap.add_argument("--decode-limit", type=int, default=500,
                     help="cap on how many signatures to actually decode via "
                          "getTransaction, even if the window has more")
    ap.add_argument("--cross-check-sample", type=int, default=5,
                     help="how many discovered `create` events to sanity-check "
                          "against pump.fun's own frontend API")
    ap.add_argument("--program", choices=["pumpfun", "pumpswap"], default="pumpfun")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("HELIUS_API_KEY")
    if not api_key:
        print("HELIUS_API_KEY not set.", file=sys.stderr)
        return 2

    address = PUMPFUN_PROGRAM if args.program == "pumpfun" else PUMPSWAP_PROGRAM

    print("=" * 78)
    print("STEP 1: live IDL -- find `create`'s account list from REAL evidence")
    print("=" * 78)
    idl = fetch_pump_idl()
    create_ix = find_create_instruction(idl)
    create_disc = anchor_discriminator("create")
    accounts_decl = create_ix.get("accounts") or []
    print(f"`create` discriminator: {create_disc.hex()}")
    print(f"`create`'s IDL-declared accounts, in order:")
    for i, acc in enumerate(accounts_decl):
        print(f"  [{i}] {acc.get('name')}")
    args_decl = create_ix.get("args") or []
    print(f"`create`'s IDL-declared args (NOT decoded by this PoC, listed for reference):")
    for a in args_decl:
        print(f"  {a.get('name')}: {a.get('type')}")

    mint_idx = None
    for i, acc in enumerate(accounts_decl):
        name = (acc.get("name") or "").lower()
        if name == "mint":
            mint_idx = i
            break
    if mint_idx is None:
        # Fall back to the first account whose name merely CONTAINS "mint",
        # printed loudly as a fallback so it's never a silent guess.
        for i, acc in enumerate(accounts_decl):
            if "mint" in (acc.get("name") or "").lower():
                mint_idx = i
                print(f"\n  NOTE: no account named exactly 'mint' -- falling back to "
                      f"index {i} ({acc.get('name')!r}), which merely contains 'mint'. "
                      f"Verify this against the printed list above by eye.")
                break
    if mint_idx is None:
        print("\n  Could not find any account with 'mint' in its name at all -- "
              "stopping here rather than guess an index. See the printed "
              "account list above and pick the right index by hand.")
        return 1
    print(f"\n  Using account index {mint_idx} ({accounts_decl[mint_idx].get('name')}) as the mint.")

    # Also build the full discriminator map (all instructions) so any
    # OTHER instruction accidentally matching create's discriminator would
    # be visible (it can't -- discriminators are collision-free by
    # construction -- but this mirrors poc_idl_classify.py's structure and
    # costs nothing).
    disc_by_program = {
        PUMPFUN_PROGRAM: {anchor_discriminator(i["name"]): i["name"] for i in idl["instructions"]},
        PUMPSWAP_PROGRAM: {anchor_discriminator(i["name"]): i["name"] for i in idl["instructions"]},
    }

    print("\n" + "=" * 78)
    print(f"STEP 2: signature volume for {args.program} ({address}) over the "
          f"last {args.minutes_back} minute(s)")
    print("=" * 78)
    now_ts = int(time.time())
    since_ts = now_ts - int(args.minutes_back * 60)
    sigs = fetch_signatures_window(address, since_ts, now_ts, api_key)
    n_sigs = len(sigs)
    per_hour = n_sigs / (args.minutes_back / 60.0) if args.minutes_back > 0 else float("nan")
    print(f"\n  {n_sigs} signature(s) in the sampled window")
    print(f"  extrapolated rate: ~{per_hour:,.0f} signatures/hour "
          f"(~{per_hour * 24:,.0f}/day, ~{per_hour * 24 * 7:,.0f}/week)")
    print(f"  ^ THIS is the real number to judge whether decoding a wider window")
    print(f"    (getTransaction per signature) is affordable -- not a guess.")

    to_decode = [s for s, _ in sigs][: args.decode_limit]
    print(f"\n  decoding {len(to_decode)}/{n_sigs} signature(s) this run "
          f"(--decode-limit={args.decode_limit})")

    print("\n" + "=" * 78)
    print("STEP 3: decode sampled signatures, looking for `create` events")
    print("=" * 78)
    hits = []
    errors = 0
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(decode_tx_for_create, sig, api_key, disc_by_program,
                                    create_disc, mint_idx): sig for sig in to_decode}
        for fut in concurrent.futures.as_completed(futures):
            completed += 1
            if completed % 50 == 0 or completed == len(to_decode):
                print(f"  ... {completed}/{len(to_decode)} decoded, {len(hits)} create hit(s) so far",
                      file=sys.stderr)
            try:
                hit = fut.result()
            except Exception as e:
                errors += 1
                continue
            if hit is not None:
                hits.append(hit)

    print(f"\n  {len(hits)} `create` event(s) found out of {len(to_decode)} decoded "
          f"({errors} decode error(s))")
    for h in hits[:20]:
        ts_ms = int(h["block_time"]) * 1000 if h.get("block_time") is not None else None
        print(f"    sig={h['sig'][:16]}...  mint={h.get('mint')}  "
              f"block_time_ms={ts_ms}{'  ' + h.get('note', '') if h.get('note') else ''}")
    if len(hits) > 20:
        print(f"    ... and {len(hits) - 20} more")

    if not hits:
        print("\n  No `create` events in this sample -- either the window is too short/")
        print("  quiet, or something upstream is wrong. Try a longer --minutes-back")
        print("  before concluding anything.")
        return 0

    print("\n" + "=" * 78)
    print(f"STEP 4: cross-check up to {args.cross_check_sample} hit(s) against "
          f"pump.fun's OWN frontend API (independent sanity check)")
    print("=" * 78)
    for h in hits[: args.cross_check_sample]:
        mint = h.get("mint")
        if not mint:
            continue
        our_ts_ms = int(h["block_time"]) * 1000 if h.get("block_time") is not None else None
        api_rec = cross_check_pumpfun_api(mint)
        api_ts_ms = api_rec.get("created_timestamp")
        if api_rec["status"] == "ok" and isinstance(api_ts_ms, (int, float)) and our_ts_ms is not None:
            delta_s = (our_ts_ms - int(api_ts_ms)) / 1000.0
            print(f"  {mint}  our_blockTime_ms={our_ts_ms}  "
                  f"pumpfun_api_created_timestamp={int(api_ts_ms)}  delta={delta_s:+.1f}s")
        else:
            print(f"  {mint}  our_blockTime_ms={our_ts_ms}  "
                  f"pump.fun API: status={api_rec['status']} "
                  f"({api_rec.get('error', 'no created_timestamp field')})")
        time.sleep(0.15)

    print("\nInterpretation:")
    print("  - delta close to 0 (a few seconds) for every cross-checked hit -> the")
    print("    on-chain blockTime IS the real creation time, exactly as expected --")
    print("    this path can fully replace D55's unofficial-API cache for timestamps.")
    print("  - the per-hour rate above tells you what a --minutes-back 10080 (1 week)")
    print("    pull would actually cost in getTransaction calls -- multiply it out")
    print("    before running one; if it's too large, the next step is finding a")
    print("    cheaper create-only filter (Bitquery's Instructions cube, or Helius's")
    print("    Enhanced Transactions 'type' classification) rather than brute-force")
    print("    decoding every signature.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
