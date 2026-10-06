#!/usr/bin/env python3
"""D78 -- diagnose token_dna_report.py's astronomically large max_move_30m
values (e.g. +34,128,412,919.87 -- +3.4 TRILLION percent) with real evidence
instead of guessing.

That magnitude is far beyond anything pump.fun's bonding curve mechanics can
produce (D69's mechanical-pump finding is about launches moving fast and
hard, not about a token's price legitimately changing by ten orders of
magnitude) -- it smells like a data artifact in `tape/sources/helius.py`'s
`price = quote_amount / base_amount` computation, most likely on the very
first (entry) swap: `helius.py`'s own module docstring already lists "not
yet verified against a known-good tape" as an open item (points 1-2 of its
NOT YET VERIFIED section), and this is exactly the kind of edge case that
warning was for.

This script does NOT re-fetch anything from Helius -- it just prints the
raw, already-backfilled `CanonicalSwap` rows straight from the Store for the
worst-offender mints, earliest first, so the actual base_amount/quote_amount/
price/side/sig behind the anomaly is visible directly (D3: verify against
real evidence, don't guess). No new API calls, no new cost.

    python scripts/diag_price_anomaly.py --data data
    (defaults to the top 5 mints by max_move_30m in data/token_dna_report.csv,
    if that file exists; or pass specific mints with --mint, repeatable)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.sanity import filter_implausible_swaps
from tape.store import Store


def _worst_offenders(csv_path: Path, top: int) -> List[str]:
    import pandas as pd
    df = pd.read_csv(csv_path)
    return df.sort_values("max_move_30m", ascending=False).head(top)["mint"].tolist()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--mint", action="append", default=[],
                     help="specific mint(s) to inspect; repeatable. If omitted, "
                          "reads --from-dna-report (or <data>/token_dna_report.csv)")
    ap.add_argument("--from-dna-report", default=None,
                     help="path to token_dna_report.csv (default: <data>/token_dna_report.csv)")
    ap.add_argument("--top", type=int, default=5,
                     help="how many worst-offender mints to inspect, by max_move_30m")
    ap.add_argument("--n-swaps", type=int, default=10,
                     help="how many of the EARLIEST swaps to print per mint")
    args = ap.parse_args()

    mints = list(args.mint)
    if not mints:
        report_path = Path(args.from_dna_report) if args.from_dna_report \
            else Path(args.data) / "token_dna_report.csv"
        if report_path.exists():
            mints = _worst_offenders(report_path, args.top)
            print(f"(no --mint given -- using top {len(mints)} by max_move_30m from {report_path})")
        else:
            print(f"No --mint given and {report_path} does not exist -- nothing to inspect. "
                  f"Run scripts/token_dna_report.py first, or pass --mint explicitly.",
                  file=sys.stderr)
            return 2

    store = Store(args.data)
    for mint in mints:
        swaps = list(store.iter_swaps(mint))
        print("\n" + "=" * 100)
        print(f"mint={mint}   total_swaps_in_store={len(swaps)}")
        print("=" * 100)
        if not swaps:
            print("  (no swaps in store)")
            continue
        entry_price = swaps[0].price
        print(f"  entry (swap #0): sig={swaps[0].sig}  ts_ms={swaps[0].ts_ms}  "
              f"side={swaps[0].side}  base_amount={swaps[0].base_amount!r}  "
              f"quote_amount={swaps[0].quote_amount!r}  price={swaps[0].price!r}  "
              f"venue={swaps[0].venue}  wallet={swaps[0].wallet}")
        # D78/D79: mark which of these swaps tape/sanity.py's price-plausibility
        # filter would drop, so the fix can be checked against the exact
        # evidence that motivated it, not just trusted on its own tests.
        _, implausible = filter_implausible_swaps(swaps)
        implausible_sigs = {s.sig for s in implausible}
        print(f"\n  {'#':>3s} {'ts_ms':>14s} {'side':5s} {'base_amount':>20s} {'quote_amount':>16s} "
              f"{'price':>20s} {'ret_vs_entry':>16s} {'filter':>10s}  sig")
        for i, s in enumerate(swaps[: args.n_swaps]):
            ret = (s.price / entry_price - 1.0) if entry_price else float("nan")
            flag = "DROPPED" if s.sig in implausible_sigs else ""
            print(f"  {i:3d} {s.ts_ms:14d} {s.side:5s} {s.base_amount:20.10g} {s.quote_amount:16.10g} "
                  f"{s.price:20.10g} {ret:+16.6g} {flag:>10s}  {s.sig}")
        if implausible:
            print(f"  -- tape/sanity.py would drop {len(implausible)}/{len(swaps)} swap(s) "
                  f"from this token's full tape as implausible (>50x local-median outlier)")
    print("\n" + "=" * 100)
    print("READ THIS FOR: is entry (#0)'s base_amount suspiciously tiny relative to its")
    print("quote_amount (that alone would make price = quote/base blow up)? Is entry's side/sig")
    print("consistent with a normal trade, or does it look like something else got miscounted")
    print("as a swap (e.g. an account-creation artifact)? Compare entry's price to swap #1, #2 --")
    print("a real bonding-curve launch should still show a SMOOTH, bounded progression, not a")
    print("multi-order-of-magnitude jump between adjacent swaps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
