#!/usr/bin/env python3
"""PROOF OF CONCEPT, not production code -- same status as poc_idl_classify.py.

Question this answers on REAL already-backfilled data, not a guess or an
outside paper: given THIS project's own barrier definition (upper_multiple/
lower_pct), how fast do winning (UP) labels actually resolve, and how does
the usable/base-rate picture change across candidate horizons?

Context: the 2026-09-22 information_audit.py run on --since 2026-09-01 came
back no_edge_found with only 94 usable tokens, and the permutation null's own
noise ceiling (mean max|AUC-0.5|=0.158) was bigger than anything observed --
underpowered, not necessarily "no signal". Public research on pump.fun
(CoinGecko's lifespan study; the arXiv "Predicting the success of new
crypto-tokens" paper, Sept-Oct 2025 data) reports a MEDIAN TIME-TO-GRADUATION
of ~4.4 minutes for tokens that succeed at all, and ~80% of all tokens dying
within a day. That is a different outcome (bonding-curve graduation at ~85
SOL) from this project's price-multiple barrier, and a different sample
period, so it is context, not a number to import blindly (this project's own
"never assume, verify against real evidence" discipline) -- this script
checks whether the SAME fast-resolution pattern shows up in this project's
own labels before anyone changes --horizon-min on the strength of a paper
about someone else's dataset.

    python scripts/poc_horizon_diagnostics.py --data data --since 2026-09-01
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from tape.bars import BarBuilder, band_bar_threshold
from tape.features import TokenState
from tape.labels import BarrierConfig, label_series, UP, DOWN, TIMEOUT
from tape.store import Store

_spec = importlib.util.spec_from_file_location(
    "information_audit", Path(__file__).resolve().parent / "information_audit.py")
_ia = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ia)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--sample", type=int, default=200, help="max tokens to replay")
    ap.add_argument("--upper", type=float, default=1.6)
    ap.add_argument("--lower", type=float, default=-0.30)
    ap.add_argument("--bar-fraction", type=float, default=0.01)
    ap.add_argument("--stride", type=int, default=5)
    args = ap.parse_args()

    store = Store(args.data)

    # -- 1. Full coverage, WITHOUT any --since filter, so a narrow date window
    #    is never mistaken for "that's all the data there is". --------------
    print("=" * 78)
    print("FULL STORE COVERAGE (no date filter)")
    print("=" * 78)
    cov = store.coverage()
    print(cov.to_string(index=False))
    total_mints_all_time = store.sql("SELECT count(DISTINCT mint) AS n FROM swaps")["n"][0]
    print(f"\ndistinct mints, entire store, no date filter: {total_mints_all_time}")

    # -- 2. The eligible universe for the window actually requested. --------
    universe = _ia.chronological_universe(store, args.since, args.until)
    eligible = [(m, ts) for m, ts, n in universe if n >= _ia.MIN_SWAPS_PER_TOKEN]
    print(f"eligible mints in [{args.since}, {args.until}] (>= "
          f"{_ia.MIN_SWAPS_PER_TOKEN} swaps): {len(eligible)} "
          f"(of {len(universe)} with any swap in range)")
    sample_mints = [m for m, _ in eligible[: args.sample]]
    if len(sample_mints) < 10:
        print("\ntoo few eligible tokens to say anything -- widen --since/--until first.")
        return 2

    # -- 3. Replay bars once per token; re-label at several horizons from the
    #    SAME bar sequence, so this isolates the horizon choice from any other
    #    variable (universe, band, bar construction all held fixed). --------
    candidate_horizons_min = [2, 5, 10, 15, 30, 60]
    cfg_by_h = {h: BarrierConfig(args.upper, args.lower, int(h * 60_000))
               for h in candidate_horizons_min}

    resolve_seconds_up = []   # how long an eventual UP took to resolve, at the LONGEST horizon
    per_horizon_counts = {h: {"UP": 0, "DOWN": 0, "TIMEOUT_or_truncated": 0} for h in candidate_horizons_min}

    n_tokens_used = 0
    for mint in sample_mints:
        swaps = list(store.iter_swaps(mint))
        if len(swaps) < _ia.MIN_SWAPS_PER_TOKEN:
            continue
        depth = next((s.quote_reserve_after for s in swaps if s.quote_reserve_after), None) or 1.0
        bb = BarBuilder("dollar", threshold=band_bar_threshold(depth, args.bar_fraction))
        st = TokenState(mint, created_ts_ms=swaps[0].ts_ms)
        h, l, c, t = [], [], [], []
        for s in swaps:
            bar = bb.push(s)
            if bar is None:
                continue
            st.update(bar)
            h.append(bar.high); l.append(bar.low); c.append(bar.close)
            t.append(bar.close_ts_ms)
        if len(c) < _ia.MIN_BARS_PER_TOKEN:
            continue
        n_tokens_used += 1

        for hmin, cfg in cfg_by_h.items():
            labels = label_series(h, l, c, t, cfg, mint=mint, stride=args.stride)
            for lab in labels:
                if lab.truncated:
                    per_horizon_counts[hmin]["TIMEOUT_or_truncated"] += 1
                    continue
                if lab.outcome == UP:
                    per_horizon_counts[hmin]["UP"] += 1
                elif lab.outcome == DOWN:
                    per_horizon_counts[hmin]["DOWN"] += 1
                else:
                    assert lab.outcome == TIMEOUT
                    per_horizon_counts[hmin]["TIMEOUT_or_truncated"] += 1
                if hmin == max(candidate_horizons_min) and lab.outcome == UP:
                    resolve_seconds_up.append((lab.t1_ms - lab.t0_ms) / 1000.0)

    print(f"\ntokens actually replayed: {n_tokens_used}")

    print("\n" + "=" * 78)
    print("OUTCOME MIX BY CANDIDATE HORIZON (same bars, same band, only horizon varies)")
    print("=" * 78)
    print(f"{'horizon_min':>11s} {'UP':>8s} {'DOWN':>8s} {'TIMEOUT/trunc':>14s} {'n':>8s} {'up_rate':>9s}")
    for hmin in candidate_horizons_min:
        c = per_horizon_counts[hmin]
        n = c["UP"] + c["DOWN"] + c["TIMEOUT_or_truncated"]
        up_rate = c["UP"] / n if n else float("nan")
        print(f"{hmin:11.0f} {c['UP']:8d} {c['DOWN']:8d} {c['TIMEOUT_or_truncated']:14d} {n:8d} {up_rate:9.4f}")

    print("\n" + "=" * 78)
    print(f"TIME-TO-RESOLVE for UP outcomes, at the {max(candidate_horizons_min):.0f}min horizon (seconds)")
    print("=" * 78)
    if resolve_seconds_up:
        arr = np.array(resolve_seconds_up)
        for p in (10, 25, 50, 75, 90, 99):
            print(f"  p{p:2d}: {np.percentile(arr, p):8.1f}s")
        print(f"  n UP resolutions: {len(arr)}")
        frac_under_5min = float((arr <= 300).mean())
        print(f"  fraction of UP resolutions that happened within 5 minutes: {frac_under_5min:.3f}")
    else:
        print("  no UP outcomes at all in this sample -- see the outcome-mix table above.")

    print("\nInterpretation: if `up_rate` barely changes past some horizon and most UP")
    print("resolutions happen well before it, later minutes mostly ADD timeouts/truncation")
    print("without adding wins -- a shorter --horizon-min for information_audit.py would raise")
    print("the base rate and the usable-row count without changing what's actually being found.")
    print("If up_rate keeps climbing all the way to 60min, the 30min default is short, not long.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
