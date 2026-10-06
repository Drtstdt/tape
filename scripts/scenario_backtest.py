#!/usr/bin/env python3
"""D73/D74/D75/D76/D79 -- naive exit-rule sweep on REAL historical swap
tapes, not a model.

D79: every token's swaps now go through tape/sanity.py::filter_implausible_swaps()
before anything else -- D78 found real evidence of non-trade transactions
(fee claims, dust transfers) getting miscounted as trades by
tape/sources/helius.py, producing implied prices 1,000x-30,000x their
neighbors. This filter lives here and in token_dna_report.py ONLY, by the
user's explicit choice -- NOT in tape/bars.py or information_audit.py's
build_one(), which stay untouched (see tape/sanity.py's docstring for why).

The user asked, directly: "let the bot actually try investing, play out every
scenario -- one sells 50%, one sells 10%, one holds, one sells 100% -- and show
me which one makes the best progress and works on most tokens." D73 first
built this on fixed WALL-CLOCK checkpoints; D74 replaced that with a PRICE
MOVE trigger per user correction. D75 (this version) fixes a framing bug
found in D74's own first real run: the "conditional on firing" table answers
a near-tautological question at high thresholds (restricting to tokens that
ALREADY moved e.g. +200% before scoring "sell when it moves +200%" mostly
just re-measures the move you conditioned on), which is NOT the question the
user actually asked -- "which scenario, run as an actual rule, does best" has
to also count what happens on the tokens that never reach a given threshold
(where the rule does nothing and the token just behaves like hold).

  - Every scenario buys a symbolic 1.0 SOL of a token at its FIRST observed
    swap (the earliest possible entry -- no cherry-picked entry timing).
  - The trigger is the FIRST swap where price has moved at least
    +/-threshold from entry, in EITHER direction (a pump or a dump both count
    as "something happened" -- this only tests whether reacting to movement
    at all helps, not whether the move happened to be up). At that swap the
    scenario sells a fixed FRACTION of the position at the real observed
    price, and holds whatever remains to a fixed HORIZON, marked at the real
    observed price nearest the horizon (same "mark at last close in window,
    else at tape end" convention as tape/labels.py's triple_barrier).
  - (move_threshold, sell_fraction) pairs are a small PRE-REGISTERED grid --
    fixed in this file BEFORE any result was looked at, never tuned to what
    looked good (same discipline as D18 / Store.mints()'s docstring).
    "hold" (sell_fraction=0) needs no trigger and is the baseline every other
    scenario is compared against.
  - TWO tables are reported per threshold, and they answer different
    questions:
      DEPLOYABLE (primary): every one of the n_used tokens, using the fired
      scenario value when the threshold fired and hold_pnl_pct when it never
      did -- this is what actually running the rule would have returned,
      unconditionally, and is what "best scenario" should be read from.
      CONDITIONAL-ON-FIRING (diagnostic only): restricted to tokens where the
      threshold fired, kept because it is useful for understanding MECHANISM
      (how good is the exit once triggered) but explicitly flagged as
      trending tautological at high thresholds and NOT the number to rank
      scenarios by.
  - A pool-depth diagnostic (median quote_reserve_after on the entry swap) is
    printed once, to directly test rather than assume whether "first observed
    swap" tends to land in a near-empty pool -- the D69 mechanical-pump
    hypothesis for why moves this large happen in 0-5 seconds. (D76: Helius's
    `historical()` does not populate this field at all here -- 0/65 tokens on
    the first real run -- so this check currently reports "can't check"
    rather than a false negative.)
  - D76: an OVERSHOOT diagnostic per threshold, because on-chain swaps are
    discrete, not continuous. The first swap that crosses a threshold can
    land far past it (e.g. from +50% straight to +900% in one trade on a
    near-empty pool) -- nobody could have been "filled" near the nominal
    threshold in that case, so a huge scenario return there is a statement
    about discrete price jumps, not about a sell rule working. Reported per
    threshold: median/p90 overshoot past nominal, and the median swap-index
    the trigger fired at (a low index means it fired within the tape's first
    few trades, before anything resembling real price discovery).

This is NOT information_audit.py's Stage 1 gate. It fits nothing and claims
no predictive edge -- it just replays fixed, dumb, mechanical rules against
real realized prices and reports what actually would have happened. Two
honesty guards still apply, because "just descriptive" is exactly where
p-hacking hides:

  1. Comparing scenario vs hold on the SAME token, paired
     (tape/cv.py::paired_token_bootstrap) -- comparing two level means across
     different exit policies is the wrong statistic (cv.py's own docstring:
     it is what produced v3's false "time_stop is the whole deficit" reading).
  2. The grid has len(SELL_FRACTIONS) * len(MOVE_THRESHOLDS) DEPLOYABLE cells,
     each compared to hold -- multiple comparisons, same problem
     information_audit.py built a permutation null for. A full permutation
     null is overkill for a continuous-outcome exploratory sweep, so this
     script uses the cheaper, standard Bonferroni correction instead
     (alpha / n_cells) on the single best-looking DEPLOYABLE cell.

Sample caveat (still true, D56/D72): the backfilled universe today is still
essentially one continuous calendar burst, not spread across real days.
Whatever "wins" here is a fair readout of THIS sample and nothing has been
shown yet about whether it holds on a different day.

    python scripts/scenario_backtest.py --data data --real-creation-file data/real_creation_times.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from tape.cv import paired_token_bootstrap, token_bootstrap
from tape.sanity import filter_implausible_swaps
from tape.store import Store

# Same script directory is on sys.path[0] automatically (python adds the
# running script's own directory) -- this reuses information_audit.py's
# universe-selection logic verbatim rather than re-deriving it, so any future
# fix there (real-creation filtering, swap-count floor) applies here too.
from information_audit import (  # noqa: E402
    MIN_SWAPS_PER_TOKEN,
    chronological_universe,
    filter_by_real_creation,
    load_real_creation_cache,
)

# ---------------------------------------------------------------------------
# THE GRID -- fixed here, before any run of this script. Do not tune these to
# whatever cell looked best on a run; that is exactly the mistake this whole
# project's discipline exists to prevent (D18).
# ---------------------------------------------------------------------------
MOVE_THRESHOLDS: Tuple[float, ...] = (0.05, 0.10, 0.25, 0.50, 1.00, 2.00)
SELL_FRACTIONS: Dict[str, float] = {
    "sell_10": 0.10,
    "sell_50": 0.50,
    "sell_100": 1.00,
}
MIN_TOKENS = 10  # below this, CIs are too wide to say anything -- refuse, like audit's collecting_data


def simulate_one(swaps, horizon_s: float) -> Optional[Dict]:
    """One token's real swap tape -> per-move-threshold pnl_pct for every
    scenario, plus the threshold-independent 'hold' baseline. Returns None if
    the tape is too short to say anything (fewer than 2 swaps -- no price
    ever moved).

    Entry price/time = the FIRST swap in the tape (earliest possible entry,
    matches build_one()'s bar-0 convention in information_audit.py). All
    prices used are REAL observed swap prices; nothing here modifies price on
    our own hypothetical trade (no slippage from our own volume -- the same
    simplification scripts/live_paper_monitor.py already states and uses,
    and the weakest part of this whole exercise when the entry pool is thin
    -- see entry_reserve below).

    For each threshold, the trigger is the FIRST swap (strictly after entry,
    at or before the horizon deadline) whose |price/entry - 1| >= threshold.
    A threshold that never fires before the horizon is simply absent from the
    returned dict's "moves" -- never imputed as 0 or as hold (main() is what
    turns that absence into the DEPLOYABLE table's "behaves like hold").
    """
    if len(swaps) < 2:
        return None
    entry_ts = swaps[0].ts_ms
    entry_price = swaps[0].price
    if entry_price <= 0:
        return None
    entry_reserve = swaps[0].quote_reserve_after  # None if the source never reported it

    horizon_deadline = entry_ts + int(horizon_s * 1000)
    horizon_price = entry_price
    horizon_truncated = True
    for s in swaps:
        if s.ts_ms > horizon_deadline:
            horizon_truncated = False
            break
        horizon_price = s.price
    hold_pnl_pct = horizon_price / entry_price - 1.0

    moves: Dict[float, Dict] = {}
    for thr in MOVE_THRESHOLDS:
        trigger_price = None
        trigger_ts = None
        trigger_index = None
        for idx, s in enumerate(swaps[1:], start=1):
            if s.ts_ms > horizon_deadline:
                break
            if abs(s.price / entry_price - 1.0) >= thr:
                trigger_price = s.price
                trigger_ts = s.ts_ms
                trigger_index = idx
                break
        if trigger_price is None:
            continue  # never moved this much before the horizon -- not fired
        scenarios = {}
        for name, frac in SELL_FRACTIONS.items():
            sold_sol = frac * (trigger_price / entry_price)
            remaining_sol = (1.0 - frac) * (horizon_price / entry_price)
            scenarios[name] = sold_sol + remaining_sol - 1.0
        actual_move = abs(trigger_price / entry_price - 1.0)
        moves[thr] = {
            "scenarios": scenarios,
            "seconds_to_trigger": (trigger_ts - entry_ts) / 1000.0,
            # D76: swaps are discrete, not continuous -- the first swap that
            # crosses `thr` can land well past it (e.g. jump from +50% to
            # +900% in one trade on a near-empty pool). `overshoot` measures
            # exactly how far past nominal the ACTUAL fill would have been,
            # so a huge scenario return can be told apart from "the rule
            # worked" versus "the trigger never could have been filled near
            # its nominal price in the first place".
            "overshoot": actual_move - thr,
            "trigger_swap_index": trigger_index,  # how many swaps into the tape
        }

    return {
        "hold_pnl_pct": hold_pnl_pct,
        "truncated": horizon_truncated,
        "moves": moves,
        "entry_reserve": entry_reserve,
    }


def _stats(values: np.ndarray, groups: np.ndarray, n_boot: int, seed: int) -> Dict:
    lo, hi = token_bootstrap(values, groups, n_boot=n_boot, seed=seed)
    return {
        "n": len(values),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "win_rate": float((values > 0).mean()),
        "ci_lo": lo,
        "ci_hi": hi,
    }


def _bonferroni_ci(diff: np.ndarray, groups: np.ndarray, alpha: float, n_boot: int, seed: int) -> Tuple[float, float]:
    """Same resampling as tape/cv.py::token_bootstrap, at an arbitrary
    corrected percentile instead of the hardcoded 95% -- that helper's
    signature has no room for a custom alpha, so this is a thin, deliberately
    separate function rather than a hidden alpha kwarg jammed into it."""
    rng = np.random.default_rng(seed)
    uniq = np.unique(groups)
    by_group = {g: diff[groups == g] for g in uniq}
    means = np.empty(n_boot)
    for b in range(n_boot):
        picked = rng.choice(uniq, size=len(uniq), replace=True)
        means[b] = np.concatenate([by_group[g] for g in picked]).mean()
    lo = float(np.percentile(means, 100 * alpha / 2))
    hi = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return lo, hi


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--horizon-min", type=float, default=30.0,
                     help="matches information_audit.py's default")
    ap.add_argument("--real-creation-file", default=None)
    ap.add_argument("--max-observed-age-hours", type=float, default=6.0)
    ap.add_argument("--no-age-filter", action="store_true")
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    store = Store(args.data)

    # -- Universe: identical logic/order to information_audit.py -----------
    universe = chronological_universe(store, args.since, args.until)
    requested = len(universe)
    eligible_by_swaps = [(m, ts) for m, ts, n in universe if n >= MIN_SWAPS_PER_TOKEN]

    real_creation_path = Path(args.real_creation_file) if args.real_creation_file \
        else Path(args.data) / "real_creation_times.json"
    cache = None
    if not args.no_age_filter and real_creation_path.exists():
        try:
            cache = load_real_creation_cache(real_creation_path)
        except Exception as e:
            print(f"  (could not read {real_creation_path}: {e} -- age filter disabled)")

    excluded_too_old = excluded_no_real_creation = 0
    if cache is not None:
        eligible, excluded_too_old, excluded_no_real_creation = filter_by_real_creation(
            eligible_by_swaps, cache, args.max_observed_age_hours)
    else:
        eligible = eligible_by_swaps

    limited = eligible[: args.limit]
    mints = [m for m, _ in limited]

    print("=" * 78)
    print("UNIVERSE  (same selection logic as information_audit.py)")
    print("=" * 78)
    print(f"  requested (any swap): {requested}")
    print(f"  eligible-by-swaps (>= {MIN_SWAPS_PER_TOKEN} swaps): {len(eligible_by_swaps)}")
    if cache is not None:
        print(f"  excluded (>{args.max_observed_age_hours:.1f}h old at first-seen): {excluded_too_old}")
        print(f"  excluded (no real-creation-time data): {excluded_no_real_creation}")
    else:
        print("  NOTE: age filter DISABLED -- no real-creation-time cache found.")
    print(f"  considered: {len(mints)}")

    if len(mints) < MIN_TOKENS:
        print(f"\nstatus=collecting_data  eligible_mints={len(mints)}  (need >= {MIN_TOKENS})")
        return 2

    # -- Simulate every token, keyed by mint (needed to build the DEPLOYABLE
    #    table's "behaves like hold when not fired" logic below) -----------
    horizon_s = args.horizon_min * 60.0
    results: Dict[str, Dict] = {}
    n_too_short = 0
    n_truncated = 0
    n_swaps_dropped = 0
    n_tokens_with_drops = 0

    for mint in mints:
        swaps = list(store.iter_swaps(mint))
        if len(swaps) < MIN_SWAPS_PER_TOKEN:
            continue
        # D78/D79: drop swaps whose price is a >50x local outlier -- almost
        # certainly a non-trade transaction (fee claim, dust transfer, LP op)
        # miscounted as a trade by tape/sources/helius.py's balance-delta
        # approach (its own docstring names this exact limitation), not a
        # real bonding-curve move. See tape/sanity.py's docstring for why
        # this filter lives here and NOT in tape/bars.py / information_audit.py.
        swaps, implausible = filter_implausible_swaps(swaps)
        if implausible:
            n_swaps_dropped += len(implausible)
            n_tokens_with_drops += 1
        if len(swaps) < MIN_SWAPS_PER_TOKEN:
            continue
        sim = simulate_one(swaps, horizon_s)
        if sim is None:
            n_too_short += 1
            continue
        if sim["truncated"]:
            n_truncated += 1
        results[mint] = sim

    n_used = len(results)
    print(f"  used (simulated): {n_used}   too-short-to-simulate: {n_too_short}   "
          f"horizon-truncated (tape ended before {args.horizon_min:.0f}min): {n_truncated}")
    print(f"  price-sanity filter (D78/D79): dropped {n_swaps_dropped} implausible swap(s) "
          f"across {n_tokens_with_drops} token(s) -- see tape/sanity.py")
    if n_used < MIN_TOKENS:
        print(f"\nstatus=collecting_data  usable_tokens={n_used}  (need >= {MIN_TOKENS})")
        return 2

    reserves = [r["entry_reserve"] for r in results.values() if r["entry_reserve"] is not None]
    print(f"  entry-swap pool depth (quote_reserve_after) reported for {len(reserves)}/{n_used} tokens"
          + (f"; median={np.median(reserves):.4g}  p10={np.percentile(reserves, 10):.4g}  "
             f"p90={np.percentile(reserves, 90):.4g}" if reserves else " -- source never reports it, can't check"))
    print("  (checks whether the FIRST observed swap tends to land in a near-empty pool -- the")
    print("   D69 mechanical-pump hypothesis for why large moves can happen in 0-5 seconds)")

    all_mints = sorted(results.keys())
    hold_arr = np.array([results[m]["hold_pnl_pct"] for m in all_mints])
    hold_groups_arr = np.array(all_mints)
    hold_stats = _stats(hold_arr, hold_groups_arr, args.n_boot, args.seed)

    print("\n" + "=" * 78)
    print("BASELINE -- hold (never sell before horizon); threshold-independent by construction")
    print("=" * 78)
    print(f"  n={hold_stats['n']:5d}  mean={hold_stats['mean']:+.4f}  median={hold_stats['median']:+.4f}  "
          f"win_rate={hold_stats['win_rate']:.3f}  95% CI (token bootstrap)=[{hold_stats['ci_lo']:+.4f}, {hold_stats['ci_hi']:+.4f}]")
    print("  NOTE: mean vs median diverging hard is normal here (a few huge pumps, most tokens")
    print("  bleed out) -- read win_rate/median as the honest center, mean as tail-driven.")

    print("\n" + "=" * 78)
    print("DEPLOYABLE  (what running this exact rule on EVERY token would have returned: fired")
    print("value if the threshold fired before horizon, hold_pnl_pct if it never did -- THIS is")
    print("the table 'best scenario' should be read from, not the conditional one below)")
    print("=" * 78)

    deploy_cells = []  # (thr, name, stats, paired_lo, paired_hi, excludes_zero, paired_mean)
    for thr in MOVE_THRESHOLDS:
        fired_mints = [m for m in all_mints if thr in results[m]["moves"]]
        fired_fraction = len(fired_mints) / n_used
        secs = [results[m]["moves"][thr]["seconds_to_trigger"] for m in fired_mints]
        overshoots = [results[m]["moves"][thr]["overshoot"] for m in fired_mints]
        trigger_idxs = [results[m]["moves"][thr]["trigger_swap_index"] for m in fired_mints]
        print(f"\nmove_threshold=+/-{thr:>5.0%}  fired_fraction={fired_fraction:.1%} "
              f"({len(fired_mints)}/{n_used})"
              + (f"  median_seconds_to_trigger={np.median(secs):.0f}s" if secs else ""))
        if overshoots:
            print(f"  median_overshoot_past_nominal=+{np.median(overshoots):.0%}pt  "
                  f"p90_overshoot=+{np.percentile(overshoots, 90):.0%}pt  "
                  f"median_swap_#_at_trigger={np.median(trigger_idxs):.0f}  "
                  f"(swaps are discrete -- a big overshoot means the 'fill' at this threshold")
            print(f"  could never have happened near its nominal price; a low swap-# means it")
            print(f"  fired within the first few trades of the tape, not after real price discovery)")
        print(f"  {'scenario':10s} {'mean':>9s} {'median':>9s} {'win_rate':>9s} {'95% CI':>20s}   "
              f"{'vs hold (paired)':>20s}  {'excl.0':>7s}")
        for name in SELL_FRACTIONS:
            deploy_vals = np.array([
                results[m]["moves"][thr]["scenarios"][name] if thr in results[m]["moves"]
                else results[m]["hold_pnl_pct"]
                for m in all_mints
            ])
            st = _stats(deploy_vals, hold_groups_arr, args.n_boot, args.seed)
            diff = deploy_vals - hold_arr  # 0 for every token where this threshold never fired
            plo, phi = paired_token_bootstrap(diff, hold_groups_arr, n_boot=args.n_boot, seed=args.seed)
            excludes_zero = (not np.isnan(plo)) and (not np.isnan(phi)) and (plo > 0 or phi < 0)
            print(f"  {name:10s} {st['mean']:+9.4f} {st['median']:+9.4f} {st['win_rate']:9.3f} "
                  f"[{st['ci_lo']:+7.4f},{st['ci_hi']:+7.4f}]   [{plo:+7.4f},{phi:+7.4f}]   {str(excludes_zero):>7s}")
            deploy_cells.append((thr, name, st, plo, phi, excludes_zero, float(diff.mean())))

    print("\n" + "=" * 78)
    print("CONDITIONAL ON FIRING (diagnostic only -- restricted to tokens where the threshold")
    print("actually fired. At high thresholds this trends TAUTOLOGICAL: conditioning on 'already")
    print("moved >=T%' before scoring 'sell when it moves >=T%' mostly re-measures the move you")
    print("conditioned on, especially with median trigger times this low. Useful for understanding")
    print("mechanism, NOT for ranking scenarios -- use the DEPLOYABLE table above for that.")
    print("=" * 78)
    for thr in MOVE_THRESHOLDS:
        fired_mints = [m for m in all_mints if thr in results[m]["moves"]]
        if len(fired_mints) < MIN_TOKENS:
            continue
        g = np.array(fired_mints)
        hold_for_pairing = np.array([results[m]["hold_pnl_pct"] for m in fired_mints])
        print(f"\nmove_threshold=+/-{thr:>5.0%}  (n={len(fired_mints)} fired tokens)")
        for name in SELL_FRACTIONS:
            arr = np.array([results[m]["moves"][thr]["scenarios"][name] for m in fired_mints])
            st = _stats(arr, g, args.n_boot, args.seed)
            diff = arr - hold_for_pairing
            plo, phi = paired_token_bootstrap(diff, g, n_boot=args.n_boot, seed=args.seed)
            print(f"  {name:10s} mean={st['mean']:+9.4f}  median={st['median']:+9.4f}  "
                  f"win_rate={st['win_rate']:.3f}  vs hold (paired) 95% CI=[{plo:+.4f},{phi:+.4f}]")

    if not deploy_cells:
        print("\nstatus=collecting_data  no move threshold had enough evaluable tokens")
        return 2

    # -- Best cell + multiple-testing correction, on the DEPLOYABLE table ---
    n_cells = len(deploy_cells)
    alpha_corrected = 0.05 / n_cells
    ci_pct_corrected = 100 * (1 - alpha_corrected)
    best = max(deploy_cells, key=lambda c: c[6])  # rank by paired mean diff vs hold
    best_thr, best_name, best_st, best_plo, best_phi, best_excl, best_diff = best

    print("\n" + "=" * 78)
    print("SUMMARY  (ranked on the DEPLOYABLE table)")
    print("=" * 78)
    by_mean = max(deploy_cells, key=lambda c: c[2]["mean"])
    by_win = max(deploy_cells, key=lambda c: c[2]["win_rate"])
    print(f"  best by raw mean return:      {by_mean[1]:10s} @ move=+/-{by_mean[0]:.0%}  "
          f"mean={by_mean[2]['mean']:+.4f}  win_rate={by_mean[2]['win_rate']:.3f}  n={by_mean[2]['n']}")
    print(f"  best by win_rate:              {by_win[1]:10s} @ move=+/-{by_win[0]:.0%}  "
          f"mean={by_win[2]['mean']:+.4f}  win_rate={by_win[2]['win_rate']:.3f}  n={by_win[2]['n']}")
    print(f"  best by paired edge vs hold:   {best_name:10s} @ move=+/-{best_thr:.0%}  "
          f"paired_mean={best_diff:+.4f}  95% CI=[{best_plo:+.4f},{best_phi:+.4f}]  excludes 0: {best_excl}")

    print(f"\n  MULTIPLE-TESTING NOTE: {n_cells} (scenario x move-threshold) DEPLOYABLE cells were "
          f"each compared to hold. At a plain 95% CI, roughly {0.05 * n_cells:.1f} of them would be "
          f"expected to exclude 0 by CHANCE ALONE even if no scenario ever beat hold. Re-checking "
          f"the single best cell above at the Bonferroni-corrected {ci_pct_corrected:.1f}% level:")
    deploy_vals_best = np.array([
        results[m]["moves"][best_thr]["scenarios"][best_name] if best_thr in results[m]["moves"]
        else results[m]["hold_pnl_pct"]
        for m in all_mints
    ])
    diff_best = deploy_vals_best - hold_arr
    lo_c, hi_c = _bonferroni_ci(diff_best, hold_groups_arr, alpha_corrected,
                                 n_boot=max(args.n_boot, 4000), seed=args.seed)
    excl_c = lo_c > 0 or hi_c < 0
    print(f"    corrected {ci_pct_corrected:.1f}% CI = [{lo_c:+.4f}, {hi_c:+.4f}]   excludes 0: {excl_c}")

    print("\n" + "=" * 78)
    if best_excl and excl_c:
        print("VERDICT: on this sample, the best scenario's edge over hold survives the")
        print("  Bonferroni correction for sweeping this whole grid, evaluated the DEPLOYABLE")
        print("  (unconditional) way. This is still ONLY a realized-P&L readout on one largely")
        print("  single-day sample (D56/D72) with no slippage/impact modeling for our own trade")
        print("  -- and if median_seconds_to_trigger above is in the low single digits, treat")
        print("  this as a statement about bonding-curve launch mechanics (D69), not a strategy")
        print("  a human or even most bots could actually execute -- NOT a validated strategy.")
    else:
        print("VERDICT: no scenario's edge over hold survives correction for testing this")
        print(f"  many cells ({n_cells}), evaluated the DEPLOYABLE (unconditional) way. The")
        print("  uncorrected numbers above are real and honest, but at this sample size they do")
        print("  not distinguish these four dumb rules from each other or from chance. More")
        print("  tokens (wider calendar spread, per the standing discover/backfill loop) is what")
        print("  would change that -- not a bigger grid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
