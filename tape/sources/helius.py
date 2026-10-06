"""Helius adapter -- backfill via ledger balance deltas. Two Helius calls,
each used for what it's actually good at, not one call doing both jobs:
`getTransfersByAddress` (mint + server-side time filtered) purely to find
which SIGNATURES fall in the window, then `getTransaction` per signature
for the actual balance-delta data.

D25-D30 (docs/DECISIONS.md) is the full trail: BigQuery (quota exhausted,
plus real documented cost-blowup risk on unscoped queries), the Bitquery
`archive` add-on ($100/mo, declined), Old Faithful (confirmed to need real
self-hosted infra -- its address-scoped lookup index "cannot currently be
used remotely"), and Bitquery's `Trading.Trades` cube (deeper retention,
but D30 caught it live returning ZERO trades for three mints proven trading
PumpSwap moments earlier on a different, already-verified Bitquery cube --
it does not cover this project's actual venues). Helius is the one path
left that is already paid for ($49/mo Developer plan, docs/DATA.md) and has
real, verified data behind every piece of it.

WHY TWO CALLS, NOT ONE: the first version of this adapter used
`getSignaturesForAddress` alone for signature discovery -- it takes ANY
account with no mint filter, so it looked like the simpler choice over
`getTransfersByAddress` (which needs a wallet-OWNER address, not a mint,
and excludes every other mint by construction -- including the SOL leg of
the very swap being reconstructed, the ambiguity `helius_probe.py`
originally ran into, D25). That reasoning was real but incomplete: it
missed that `getSignaturesForAddress` has NO server-side time-range filter
at all, only a signature cursor, so it must page backward from "now"
through every signature newer than the target window before reaching it --
confirmed live, 2026-09-21, running this against a pool that kept trading
for another day past the window under test: thousands of irrelevant,
more-recent signatures scanned first, silently, before the real work even
started. Fixed by using `getTransfersByAddress` ONLY to enumerate
signatures (it takes a `mint` filter AND a server-side `blockTime.gte/lt`
filter, so pagination is bounded by the window, not by unrelated history --
the same endpoint D25 already proved fast for this exact pool, 18,130 real
records) -- its own transfer amounts/sides are never used, only
`signature`, so the SOL-leg ambiguity that endpoint has doesn't matter
here. Every signature still goes through a real `getTransaction` call for
the actual balance-delta reconstruction. `helius_probe.py`'s own Step 3
(2026-09-21) already confirmed `getTransaction` returns
`meta.preBalances`/`postBalances` and `meta.preTokenBalances`/
`postTokenBalances` -- the exact shape D22/D23 already built and verified
balance-delta parsing against, straight from a BigQuery export of the same
ledger data. `_token_delta`/`_native_sol_delta` below port that logic
(index-based matching; closed/created-account zero-fill) from
`tape/scripts/parse_bigquery_export.py` -- see that module's docstring for
why "only look at what's present" silently dropped 71% of rows (every
full-exit sell) the first time this was tried.

DEX-agnostic by construction (ledger balance deltas, not instruction
decoding) -- satisfies D3's "buy the parsing, write zero decoders". Also,
per that same reasoning, cruder than a real decoder: a non-trade
transaction that moves the queried mint for the signer (a plain transfer,
an LP deposit/withdrawal) would be miscounted as a trade. Not handled here.

`venue` is NOT a human-readable protocol name the way Bitquery's adapter
gets one for free (`Dex.ProtocolName`) -- there is no such field on a plain
`getTransaction` response, and guessing a program-id -> name mapping from
memory would be exactly the kind of unverified guess D3 exists to prevent.
Instead `venue` is the raw, sorted, `+`-joined set of top-level program ids
the transaction actually invoked -- real, unguessed data straight from the
transaction, just not yet translated to a friendly name. Map known program
ids to names once you have a verified list, rather than hardcoding one here
now.

NOT YET VERIFIED, same discipline as every other adapter in this project
(BEFORE trusting this for a real backfill):

  1. Run this against a mint you already have a v3 tape for.
  2. Diff every field against that tape / against `parse_bigquery_export.py`'s
     BigQuery-based reconstruction for the same mint (D22/D23 already showed
     `corr(tape_buy, vendor_buy) = 0.804` for that source on ledger deltas --
     this adapter should land in the same range, since it reads the same
     kind of data from the same ledger).
  3. Confirm the side convention (buy = queried mint's balance increased for
     the signer) on real transactions, the same way D23 did.
  4. Only then run a real multi-month backfill.
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
import time
from typing import AsyncIterator, Iterator, List, Optional

from ..env import load_project_dotenv
from ..schema import BUY, SELL, CanonicalSwap
from . import SourceAdapter

RPC_URL = "https://mainnet.helius-rpc.com/"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Same 429 discipline as tape/scripts/helius_probe.py (D25, confirmed live:
# a free-tier key hit 429 after ~70 unthrottled calls). Kept as a separate
# copy here rather than imported -- sources/ is the production module
# scripts/ depends on, not the other way (same reasoning as
# sources/bitquery.py's _parse_bq_iso_ms being its own copy).
MAX_RETRIES = 8
RETRY_BACKOFF_CAP_S = 30.0
INTER_CALL_DELAY_S = 0.1

# Real, already-verified program ids (see tape/sources/bitquery.py's own
# comment on how these were sourced) -- kept as a LOCAL copy rather than
# imported, same decoupling principle as MAX_RETRIES/INTER_CALL_DELAY_S
# above (sources/ adapters don't depend on each other).
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
KNOWN_VENUE_PROGRAMS = (PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM)
_MAX_VENUE_LEN = 120


def _venue_from_program_ids(program_ids: list) -> str:
    """D65 (2026-09-22, docs/DECISIONS.md): BUG FOUND LIVE -- the original
    design (join EVERY top-level program id touched, sorted, with "+",
    unbounded in length) produced a >200-character `venue=<...>` directory
    component that Windows rejected outright (WinError 123 / "the
    filename, directory name, or volume label syntax is incorrect"),
    crashing `Store.write_swaps()` mid-backfill. Root cause: a transaction
    bundling ATA creation + compute budget + memo + token program + the
    real pump.fun/PumpSwap call all in ONE atomic tx (common for a snipe
    seconds after a token's `create_v2`) touches far more programs than a
    routine trade on an already-established mint -- this had simply never
    come up before `scripts/backfill_discovered_launches.py` (D64) became
    the first thing in this project to pull swaps from very close to a
    token's real creation moment, at scale.

    Fix: prefer the ACTUAL trading venue when one of the two real,
    already-verified program ids this whole project's universe is built
    from (PUMPFUN_PROGRAM/PUMPSWAP_PROGRAM) is present -- not just a
    length fix, `venue=6EF8rre...F6P` is also strictly more meaningful
    than a 30-program joined string for anything that groups by venue
    (`Store.mints()`'s own `GROUP BY venue`, for one). Falls back to the
    original full-joined-list behaviour, UNCHANGED, for the rare case
    neither known program is present (e.g. an unrecognized router) --
    except that fallback is now ALSO capped at `_MAX_VENUE_LEN`
    (hash-suffixed if truncated) as a defensive backstop, so venue
    construction alone can never again be the thing that crashes a
    backfill on a filesystem path limit, regardless of what unexpected
    program combination shows up in the future."""
    known = [p for p in program_ids if p in KNOWN_VENUE_PROGRAMS]
    if known:
        return "+".join(sorted(known))
    joined = "+".join(program_ids) if program_ids else "unknown"
    if len(joined) <= _MAX_VENUE_LEN:
        return joined
    import hashlib
    digest = hashlib.sha256(joined.encode()).hexdigest()[:10]
    return f"{joined[:_MAX_VENUE_LEN - 11]}_{digest}"

# `getTransaction`'s `maxSupportedTransactionVersion` -- confirmed live,
# 2026-09-21, via helius_verify.py's first real run: version 0 (this
# constant's original value) rejected a REAL transaction with "Transaction
# version (1) is not supported ... try again with maxSupportedTransactionVersion:
# 1", i.e. versioned transactions newer than v0 exist on-chain now and this
# adapter must ask for at least that version or it silently can't read them
# (a loud RuntimeError here, not a silent gap, but still a hard stop on any
# transaction at or above whatever this is set to -- bump it again if a
# future run hits the same error for a higher version number; do not guess
# ahead of what's actually been seen).
MAX_SUPPORTED_TX_VERSION = 1

# D42 (2026-09-22, docs/DECISIONS.md): the serial one-getTransaction-
# call-at-a-time loop (plus INTER_CALL_DELAY_S between each) was sized for
# Helius's FREE tier, which this project ran on until the user upgraded to
# Developer ($49/mo, D41) -- confirmed 50 requests/sec, a real, documented
# limit, not a guess. 20 concurrent workers is a deliberately conservative
# fraction of that (leaves headroom for the retry/backoff any individual
# call can trigger, plus whatever `_fetch_signatures`'s own
# getTransfersByAddress pagination is doing around the same time) rather
# than a measured ceiling -- raise via `max_workers=` once observed safe
# in practice, don't assume 50 is safe to run flat-out with no margin.
DEFAULT_MAX_WORKERS = 20


def _rpc_call(method: str, params: list, api_key: str) -> dict:
    """One JSON-RPC 2.0 call, with 429 retry/backoff. Raises loud on any
    other transport error or JSON-RPC `error` field -- never returns a
    partial/guessed result. See module docstring for why this isn't shared
    code with helius_probe.py."""
    import httpx

    url = f"{RPC_URL}?api-key={api_key}"
    payload = {"jsonrpc": "2.0", "id": "helius_source", "method": method, "params": params}
    for attempt in range(1, MAX_RETRIES + 1):
        resp = httpx.post(url, json=payload, timeout=30.0)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait_s = float(retry_after) if retry_after else min(2 ** attempt, RETRY_BACKOFF_CAP_S)
            time.sleep(wait_s)
            continue
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise RuntimeError(f"Helius RPC error calling {method}: {body['error']}")
        return body
    raise RuntimeError(
        f"Still getting 429 from {method} after {MAX_RETRIES} retries with backoff -- "
        f"a real, sustained rate limit, not a transient blip."
    )


def resolve_pool_owner(mint: str, api_key: str) -> Optional[str]:
    """Ported (logic identical) from tape/scripts/helius_probe.py, confirmed
    live 2026-09-21 (D25) against a real, actively-traded pool. Both calls
    used are plain, documented Solana RPC methods, not Helius-specific and
    not a program decoder -- address-graph lookup, per D3. Returns None --
    never a guess -- if the mint has no token accounts or the response
    doesn't have the expected jsonParsed SPL-token-account shape."""
    largest = _rpc_call("getTokenLargestAccounts", [mint], api_key)
    accounts = (largest.get("result") or {}).get("value") or []
    if not accounts:
        return None
    top_account = accounts[0]["address"]
    info = _rpc_call("getAccountInfo", [top_account, {"encoding": "jsonParsed"}], api_key)
    try:
        return info["result"]["value"]["data"]["parsed"]["info"]["owner"]
    except (KeyError, TypeError):
        return None


def _fetch_signatures(owner: str, mint: str, since_ms: int, until_ms: int, api_key: str) -> List[str]:
    """Unique signatures for swaps of `mint` touching `owner` in
    [since_ms, until_ms].

    REAL BUG, found live 2026-09-21 running this against a busy pool that
    was still trading well after the tape window used to verify it ends:
    the first version of this function used `getSignaturesForAddress`,
    which has NO server-side time-range filter at all -- only a
    `before`/`until` SIGNATURE cursor -- so it must page BACKWARD from
    "now" through every signature newer than the window before it ever
    reaches the window itself. For a pool that kept trading for another day
    past the window under test, that meant scanning thousands of
    irrelevant, more-recent signatures first, with zero visibility, before
    the actual per-transaction fetch even started -- indistinguishable from
    hung.

    Fixed by switching to `getTransfersByAddress` (the same endpoint D25's
    `helius_probe.py` already proved fast for exactly this pool -- 18,130
    real records pulled) purely as a SIGNATURE-DISCOVERY index: it takes a
    `mint` filter AND a server-side `blockTime.gte/lt` filter, so pagination
    is bounded by what's actually in the window, not by how much history
    exists after it. Its own amounts/sides are NOT used here (D25 left
    genuinely open whether it reliably carries the SOL leg) -- only
    `signature`, which is unambiguous regardless of that question. Every
    signature it returns still goes through a real `getTransaction` call
    in `historical()` for the actual balance-delta reconstruction, so any
    gap in `getTransfersByAddress`'s own data quality cannot leak into the
    result, only into how it's found.

    Same stall-guard as `helius_probe.py::fetch_helius_transfers`
    (paginationToken repeating across two consecutive pages -- a real bug,
    not just a busy pool), and the same progress printing, for the same
    reason: this can be a lot of pages for a busy pool and must never go
    silent.
    """
    since_s, until_s = since_ms // 1000, until_ms // 1000
    out: List[str] = []
    seen: set = set()
    pagination_token = None
    page_num = 0
    while True:
        page_num += 1
        params_obj: dict = {
            "mint": mint,
            "limit": 100,
            "sortOrder": "asc",
            "solMode": "merged",
            "filters": {"blockTime": {"gte": since_s, "lt": until_s + 1}},
        }
        if pagination_token:
            params_obj["paginationToken"] = pagination_token
        body = _rpc_call("getTransfersByAddress", [owner, params_obj], api_key)
        result = body.get("result") or {}
        page_data = result.get("data") or []
        for entry in page_data:
            sig = entry.get("signature")
            if sig and sig not in seen:
                seen.add(sig)
                out.append(sig)
        new_token = result.get("paginationToken")
        if new_token is not None and new_token == pagination_token:
            raise RuntimeError(
                f"paginationToken did not advance across two consecutive pages "
                f"(stuck at {new_token!r}) after {page_num} pages / {len(out)} "
                f"signatures -- a real pagination bug, not just a busy pool."
            )
        if page_num % 10 == 0 or new_token is None:
            print(f"[helius] ... signature discovery page {page_num}: "
                  f"{len(out)} unique signatures so far", file=sys.stderr)
        if new_token is None:
            break
        pagination_token = new_token
        time.sleep(INTER_CALL_DELAY_S)
    return out


def _signer_pubkey(account_keys: list) -> Optional[str]:
    """accountKeys[0] is always the fee payer / first required signer on
    Solana -- a protocol-level invariant, not an inference. jsonParsed
    encoding can return each entry as either a bare pubkey string or a
    `{"pubkey": ..., "signer": bool, ...}` object (confirmed both shapes
    live via helius_probe.py's Step 3, 2026-09-21) -- handle both rather
    than assume one."""
    if not account_keys:
        return None
    first = account_keys[0]
    return first.get("pubkey") if isinstance(first, dict) else first


def _native_sol_delta(pre_balances: list, post_balances: list, idx: int) -> Optional[float]:
    """The signer's own lamport delta, net of the network fee (not of any
    AMM trading fee) -- same small, accepted discrepancy documented in
    parse_bigquery_export.py's module docstring, not a bug."""
    if idx >= len(pre_balances) or idx >= len(post_balances):
        return None
    try:
        return (float(post_balances[idx]) - float(pre_balances[idx])) / 1e9
    except (TypeError, ValueError):
        return None


def _ui_amount(entry: Optional[dict]) -> tuple:
    """(raw_amount, decimals) from a jsonParsed token-balance entry's
    `uiTokenAmount`, or (0.0, 0) if `entry` is None (see `_token_delta`)."""
    if entry is None:
        return 0.0, 0
    ui = entry.get("uiTokenAmount") or {}
    try:
        return float(ui.get("amount", 0)), int(ui.get("decimals", 0))
    except (TypeError, ValueError):
        return 0.0, 0


def _token_deltas_all(pre_token_balances: list, post_token_balances: list,
                       pubkey: str) -> dict:
    """Like `_token_delta`, but for EVERY mint `pubkey`'s balance changed in
    this transaction, not one pre-specified mint -- what `from_signatures()`
    needs, since a signature discovered via a bare program-id scan (D38,
    docs/DECISIONS.md) doesn't come with a mint attached the way a per-mint
    `historical()` call does. Same index-based join and closed/created-account
    zero-fill as `_token_delta` (D22 follow-up), just grouped by mint instead
    of filtered to one. Returns {} (never a guess) if `pubkey` touched no
    token balances at all -- e.g. they were only the fee payer."""
    pre_by_idx = {b["accountIndex"]: b for b in pre_token_balances if "accountIndex" in b}
    post_by_idx = {b["accountIndex"]: b for b in post_token_balances if "accountIndex" in b}
    mints_by_idx: dict = {}
    for entry in list(pre_by_idx.values()) + list(post_by_idx.values()):
        if entry.get("owner") == pubkey and "mint" in entry:
            mints_by_idx[entry["accountIndex"]] = entry["mint"]
    deltas: dict = {}
    for idx, mint in mints_by_idx.items():
        pre = pre_by_idx.get(idx)
        post = post_by_idx.get(idx)
        pre_raw, pre_dec = _ui_amount(pre)
        post_raw, post_dec = _ui_amount(post)
        decimals = pre_dec or post_dec
        pre_amt = pre_raw / (10 ** decimals) if pre is not None else 0.0
        post_amt = post_raw / (10 ** decimals) if post is not None else 0.0
        delta = post_amt - pre_amt
        if delta != 0:
            deltas[mint] = delta
    return deltas


def _counterparty_owner(pre_token_balances: list, post_token_balances: list,
                         mint: str, exclude_pubkey: str) -> Optional[str]:
    """The OTHER owner touched for `mint` in this transaction -- the pool
    vault, in the two-party case every pump.fun/PumpSwap trade actually is.
    Free (reads the same `getTransaction` response already fetched) versus
    the `resolve_pool_owner()` extra RPC round-trip `historical()` pays for
    when the mint (and therefore its pool) is already known ahead of time.
    Returns the first non-`exclude_pubkey` owner found for `mint`, or None
    -- never a guess -- if there isn't one (e.g. a 3+-party route this
    two-owner assumption doesn't hold for; `from_signatures()` falls back to
    a literal `\"unknown\"` pool in that case rather than fabricate one)."""
    for entry in list(pre_token_balances) + list(post_token_balances):
        if entry.get("mint") == mint and entry.get("owner") not in (None, exclude_pubkey):
            return entry["owner"]
    return None


def _token_delta(pre_token_balances: list, post_token_balances: list,
                  pubkey: str, mint: str) -> Optional[float]:
    """Mirrors tape/scripts/parse_bigquery_export.py::_token_delta exactly,
    field names adjusted for Helius's jsonParsed shape (`accountIndex`
    camelCase vs BigQuery's `account_index`; amount+decimals nested under
    `uiTokenAmount` vs flat). Same closed/created-account handling (D22
    follow-up): absent from `pre` reads as balance 0 (freshly-created
    account), absent from `post` reads as balance 0 (account closed on a
    full-balance sell, a protocol-level invariant -- closing requires
    already being empty). Matched by `accountIndex`, the stable join key.
    None -- never a guess -- if no entry for (pubkey, mint) exists on
    either side."""
    pre_by_idx = {b["accountIndex"]: b for b in pre_token_balances if "accountIndex" in b}
    post_by_idx = {b["accountIndex"]: b for b in post_token_balances if "accountIndex" in b}
    matched_idx = None
    for entry in list(pre_by_idx.values()) + list(post_by_idx.values()):
        if entry.get("owner") == pubkey and entry.get("mint") == mint:
            matched_idx = entry["accountIndex"]
            break
    if matched_idx is None:
        return None
    pre = pre_by_idx.get(matched_idx)
    post = post_by_idx.get(matched_idx)
    pre_raw, pre_dec = _ui_amount(pre)
    post_raw, post_dec = _ui_amount(post)
    decimals = pre_dec or post_dec
    pre_amt = pre_raw / (10 ** decimals) if pre is not None else 0.0
    post_amt = post_raw / (10 ** decimals) if post is not None else 0.0
    return post_amt - pre_amt


class HeliusSource(SourceAdapter):
    name = "helius"

    def __init__(self, api_key: str | None = None, max_workers: int = DEFAULT_MAX_WORKERS) -> None:
        load_project_dotenv()
        self.api_key = api_key or os.environ.get("HELIUS_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "HELIUS_API_KEY not set. Put `HELIUS_API_KEY=...` on its own line "
                "in tape/.env, or set it as a real environment variable."
            )
        self.max_workers = max_workers

    def historical(self, mint: str, start_ms: int, end_ms: int) -> Iterator[CanonicalSwap]:
        """Swaps for `mint` in [start_ms, end_ms], ascending by ts_ms.

        Resolves the pool's vault-owner address once (D25), pulls every
        signature that touched it in the window, then reconstructs each
        transaction's swap from ledger balance deltas -- one
        `getTransaction` call per signature, so this is O(signatures), not
        O(pages) the way the `getTransfersByAddress` approach was. Raises
        loud (never yields nothing silently) if the pool owner can't be
        resolved at all -- that's a real problem with the mint/lookup, not
        "no trades in this window".

        PROGRESS IS PRINTED (to stderr) -- found live, 2026-09-21: a busy
        pump.fun pool can have 10,000+ signatures in a few-hour window (the
        same pool D25's `getTransfersByAddress` probe found 18,130 transfer
        records for), and one `getTransaction` call per signature at
        roughly a few hundred ms each is tens of minutes of real wall-clock
        time with NOTHING to show for it if this stays silent -- which the
        first version of this method did, and it looked indistinguishable
        from hung. Never go silent on a loop that can run this long.
        """
        owner = resolve_pool_owner(mint, self.api_key)
        if owner is None:
            raise RuntimeError(
                f"Could not resolve a pool-vault owner for {mint} -- see "
                f"resolve_pool_owner. Nothing downstream of this can mean "
                f"anything without it."
            )
        print(f"[helius] resolved pool owner {owner} for {mint}", file=sys.stderr)
        # _fetch_signatures already returns ascending order (`sortOrder: "asc"`
        # on getTransfersByAddress) -- no reversal needed, unlike the old
        # getSignaturesForAddress-based version this replaced.
        sigs = _fetch_signatures(owner, mint, start_ms, end_ms, self.api_key)
        sigs = list(dict.fromkeys(sigs))  # de-dup up front, preserve order -- see below
        total = len(sigs)
        print(f"[helius] {total} unique signatures in window -- fetching via getTransaction "
              f"with {self.max_workers} concurrent workers (D42, docs/DECISIONS.md: was "
              f"one-at-a-time with a fixed delay, sized for the Free tier this project no "
              f"longer runs on)", file=sys.stderr)

        def _fetch_one(sig: str) -> dict:
            return _rpc_call(
                "getTransaction",
                [sig, {"encoding": "jsonParsed",
                       "maxSupportedTransactionVersion": MAX_SUPPORTED_TX_VERSION}],
                self.api_key,
            )

        # `executor.map` yields results in the SAME ORDER as `sigs` (not
        # completion order) while running up to `max_workers` calls
        # concurrently underneath -- this is what makes it a safe drop-in
        # for a loop `historical()`'s own contract requires to be
        # deterministic and ascending by ts_ms (sigs are already ascending,
        # D31/D32), without needing to buffer and re-sort results by hand.
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for sig, tx in zip(sigs, executor.map(_fetch_one, sigs)):
                completed += 1
                if completed % 100 == 0 or completed == total:
                    print(f"[helius] ... {completed}/{total} signatures processed", file=sys.stderr)
                swap = self._to_canonical(tx, sig, mint, owner)
                if swap is not None:
                    yield swap

    def from_signatures(self, sigs, checkpoint_path: Optional[str] = None) -> Iterator[CanonicalSwap]:
        """The other half of D38's universe-scaling plan (docs/DECISIONS.md):
        `historical()` needs a MINT to start from (resolve its pool, find its
        signatures) -- exactly backwards when the goal is DISCOVERING mints
        this project doesn't have tapes for yet. This takes a flat list of
        signatures from anywhere (the intended source: a cheap BigQuery scan
        for transactions touching the pump.fun/PumpSwap program ids, see
        `tape/scripts/bigquery_discover.py` -- but it doesn't care where the
        list came from) and decodes each one for WHATEVER mint(s) actually
        moved for its signer, via `_token_deltas_all` instead of the
        one-mint `_token_delta` `historical()` uses. A signature can yield
        zero swaps (signer wasn't a party to any token movement -- just the
        fee payer for someone else), one (the common case), or more than one
        (a route that touched multiple mints in one transaction) -- all
        handled the same way, one CanonicalSwap per (signature, mint) pair.

        `pool` is filled from `_counterparty_owner` (free -- reads the same
        response) rather than a second `resolve_pool_owner` RPC round-trip
        per signature, which would undo most of the point of skipping
        per-mint signature discovery in the first place.

        CHECKPOINTED if `checkpoint_path` is given: every processed
        signature is appended immediately after its `getTransaction` call
        returns, and already-checkpointed signatures are skipped at start.
        A discovery-driven pull can be many thousands of signatures with no
        natural "window" to resume by half-open interval the way
        `historical()`'s since_ms/until_ms does -- losing progress on an
        interrupted run here means re-paying for calls already made, the
        exact waste D36 fixed on the per-mint path. Silent about mints
        never seen before, unlike `historical()`'s loud pool-resolution
        failure -- there is no "expected" pool here to fail to resolve."""
        done: set = set()
        if checkpoint_path and os.path.exists(checkpoint_path):
            with open(checkpoint_path, "r") as f:
                done = {line.strip() for line in f if line.strip()}
            print(f"[helius] resuming: {len(done)} signature(s) already checkpointed, skipping",
                  file=sys.stderr)
        sigs = [s for s in sigs if s not in done]
        total = len(sigs)
        print(f"[helius] decoding {total} signature(s) via getTransaction with "
              f"{self.max_workers} concurrent workers (D42)", file=sys.stderr)
        checkpoint_file = open(checkpoint_path, "a") if checkpoint_path else None
        try:
            # Fetches run concurrently (worker threads); the checkpoint write
            # and the yield both stay in THIS thread, in `sigs` order (the
            # same `executor.map` ordering guarantee `historical()` relies
            # on) -- so the checkpoint file is never written out of order or
            # from two threads at once, even though the network calls behind
            # it are parallel.
            completed = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                for sig, tx in zip(sigs, executor.map(self._rpc_call_getTransaction, sigs)):
                    completed += 1
                    if completed % 100 == 0 or completed == total:
                        print(f"[helius] ... {completed}/{total} signatures processed", file=sys.stderr)
                    for swap in self._to_canonical_any_mint(tx, sig):
                        yield swap
                    if checkpoint_file:
                        checkpoint_file.write(sig + "\n")
                        checkpoint_file.flush()
        finally:
            if checkpoint_file:
                checkpoint_file.close()

    def _rpc_call_getTransaction(self, sig: str) -> dict:
        return _rpc_call(
            "getTransaction",
            [sig, {"encoding": "jsonParsed",
                   "maxSupportedTransactionVersion": MAX_SUPPORTED_TX_VERSION}],
            self.api_key,
        )

    def _to_canonical_any_mint(self, tx: dict, sig: str) -> List[CanonicalSwap]:
        """Like `_to_canonical`, but for every mint the signer's balance
        changed for, not one pre-specified mint -- see `from_signatures`."""
        result = tx.get("result")
        if result is None:
            return []
        meta = result.get("meta") or {}
        if meta.get("err") is not None:
            return []
        message = (result.get("transaction") or {}).get("message") or {}
        account_keys = message.get("accountKeys") or []
        block_time = result.get("blockTime")
        slot = result.get("slot")
        if block_time is None or slot is None:
            return []
        signer = _signer_pubkey(account_keys)
        if signer is None:
            return []
        pre_tb = meta.get("preTokenBalances") or []
        post_tb = meta.get("postTokenBalances") or []
        deltas = _token_deltas_all(pre_tb, post_tb, signer)
        deltas.pop(SOL_MINT, None)  # the quote leg, not a base token being swapped
        if not deltas:
            return []
        sol_delta = _native_sol_delta(meta.get("preBalances") or [], meta.get("postBalances") or [], 0)
        if sol_delta is None:
            return []
        program_ids = sorted({
            ix.get("programId") if isinstance(ix, dict) else None
            for ix in (message.get("instructions") or [])
            if isinstance(ix, dict) and ix.get("programId")
        })
        venue = _venue_from_program_ids(program_ids)
        # KNOWN LIMITATION, not handled: a tx touching >1 mint (e.g. a router
        # hop through an intermediate token) would attribute the SAME
        # sol_delta to each mint's swap, double-counting quote volume. Rare
        # for a direct pump.fun/PumpSwap trade (the overwhelming majority of
        # what a bare program-id scan finds), not rare in general -- revisit
        # if a real run's swap counts look inflated versus signature counts.
        out = []
        for mint, token_delta in deltas.items():
            side = BUY if token_delta > 0 else SELL
            base_amount = abs(token_delta)
            quote_amount = abs(sol_delta)
            price = quote_amount / base_amount if base_amount else 0.0
            pool = _counterparty_owner(pre_tb, post_tb, mint, signer) or "unknown"
            out.append(CanonicalSwap(
                mint=mint, venue=venue, pool=pool, ts_ms=int(block_time) * 1000,
                slot=int(slot), sig=sig, side=side, base_amount=base_amount,
                quote_amount=quote_amount, quote_mint=SOL_MINT, price=price,
                wallet=signer, source=self.name,
            ))
        return out

    async def stream(self) -> AsyncIterator[CanonicalSwap]:  # pragma: no cover
        raise NotImplementedError(
            "No live path implemented or verified for this adapter -- Bitquery's "
            "WebSocket (also unverified, see sources/bitquery.py::stream) or a "
            "Helius geyser/websocket subscription would need to be built and "
            "checked against real data before this is trusted, same discipline "
            "as historical() above."
        )

    def _to_canonical(self, tx: dict, sig: str, mint: str, pool: str) -> Optional[CanonicalSwap]:
        """One `getTransaction` response -> `CanonicalSwap`, or None if a
        field this needs is missing/unparsable, the transaction failed, or
        the signer's balance for `mint` didn't actually change (not a real
        swap of this mint for them -- e.g. they were only paying the fee
        for someone else's trade). Never guesses a value."""
        result = tx.get("result")
        if result is None:
            return None
        meta = result.get("meta") or {}
        if meta.get("err") is not None:
            return None
        message = (result.get("transaction") or {}).get("message") or {}
        account_keys = message.get("accountKeys") or []
        block_time = result.get("blockTime")
        slot = result.get("slot")
        if block_time is None or slot is None:
            return None
        signer = _signer_pubkey(account_keys)
        if signer is None:
            return None
        token_delta = _token_delta(
            meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or [], signer, mint,
        )
        if token_delta is None or token_delta == 0:
            return None
        sol_delta = _native_sol_delta(meta.get("preBalances") or [], meta.get("postBalances") or [], 0)
        if sol_delta is None:
            return None
        side = BUY if token_delta > 0 else SELL
        base_amount = abs(token_delta)
        quote_amount = abs(sol_delta)
        price = quote_amount / base_amount if base_amount else 0.0
        program_ids = sorted({
            ix.get("programId") if isinstance(ix, dict) else None
            for ix in (message.get("instructions") or [])
            if isinstance(ix, dict) and ix.get("programId")
        })
        venue = _venue_from_program_ids(program_ids)
        return CanonicalSwap(
            mint=mint,
            venue=venue,
            pool=pool,
            ts_ms=int(block_time) * 1000,
            slot=int(slot),
            sig=sig,
            side=side,
            base_amount=base_amount,
            quote_amount=quote_amount,
            quote_mint=SOL_MINT,
            price=price,
            wallet=signer,
            source=self.name,
        )

    def discover(self, start_ms: int, end_ms: int) -> Iterator[dict]:
        raise NotImplementedError(
            "Select on POOL CREATION TIME, never on an outcome -- same warning as "
            "sources/bitquery.py::discover. Not yet implemented for Helius."
        )
