"""D39 (2026-09-22, docs/DECISIONS.md) -- RUN THIS FIRST, before spending any
real archive quota on a bulk backfill or discover() run. The `archive`
add-on is now paid for ($100/mo, user-confirmed), but nobody has run a real
query with `dataset: archive` against `DEXTradeByTokens` in this project.
Two separate things need checking, and neither can be assumed from
anything already verified:

  1. **Does `archive` actually reach further back than `realtime`'s ~9-12h
     (D21), and how far?** D26-D30's "archive reaches ~26+ days" evidence
     was on `Trading.Trades`, a DIFFERENT cube D30 separately proved does
     NOT cover pump.fun/PumpSwap at all -- that evidence says nothing about
     `DEXTradeByTokens` specifically.
  2. **Does `discover()`'s `Trade.Dex.ProgramAddress` where-filter actually
     work?** `ProgramAddress` has only ever been read as an OUTPUT field
     (D19-23) -- using it in a `where` clause is new and unverified.

Same bisection method D21 already used and trusts (`bitquery_smoketest.py
--bisect`), pointed at `dataset: archive` instead of `realtime`, plus a
small real discover() call. Uses a mint this project already has deep,
independently-verified ground truth for (Helius, D34) so a real trade
count can be sanity-checked, not just "did it error".

    python -m tape.scripts.bitquery_archive_smoketest \\
        --mint 3tUn8dGmDceJ9pCwFLUVo2jkD6FLWaZ78YEpAa3bpump \\
        --days-ago 1,3,7,14,30,60,90
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from ..sources.bitquery import DATASET_ARCHIVE, BitquerySource


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mint", required=True,
                     help="a mint this project already has independent ground truth "
                          "for (a v3 tape, or a Helius-verified pull, D34) -- so a "
                          "nonzero/zero trade count here can be sanity-checked, not "
                          "just observed")
    ap.add_argument("--days-ago", default="1,3,7,14,30,60,90",
                     help="comma-separated list of day offsets to bisect, each as a "
                          "1-hour window ending that many days ago (default matches "
                          "D21's --bisect style: enough points to find the boundary "
                          "without guessing it)")
    ap.add_argument("--client-id", default=None, help="for OAuth refresh (D39) -- omit "
                                                        "to use the static BITQUERY_API_KEY as before")
    ap.add_argument("--client-secret", default=None)
    ap.add_argument("--skip-discover", action="store_true",
                     help="skip the discover() program-id-filter check (part 2 above)")
    args = ap.parse_args(argv)

    try:
        source = BitquerySource(dataset=DATASET_ARCHIVE,
                                 client_id=args.client_id, client_secret=args.client_secret)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    print("=" * 70)
    print("PART 1: how far back does `dataset: archive` actually reach on "
          "DEXTradeByTokens, for a mint we can sanity-check?\n")
    import time as _time
    now_ms = int(_time.time() * 1000)
    offsets: List[int] = [int(x.strip()) for x in args.days_ago.split(",") if x.strip()]
    any_found = False
    for days in sorted(offsets, reverse=True):
        window_end = now_ms - days * 24 * 3600 * 1000
        window_start = window_end - 3600 * 1000  # 1h window, same as D21's --bisect
        try:
            swaps = list(source.historical(args.mint, window_start, window_end))
        except RuntimeError as e:
            print(f"  {days:>4}d ago: ERROR -- {e}")
            continue
        marker = "<-- has data" if swaps else ""
        print(f"  {days:>4}d ago: {len(swaps):>5} trades in a 1h window {marker}")
        any_found = any_found or bool(swaps)

    if not any_found:
        print("\nNo trades found at ANY offset tested. Before concluding archive "
              "doesn't reach back at all: try a SMALLER --days-ago (this mint may "
              "simply not have traded in any of the windows tested), or a mint you "
              "know for certain traded heavily at a specific past date.")
    else:
        print("\nFind the exact boundary the same way D21 did: narrow --days-ago "
              "around the last offset that returned data and the first one that "
              "didn't, until you have a real number to write into docs/DECISIONS.md "
              "(the way D21 pinned realtime's ~9-12h boundary).")

    if not args.skip_discover:
        print("\n" + "=" * 70)
        print("PART 2: does discover()'s Trade.Dex.ProgramAddress where-filter "
              "actually work? (a small, cheap call -- 1h window, whatever `archive` "
              "can currently reach per Part 1 above)\n")
        # Use whatever the deepest offset that had data in Part 1 was, so this
        # isn't testing a window archive has already shown it can't reach.
        probe_end = now_ms - (min((d for d in offsets), default=1)) * 24 * 3600 * 1000
        probe_start = probe_end - 3600 * 1000
        try:
            found = list(source.discover(probe_start, probe_end))
        except RuntimeError as e:
            print(f"  discover() FAILED: {e}")
            print("  This means the ProgramAddress where-filter shape is wrong, or "
                  "this Bitquery account can't use it -- do not build "
                  "scripts/backfill_bitquery.py's real run on top of this until fixed.")
            return 1
        print(f"  {len(found)} distinct mint(s) discovered in this window.")
        for f in found[:10]:
            print(f"    mint={f['mint']}  pool={f['pool']}  first_seen_ts_ms={f['first_seen_ts_ms']}")
        if not found:
            print("  Zero mints found -- could mean no pump.fun/PumpSwap activity in "
                  "this specific 1h window (try a busier one), or that the filter "
                  "silently matches nothing. Cross-check: does --mint above show up "
                  "if you widen the window to include one of its own known trade times?")

    print("\n" + "=" * 70)
    print("Write whatever this found into docs/DECISIONS.md as D40 (or the next "
          "free number) before running scripts/backfill_bitquery.py for real -- "
          "the same discipline every other adapter in this project has followed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
