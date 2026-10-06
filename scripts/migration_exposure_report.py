#!/usr/bin/env python3
"""D94 (docs/DECISIONS.md): read-only diagnostic, not a fix.

Question: does any mint in the Store show swaps on BOTH the pump.fun
bonding-curve program AND the PumpSwap (post-graduation) program -- and if
so, how early does that happen relative to this project's own 30-minute
decision/exit horizon?

Why this matters (verified against this project's own code, not a guess):

  - `Store.iter_swaps(mint)` has no venue filter at all -- it returns every
    swap for a mint in time order, pump.fun and PumpSwap alike.
  - `BarBuilder._open()` stamps a bar's `venue` from whichever swap opened
    it and never checks it again -- a bar, and the chronological bar
    sequence the whole pipeline (features, labels, the online policy) is
    built from, can silently straddle a venue change with no marker.
  - `scripts/poc_horizon_diagnostics.py` already cites public research
    (CoinGecko; the arXiv "Predicting the success of new crypto-tokens"
    paper) reporting a MEDIAN TIME-TO-GRADUATION of ~4.4 minutes for tokens
    that succeed at all -- well inside the 30-minute horizon this project
    labels and trades on. If that holds for this project's own tokens too,
    migration-mid-window would be the common case for winners, not a rare
    edge case.

What this script does NOT do: it does not change bars.py, features.py, or
any model/policy code. It only measures how big the problem actually is on
real data, per this project's "verify before you fix" discipline (D3) --
decide whether a fix is even worth building before writing one.

Usage:
    python scripts/migration_exposure_report.py --data data
    python scripts/migration_exposure_report.py --data data --horizon-min 30 --verbose
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.sources.helius import PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM
from tape.store import Store


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--horizon-min", type=float, default=30.0,
                     help="decision/exit horizon in minutes (default matches "
                          "this project's triple_barrier horizon)")
    ap.add_argument("--sample", type=int, default=0,
                     help="cap on number of mints scanned, 0 = all")
    ap.add_argument("--verbose", action="store_true",
                     help="print one line per mint that shows a migration")
    args = ap.parse_args()

    horizon_ms = args.horizon_min * 60_000.0
    store = Store(args.data)
    mints = store.mints()
    if not mints:
        print("status=collecting_data -- store has no swaps yet")
        return 0
    if args.sample:
        mints = mints[: args.sample]

    n_mints = 0
    n_with_pumpfun = 0
    n_with_migration = 0
    n_migration_within_horizon = 0
    n_migration_with_zero_prior_pumpfun_swaps = 0
    ages_at_migration_ms = []

    for mint in mints:
        n_mints += 1
        first_ts = None
        pumpfun_seen = False
        migration_ts = None
        n_pure_pumpfun_swaps_before_migration = 0
        for swap in store.iter_swaps(mint):
            if first_ts is None:
                first_ts = swap.ts_ms
            touches_pumpfun = PUMPFUN_PROGRAM in swap.venue
            touches_pumpswap = PUMPSWAP_PROGRAM in swap.venue
            if touches_pumpswap and migration_ts is None:
                migration_ts = swap.ts_ms
            if touches_pumpfun:
                pumpfun_seen = True
                if migration_ts is None:
                    # a PURE pump.fun swap (not the mixed migration-tx venue)
                    # observed strictly before migration -- i.e. real,
                    # witnessed bonding-curve trading, not just a backfill
                    # gap that happens to start at the migration tx itself
                    if not touches_pumpswap:
                        n_pure_pumpfun_swaps_before_migration += 1
        if first_ts is None:
            continue
        if pumpfun_seen:
            n_with_pumpfun += 1
        if migration_ts is not None:
            n_with_migration += 1
            age_ms = migration_ts - first_ts
            ages_at_migration_ms.append(age_ms)
            within = age_ms <= horizon_ms
            if within:
                n_migration_within_horizon += 1
            zero_prior = n_pure_pumpfun_swaps_before_migration == 0
            if zero_prior:
                n_migration_with_zero_prior_pumpfun_swaps += 1
            if args.verbose:
                print(f"{mint}: pure_pumpfun_swaps_before_migration="
                      f"{n_pure_pumpfun_swaps_before_migration} "
                      f"migration_age_min={age_ms / 60_000:.2f} "
                      f"within_horizon={within} "
                      f"{'[SUSPECT: backfill-coverage gap, not witnessed migration]' if zero_prior else ''}")

    print()
    print("=== MIGRATION EXPOSURE REPORT (D94) ===")
    print(f"horizon checked: {args.horizon_min:.0f} min")
    print(f"mints scanned: {n_mints}")
    print(f"mints with any pump.fun bonding-curve swap: {n_with_pumpfun}")
    print(f"mints that ALSO show a PumpSwap swap (migrated): {n_with_migration}")
    if n_with_migration:
        pct_within = 100.0 * n_migration_within_horizon / n_with_migration
        print(f"  of those, migrated within the {args.horizon_min:.0f}-min "
              f"horizon: {n_migration_within_horizon} ({pct_within:.1f}%)")
        ages_sorted = sorted(ages_at_migration_ms)
        median_min = ages_sorted[len(ages_sorted) // 2] / 60_000
        print(f"  median age at migration: {median_min:.2f} min "
              f"(first-swap proxy, not true creation time -- see caveat below)")
        pct_zero_prior = 100.0 * n_migration_with_zero_prior_pumpfun_swaps / n_with_migration
        print(f"  of the migrated mints, {n_migration_with_zero_prior_pumpfun_swaps} "
              f"({pct_zero_prior:.1f}%) show ZERO pure pump.fun swaps before the "
              f"migration-touching swap -- that is NOT witnessed in-window "
              f"migration, it means the Store's first-ever observed trade for "
              f"that mint already touches PumpSwap, i.e. the real bonding-curve "
              f"history for that mint was never captured at all (a backfill "
              f"coverage gap, not evidence of a fast migration). Re-run with "
              f"--verbose and look for the [SUSPECT] tag to see which mints.")
    else:
        print("  no migrations observed in this corpus -- either none of these "
              "tokens graduated, or the corpus doesn't cover enough tokens that did")
    print()
    print("CAVEAT: 'age at migration' uses this mint's first OBSERVED swap in "
          "the Store as a creation-time proxy, not TokenMeta.created_ts_ms. If "
          "backfill started watching a mint after its real creation, ages here "
          "are underestimates -- cross-check against discover_pumpfun_launches.py's "
          "recorded creation times before treating the median above as precise.")
    print("This is a measurement only. No bars/features/policy code changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())