#!/usr/bin/env python3
"""PRODUCTION discovery tool (not a poc_* script) -- the answer D54-D62
were working toward: incrementally discover genuinely NEW pump.fun
launches directly from on-chain `create_v2` events (Bitquery's
`Solana.Instructions` cube, `Program.Method: "create_v2"`, confirmed live
D60 to give a REAL creation time matching pump.fun's own API to the
second, delta=+0.0s), and cache them in the SAME file format
`scripts/fetch_real_creation_times.py` already writes
(`<data>/real_creation_times.json`), so `information_audit.py`'s existing
`--real-creation-file` age filter (D55) needs ZERO code changes to use
this strictly-better source instead of (or alongside) the unofficial
pump.fun frontend API.

WHY THIS EXISTS, AND WHY IT MUST RUN ON A SCHEDULE, NOT ONCE:
D61 confirmed `dataset: realtime` only reaches ~11h back for this cube
(matches D21's unrelated ~9-12h finding for `DEXTradeByTokens` -- likely a
account-wide `realtime` retention limit, not per-cube). D62 confirmed the
already-purchased `dataset: archive` add-on is explicitly PLAN-RESTRICTED
for `Instructions` (a clean, self-describing 403, not ambiguous). There is
therefore no single query that retroactively fills a wide calendar window
-- discovery must accumulate over REAL elapsed time, by running this
script repeatedly (the user can schedule it via Windows Task Scheduler, or
run it by hand every several hours; anything under ~10h between runs
leaves margin below the ~11h wall found live in D61).

This is a strictly cheaper version of the "spread-out backfill" option the
user passed over earlier in this thread (D56): `create_v2` events are rare
(~50+/hour lower bound, D60) relative to the ~736,000/hour total signature
firehose D57 measured, so polling for JUST the discovery event is tiny --
full swap-history backfill for each newly-discovered mint (a SEPARATE,
already-existing step via `backfill_bitquery.py`/Helius `historical()`,
NOT done by this script) can then start from very near the mint's real
birth, rather than needing to happen up front for a token that might not
even be interesting yet.

KNOWN DATA QUALITY ISSUE, handled explicitly (D60): the same mint can
appear under multiple `create_v2` events (pump.fun's multi-quote-currency
support -- `add_quote_mint` et al in the live IDL -- likely fires
`create_v2` once per (mint, quote) pair, not once per mint). This script
deduplicates by mint, keeping the EARLIEST `Block.Time` seen for it in
each run, before ever touching the cache file.

MERGE POLICY, deliberately conservative: an existing cache entry already
recorded as `"status": "ok"` (from ANY source, including the older
pump.fun-frontend-API-based `fetch_real_creation_times.py`) is NEVER
overwritten -- this script only ADDS mints the cache doesn't have yet, or
fills in ones previously recorded as `"error"`/`"implausible"`/absent.
Never silently replaces a value that was already trusted.

Writes to the cache file. Otherwise read-only (does not touch the Store or
run any backfill itself).

    python scripts/discover_pumpfun_launches.py --data data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.env import load_project_dotenv
from tape.sources.bitquery import PUMPFUN_PROGRAM, _ms_to_bq_iso, _parse_bq_iso_ms

ENDPOINT = "https://streaming.bitquery.io/graphql"
MAX_RETRIES = 5
RETRY_BACKOFF_CAP_S = 30.0
PAGE_LIMIT = 1000            # matches tape/sources/bitquery.py's PAGE_LIMIT
INTER_PAGE_DELAY_S = 0.5     # matches tape/sources/bitquery.py's INTER_PAGE_DELAY_S

# D61, confirmed live: dataset:realtime's wall for this cube is strictly
# between 11h and 12h. Default lookback stays under that with margin, so a
# first-ever run (no existing cache) doesn't immediately hit the wall.
DEFAULT_FIRST_RUN_LOOKBACK_HOURS = 10.0
# Overlap applied when RESUMING from an existing cache's latest timestamp,
# so a boundary event straddling two runs is never silently missed (same
# reasoning as any checkpoint/resume logic in this project -- prefer a
# redundant re-fetch over a silent gap).
RESUME_OVERLAP_MINUTES = 15

SOURCE_TAG = "bitquery_create_v2"

CREATE_QUERY = """
query CreateEvents($program: String!, $method: String!, $since: DateTime!, $until: DateTime!, $limit: Int!) {
  Solana(dataset: realtime) {
    Instructions(
      limit: {count: $limit}
      orderBy: {ascending: Block_Time}
      where: {
        Instruction: { Program: { Address: {is: $program}, Method: {is: $method} } }
        Block: { Time: {after: $since, before: $until} }
      }
    ) {
      Block { Time }
      Transaction { Signature }
      Instruction {
        Accounts { Address IsWritable }
        Program { Address Method Name AccountNames }
      }
    }
  }
}
"""


def _post_query(query: str, variables: dict, api_key: str) -> dict:
    """Same 429 retry/backoff discipline as `tape/sources/bitquery.py`'s
    `_post_with_retry` (D44) -- its own small copy, same reasoning as every
    other per-script helper in this project."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = httpx.post(
            ENDPOINT,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30.0,
        )
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait_s = float(retry_after) if retry_after else min(2 ** attempt, RETRY_BACKOFF_CAP_S)
            print(f"  429 rate limited, waiting {wait_s:.0f}s (retry {attempt}/{MAX_RETRIES})",
                  file=sys.stderr)
            time.sleep(wait_s)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:2000]!r}")
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Bitquery returned errors: {body['errors']}")
        return body["data"]
    raise RuntimeError(f"Still 429 after {MAX_RETRIES} retries.")


def extract_mint(accounts: List[dict], account_names: List[str]) -> Optional[str]:
    """`Accounts` and `Program.AccountNames` are parallel lists (D58/D60,
    confirmed live: matched a real mint correctly on the first try). Case-
    insensitive match on the name "mint", never a fixed positional index --
    the IDL declares 14+ accounts and this project has already been burned
    once (D55) by trusting a position instead of a name. Returns None
    (never a guess) if "mint" isn't present or the lists are misaligned."""
    if not accounts or not account_names:
        return None
    lowered = [str(n).lower() for n in account_names]
    if "mint" not in lowered:
        return None
    idx = lowered.index("mint")
    if idx >= len(accounts):
        return None
    entry = accounts[idx]
    if not isinstance(entry, dict):
        return None
    return entry.get("Address")


def parse_row(row: dict) -> Optional[dict]:
    """One `Instructions` row -> {"mint", "ts_ms", "sig"}, or None if any
    required field is missing/unparsable. Never guesses a value."""
    bt = (row.get("Block") or {}).get("Time")
    sig = (row.get("Transaction") or {}).get("Signature")
    instr = row.get("Instruction") or {}
    accounts = instr.get("Accounts") or []
    program = instr.get("Program") or {}
    account_names = program.get("AccountNames") or []
    mint = extract_mint(accounts, account_names)
    ts_ms = _parse_bq_iso_ms(bt) if bt else None
    if mint is None or ts_ms is None or sig is None:
        return None
    return {"mint": mint, "ts_ms": ts_ms, "sig": sig}


def dedupe_keep_earliest(records: List[dict]) -> Dict[str, dict]:
    """D60's multi-quote-currency finding, confirmed live: the same mint can
    appear under more than one `create_v2` event. Keeps the record with the
    SMALLEST `ts_ms` per mint -- by definition closer to the true first
    creation instant regardless of which quote-currency variant fired."""
    by_mint: Dict[str, dict] = {}
    for r in records:
        m = r["mint"]
        if m not in by_mint or r["ts_ms"] < by_mint[m]["ts_ms"]:
            by_mint[m] = r
    return by_mint


def merge_into_cache(cache: dict, new_by_mint: Dict[str, dict], now_ms: int) -> Tuple[dict, int, int]:
    """Conservative merge (see module docstring): never overwrites an
    existing `"status": "ok"` entry, from ANY source. Returns
    (updated_cache, n_added_or_fixed, n_skipped_already_ok)."""
    added, skipped = 0, 0
    for mint, rec in new_by_mint.items():
        existing = cache.get(mint)
        if existing is not None and existing.get("status") == "ok":
            skipped += 1
            continue
        cache[mint] = {
            "status": "ok",
            "real_created_ts_ms": rec["ts_ms"],
            "fetched_at_ms": now_ms,
            "source": SOURCE_TAG,
            "sig": rec["sig"],
        }
        added += 1
    return cache, added, skipped


def fetch_create_v2_events(program: str, method: str, since_ms: int, until_ms: int,
                            api_key: str) -> List[dict]:
    """Time-based pagination, mirroring `tape/sources/bitquery.py::discover()`
    exactly (advance `since_ms` to the max timestamp seen each page, stop
    when a page comes back short of `PAGE_LIMIT`, stall-guard against a
    full page of zero new records repeating at the same `since_ms`) --
    reused because it's the same underlying cube/pagination model, not
    re-derived from scratch."""
    out: List[dict] = []
    seen_sigs: set = set()
    stall_guard_last_since = None
    page_num = 0
    while since_ms <= until_ms:
        page_num += 1
        data = _post_query(CREATE_QUERY, {
            "program": program, "method": method,
            "since": _ms_to_bq_iso(since_ms), "until": _ms_to_bq_iso(until_ms),
            "limit": PAGE_LIMIT,
        }, api_key)
        rows = data["Solana"]["Instructions"]
        new_this_page = 0
        max_ts_this_page = since_ms
        for row in rows:
            parsed = parse_row(row)
            if parsed is None:
                continue
            max_ts_this_page = max(max_ts_this_page, parsed["ts_ms"])
            if parsed["sig"] in seen_sigs:
                continue
            seen_sigs.add(parsed["sig"])
            new_this_page += 1
            out.append(parsed)
        print(f"  page {page_num}: {len(rows)} row(s), {new_this_page} new "
              f"({len(out)} total so far)", file=sys.stderr)
        if len(rows) < PAGE_LIMIT:
            break
        if new_this_page == 0:
            if stall_guard_last_since == since_ms:
                raise RuntimeError(
                    f"Pagination stalled at {_ms_to_bq_iso(since_ms)}: a full page of "
                    f"{PAGE_LIMIT} rows, all already seen, at the same timestamp."
                )
            stall_guard_last_since = since_ms
        since_ms = max_ts_this_page
        time.sleep(INTER_PAGE_DELAY_S)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default=None,
                     help="cache path (default: <--data>/real_creation_times.json -- the "
                          "SAME file fetch_real_creation_times.py writes)")
    ap.add_argument("--method", default="create_v2",
                     help="confirmed live D60 -- the real pump.fun IDL event name Bitquery "
                          "reports, not the instruction name 'create'")
    ap.add_argument("--since-hours", type=float, default=None,
                     help="how far back to query -- default: resume from the cache's own "
                          "latest bitquery_create_v2 timestamp (minus overlap), or "
                          f"{DEFAULT_FIRST_RUN_LOOKBACK_HOURS}h on a first-ever run")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    out_path = Path(args.out) if args.out else Path(args.data) / "real_creation_times.json"
    cache: dict = {}
    if out_path.exists():
        with open(out_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"loaded existing cache: {len(cache)} mint(s) already recorded")

    now_ms = int(time.time() * 1000)
    if args.since_hours is not None:
        since_ms = now_ms - int(args.since_hours * 3_600_000)
    else:
        prior_ts = [rec.get("real_created_ts_ms") for rec in cache.values()
                    if rec.get("source") == SOURCE_TAG and rec.get("real_created_ts_ms")]
        if prior_ts:
            since_ms = max(prior_ts) - RESUME_OVERLAP_MINUTES * 60_000
            print(f"resuming from this cache's latest {SOURCE_TAG} timestamp, "
                  f"minus {RESUME_OVERLAP_MINUTES}min overlap")
        else:
            since_ms = now_ms - int(DEFAULT_FIRST_RUN_LOOKBACK_HOURS * 3_600_000)
            print(f"no prior {SOURCE_TAG} entries in cache -- first run, defaulting to "
                  f"{DEFAULT_FIRST_RUN_LOOKBACK_HOURS}h lookback")

    print(f"\nQuerying Instructions/Method={args.method!r} from {_ms_to_bq_iso(since_ms)} "
          f"to {_ms_to_bq_iso(now_ms)} ...")
    raw_events = fetch_create_v2_events(PUMPFUN_PROGRAM, args.method, since_ms, now_ms, api_key)
    print(f"\n{len(raw_events)} raw event(s) fetched this run")

    by_mint = dedupe_keep_earliest(raw_events)
    n_dupes = len(raw_events) - len(by_mint)
    print(f"{len(by_mint)} distinct mint(s) after dedup "
          f"({n_dupes} duplicate event(s) collapsed -- D60's multi-quote-currency finding)")

    cache, n_added, n_skipped = merge_into_cache(cache, by_mint, now_ms)
    print(f"\n{n_added} new/fixed cache entr{'y' if n_added == 1 else 'ies'}, "
          f"{n_skipped} already-'ok' entr{'y' if n_skipped == 1 else 'ies'} left untouched")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    print(f"\nwrote {len(cache)} total cached mint(s) to {out_path}")
    print(f"\nRun this again periodically (well under the ~11h dataset:realtime wall, D61) "
          f"to keep accumulating calendar coverage. `information_audit.py --real-creation-file "
          f"{out_path}` (its own default, if --data matches) will pick up every new entry "
          f"automatically -- no code changes needed there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
