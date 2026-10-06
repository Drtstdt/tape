#!/usr/bin/env python3
"""PROOF OF CONCEPT -- how far back does `dataset: realtime`'s `Instructions`
cube actually reach for `create_v2` events? D59/D60 confirmed the QUERY
shape works and gives real, on-chain-accurate creation times (cross-check
delta=+0.0s against pump.fun's own API) -- but every real-data check so
far has been against the last 2 hours. D21 already found, the hard way,
that Bitquery's own docs ("~30-day rolling window") did NOT match reality
for `DEXTradeByTokens` (~9-12h empirical wall instead) -- `Instructions` is
a DIFFERENT cube and has never been bisected. Assuming it matches either
`DEXTradeByTokens`'s reach or the docs' claim would be exactly the kind of
guess D3 exists to prevent.

Why this matters: the whole point of pursuing on-chain `create` discovery
(D56's chosen direction, over a bigger Bitquery-realtime trade backfill)
was to get a WIDE, calendar-diverse universe -- D56 found the CURRENT
universe span was only ~3.3 hours. If `Instructions`'s real reach is also
just ~9-12h, this still helps (cheap, accurate creation times) but does
NOT by itself solve the calendar-width problem; if it reaches days or
weeks, it solves both at once.

Same bisection method D21 used for `DEXTradeByTokens` (`Trade.Block.Time`
window counts at increasing hour-offsets), applied to `Instruction.Program.Method
= "create_v2"` instead. A short (10-minute) window at each offset is
enough -- D60 found ~50+ create_v2 events per hour, so a real 10-minute
window at a REACHABLE offset should reliably return >0 rows; an
UNREACHABLE offset should reliably return 0 (or error).

Writes nothing to the Store. Read-only, small `limit` at every offset.

    python scripts/poc_bitquery_instructions_retention.py --hours-back 1 6 12 24 48 72 168 336 720
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
WINDOW_MINUTES = 10  # per-offset probe window; D60 found ~50+/hour, so 10min
                      # at a reachable offset should reliably be non-empty


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
            return {"__error__": f"HTTP {resp.status_code}: {resp.text[:500]!r}"}
        body = resp.json()
        if body.get("errors"):
            return {"__error__": f"GraphQL errors: {body['errors']}"}
        return body["data"]
    return {"__error__": f"Still 429 after {MAX_RETRIES} retries."}


PROBE_QUERY = """
query ProbeReach($program: String!, $method: String!, $since: DateTime!, $until: DateTime!) {
  Solana(dataset: realtime) {
    Instructions(
      limit: {count: 5}
      orderBy: {ascending: Block_Time}
      where: {
        Instruction: { Program: { Address: {is: $program}, Method: {is: $method} } }
        Block: { Time: {after: $since, before: $until} }
      }
    ) {
      Block { Time }
      Transaction { Signature }
    }
  }
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours-back", type=float, nargs="+",
                     default=[1, 6, 12, 24, 48, 72, 168, 336, 720],
                     help="offsets to bisect reach at (720h = 30 days, matching "
                          "Bitquery's own docs claim for the 'realtime' dataset)")
    ap.add_argument("--method", default="create_v2",
                     help="confirmed live D60 -- the pump.fun IDL EVENT name "
                          "Bitquery actually reports, not the instruction name 'create'")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    now_ms = int(time.time() * 1000)
    print(f"Probing Instructions/Method={args.method!r} reach against "
          f"PUMPFUN_PROGRAM ({PUMPFUN_PROGRAM}), dataset=realtime")
    print(f"{'hours_back':>10s} {'window_start':>22s} {'rows':>6s} {'first_block_time':>22s}")
    print("-" * 70)

    first_empty = None
    for hb in sorted(args.hours_back):
        target_ms = now_ms - int(hb * 3_600_000)
        window_end_ms = target_ms + WINDOW_MINUTES * 60_000
        since_iso = _ms_to_bq_iso(target_ms)
        until_iso = _ms_to_bq_iso(window_end_ms)
        data = _post_query(PROBE_QUERY, {
            "program": PUMPFUN_PROGRAM, "method": args.method,
            "since": since_iso, "until": until_iso,
        }, api_key)
        if "__error__" in data:
            print(f"{hb:10.1f} {since_iso:>22s} {'ERROR':>6s}  {data['__error__'][:80]}")
            if first_empty is None:
                first_empty = hb
            continue
        rows = data["Solana"]["Instructions"]
        first_bt = rows[0]["Block"]["Time"] if rows else ""
        print(f"{hb:10.1f} {since_iso:>22s} {len(rows):6d} {first_bt:>22s}")
        if not rows and first_empty is None:
            first_empty = hb
        time.sleep(0.2)

    print("\nInterpretation:")
    print("  rows > 0 at an offset -> reach genuinely extends there (a REAL create_v2")
    print("  event was found in that specific 10-minute window, not just 'no error').")
    print("  rows == 0 or ERROR -> reach does NOT extend that far (or this window")
    print("  happened to have zero creates in it purely by chance -- re-run with a")
    print("  wider WINDOW_MINUTES at that one offset to rule that out before concluding")
    print("  a wall, same caution D21 used).")
    if first_empty is not None:
        print(f"\n  first empty/error offset tested: {first_empty}h -- do not assume this IS")
        print(f"  the wall until confirmed by testing offsets just below and above it, the")
        print(f"  same bisection D21 did for DEXTradeByTokens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
