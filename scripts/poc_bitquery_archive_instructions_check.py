#!/usr/bin/env python3
"""PROOF OF CONCEPT -- does the paid `dataset: archive` add-on (D26, $100/mo,
already purchased) cover the `Instructions` cube at all?

WHY THIS EXISTS: D61 just pinned `dataset: realtime`'s wall for
`Instructions`/`create_v2` between 11h and 12h -- essentially the SAME wall
D21 found for a completely different cube (`DEXTradeByTokens`), suggesting
this may be a dataset-wide retention limit, not a per-cube one. D40
already found, live, that `archive` does NOT cover `DEXTradeByTokens` for
Solana at all (a real, confirmed 403/error, not assumed) -- but `archive`
has never been tried against `Instructions` specifically. If it works
here, the user's ALREADY-PAID-FOR archive add-on could unlock weeks of
`create_v2` history in a single query, right now, fully solving D54's
calendar-width problem in one step rather than requiring an ongoing
schedule (the fallback if this doesn't pan out).

Two checks, in order:
  1. SANITY: query `dataset: archive` for a RECENT window (last 1h) --
     inside `realtime`'s already-confirmed reach. If this errors or comes
     back empty, `archive` likely doesn't serve `Instructions` at all
     (mirrors D40's finding for the other cube), independent of how far
     back it goes.
  2. REACH: if the sanity check passes, query `dataset: archive` for
     windows well BEYOND `realtime`'s ~11h wall (1 day, 3 days, 7 days, 14
     days, 30 days back) to see how far real coverage actually extends --
     printed from real evidence, not assumed to match the "~30 day"
     marketing claim any more than `realtime` did.

Writes nothing to the Store. Read-only, small `limit` at every probe.

    python scripts/poc_bitquery_archive_instructions_check.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.env import load_project_dotenv
from tape.sources.bitquery import PUMPFUN_PROGRAM, _ms_to_bq_iso

ENDPOINT = "https://streaming.bitquery.io/graphql"
MAX_RETRIES = 5
RETRY_BACKOFF_CAP_S = 30.0
WINDOW_MINUTES = 10


def _post_query(query: str, variables: dict, api_key: str) -> dict:
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
            return {"__error__": f"HTTP {resp.status_code}: {resp.text[:800]!r}"}
        body = resp.json()
        if body.get("errors"):
            return {"__error__": f"GraphQL errors: {body['errors']}"}
        return body["data"]
    return {"__error__": f"Still 429 after {MAX_RETRIES} retries."}


def probe_query(dataset: str) -> str:
    # `dataset` is a bare, unquoted argument on the `Solana` field itself
    # (same string-substitution convention `tape/sources/bitquery.py` uses
    # for TRADES_QUERY_TEMPLATE/DISCOVER_QUERY_TEMPLATE -- never a $variable,
    # per that module's own comment on why).
    return f"""
query ProbeArchive($program: String!, $method: String!, $since: DateTime!, $until: DateTime!) {{
  Solana(dataset: {dataset}) {{
    Instructions(
      limit: {{count: 5}}
      orderBy: {{ascending: Block_Time}}
      where: {{
        Instruction: {{ Program: {{ Address: {{is: $program}}, Method: {{is: $method}} }} }}
        Block: {{ Time: {{after: $since, before: $until}} }}
      }}
    ) {{
      Block {{ Time }}
      Transaction {{ Signature }}
    }}
  }}
}}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--method", default="create_v2")
    ap.add_argument("--days-back", type=float, nargs="+", default=[1, 3, 7, 14, 30])
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    now_ms = int(time.time() * 1000)

    print("=" * 78)
    print("CHECK 1: SANITY -- dataset: archive, last 1 hour (inside realtime's own "
          "confirmed reach)")
    print("=" * 78)
    since_ms = now_ms - 60 * 60_000
    until_ms = now_ms
    data = _post_query(probe_query("archive"), {
        "program": PUMPFUN_PROGRAM, "method": args.method,
        "since": _ms_to_bq_iso(since_ms), "until": _ms_to_bq_iso(until_ms),
    }, api_key)
    if "__error__" in data:
        print(f"  ERROR: {data['__error__']}")
        print("\n  archive errored even on a RECENT window -- strong evidence this add-on")
        print("  does not serve the Instructions cube at all (mirrors D40's finding for")
        print("  DEXTradeByTokens). Stopping here; do not proceed to CHECK 2 on an")
        print("  add-on that already failed the sanity check.")
        return 0
    rows = data["Solana"]["Instructions"]
    print(f"  {len(rows)} row(s) for the last 1h via archive")
    if not rows:
        print("\n  Zero rows (no error) on a RECENT window where realtime reliably found")
        print("  5+ rows -- ambiguous by itself (could be real lag in archive's own")
        print("  ingestion, or archive genuinely not covering this cube/program). Try a")
        print("  wider recent window (--days-back with a small value close to 0) before")
        print("  concluding either way.")
    else:
        print("  archive DOES return real, recent Instructions rows -- proceeding to CHECK 2.")

    print("\n" + "=" * 78)
    print("CHECK 2: REACH -- dataset: archive, at increasing day-offsets beyond "
          "realtime's ~11h wall")
    print("=" * 78)
    print(f"{'days_back':>10s} {'window_start':>22s} {'rows':>6s} {'first_block_time':>22s}")
    print("-" * 66)
    for db in sorted(args.days_back):
        target_ms = now_ms - int(db * 86_400_000)
        window_end_ms = target_ms + WINDOW_MINUTES * 60_000
        data = _post_query(probe_query("archive"), {
            "program": PUMPFUN_PROGRAM, "method": args.method,
            "since": _ms_to_bq_iso(target_ms), "until": _ms_to_bq_iso(window_end_ms),
        }, api_key)
        if "__error__" in data:
            print(f"{db:10.1f} {_ms_to_bq_iso(target_ms):>22s} {'ERROR':>6s}  "
                  f"{data['__error__'][:60]}")
            continue
        rows = data["Solana"]["Instructions"]
        first_bt = rows[0]["Block"]["Time"] if rows else ""
        print(f"{db:10.1f} {_ms_to_bq_iso(target_ms):>22s} {len(rows):6d} {first_bt:>22s}")
        time.sleep(0.2)

    print("\nInterpretation:")
    print("  If CHECK 2 shows rows > 0 at day-offsets realtime could never reach (>0.5d):")
    print("  archive covers Instructions, and a single wide backfill (using this exact")
    print("  query, paginated, deduped by mint per D60's multi-quote-currency finding)")
    print("  can build a calendar-diverse universe RIGHT NOW -- fully solving D54.")
    print("  If every offset in CHECK 2 is empty/errors despite CHECK 1 passing: archive")
    print("  has its own, shorter reach for this cube -- print exactly where it stops,")
    print("  same as any other empirically-measured wall in this project.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
