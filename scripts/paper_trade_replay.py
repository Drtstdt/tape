#!/usr/bin/env python3
"""D84 (docs/DECISIONS.md) -- Phase A of docs/PAPER_TRADING_PLAN.md: replay
the already-collected Store data chronologically through `OnlinePolicy`
(`tape/online_policy.py`), so the whole decide -> resolve -> learn loop can
be proven correct CHEAPLY and REPRODUCIBLY before ever touching live
infrastructure (`scripts/paper_trade_live.py`).

NOT expected to find an edge. The corpus is the same largely-single-day,
61-65-token population `information_audit.py` has already run against
three times (D70/D72/D83, verdict `no_edge_found` every time) -- far too
narrow to trust either a positive or a negative read here. This script's
job is to prove the MECHANICS work (decisions get made, outcomes resolve,
the model visibly updates, the ledger is honest) on data that's already on
disk, at zero API cost, before spending live monitoring time on it.

ONE DECISION PER TOKEN (not one per bar): mirrors `scripts/live_paper_monitor.py`'s
own bar0 convention. The policy is asked to decide at the FIRST bar where
`PaperRails` pass (skipping earlier, too-thin bars). If it abstains, that
token's episode ends there -- no repeated pulls on the same tape, which
would otherwise create the exact within-token serial correlation
`tape/labels.py::average_uniqueness` exists to warn about.

FULL-FEEDBACK LEARNING, NOT PARTIAL-FEEDBACK BANDIT (see
`tape/online_policy.py`'s module docstring for the reasoning): the true
outcome is observable on-chain whether or not a paper position was taken,
so `OnlinePolicy.update()` is called on EVERY resolved decision point,
entered or abstained. Only ENTERED decisions get a PnL row in the ledger.

A PARALLEL RANDOM BASELINE runs at the same time, on the same decision
points (same features, same rails) -- an `OnlinePolicy` instance pinned at
epsilon=1.0 (always explores, i.e. pure random action), logged separately
under `policy=random`. This is the control group `paper_trading_report.py`
needs to tell "the online policy did better" from "a random policy would
have looked the same on this small a sample" (docs/PAPER_TRADING_PLAN.md
Sec 6).

    python scripts/paper_trade_replay.py --data data
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.bars import BarBuilder, band_bar_threshold
from tape.costs import CostModel
from tape.features import TokenState
from tape.labels import DOWN, TIMEOUT, UP, BarrierConfig, triple_barrier
from tape.online_policy import OnlinePolicy, PaperRails, evaluate_paper_rails
from tape.sanity import filter_implausible_swaps, filter_post_migration_swaps
from tape.store import Store

from information_audit import (  # noqa: E402
    MIN_SWAPS_PER_TOKEN,
    chronological_universe,
    filter_by_real_creation,
    load_real_creation_cache,
)

LEDGER_COLUMNS = [
    "mode", "policy", "mint", "decision_ts_ms", "bar_index",
    "action", "explore", "p_raw", "epsilon_at_decision", "min_probability",
    "outcome", "truncated", "raw_pnl_pct", "cost_adjusted_pnl_pct",
    "n_bars_at_decision", "age_ms_at_decision", "model_n_decisions",
]

_OUTCOME_NAME = {UP: "UP", DOWN: "DOWN", TIMEOUT: "TIMEOUT"}


def _pnl_for_outcome(outcome: int, ret_at_horizon_pct: Optional[float],
                     cfg: BarrierConfig) -> float:
    """Same payoff convention `OnlinePolicy.breakeven_p` is derived from
    (Sec on tape/online_policy.py): UP realizes the upper barrier, DOWN the
    lower barrier -- both are EXITS at the barrier, matching triple_barrier's
    own semantics (label resolves the instant a barrier is touched). TIMEOUT
    realizes whatever the tape's actual return was at the horizon."""
    if outcome == UP:
        return cfg.upper_multiple - 1.0
    if outcome == DOWN:
        return cfg.lower_pct
    return ret_at_horizon_pct if ret_at_horizon_pct is not None else 0.0


def first_decision_bar_index(feats_by_bar: List[Dict[str, Optional[float]]],
                             rails: PaperRails) -> Optional[int]:
    """The first bar index whose features clear `PaperRails`, or None if the
    tape never does. Pulled out of `replay_one` as its own pure function so
    "which bar was the decision made on" is directly testable, independent
    of everything else that happens once a decision bar is found."""
    for i, feats in enumerate(feats_by_bar):
        if evaluate_paper_rails(feats, rails) is None:
            return i
    return None


def mints_already_decided(ledger_path: Path, mode: str = "replay") -> set:
    """D89 (docs/DECISIONS.md): mints this ledger already has a decision row
    for under the given `mode`, so a re-run of this script against a Store
    that's grown since the last run doesn't silently re-decide on the SAME
    mint twice -- would duplicate ledger rows and break the one-decision-
    per-token design (`first_decision_bar_index`'s whole point). Mirrors
    `backfill.py`/`backfill_discovered_launches.py`'s existing "skip what's
    already recorded" idempotency, applied here to decisions instead of
    Store rows. Returns an empty set if the ledger doesn't exist yet (a
    fresh corpus, nothing to skip)."""
    if not Path(ledger_path).exists():
        return set()
    decided: set = set()
    with open(ledger_path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("mode") == mode:
                decided.add(row["mint"])
    return decided


def prepare_decision(mint: str, swaps, cfg: BarrierConfig, bar_fraction: float,
                     rails: PaperRails, cost: CostModel, nominal_size: float,
                     reasons: Optional[Counter] = None) -> Optional[Dict]:
    """D117 (docs/DECISIONS.md): the POLICY-INDEPENDENT half of what
    `replay_one` always did -- bars, features, first rails-passing bar, the
    one triple-barrier label, PnL. Nothing here reads or mutates an
    `OnlinePolicy`, so it can run in worker processes for many mints at once
    (`scripts/paper_trade_replay_v2.py`); the policy's decide()/update() --
    the only inherently sequential part -- stays in `apply_decision`, run in
    chronological order by exactly one process. Returns None when the mint
    yields no decision (the reason is tallied in `reasons` exactly as
    before), else a plain picklable dict."""
    if len(swaps) < MIN_SWAPS_PER_TOKEN:
        if reasons is not None:
            reasons["too_few_swaps_after_sanity_filter"] += 1
        return None

    depth = next((s.quote_reserve_after for s in swaps if s.quote_reserve_after), None) or 1.0
    bb = BarBuilder("dollar", threshold=band_bar_threshold(depth, bar_fraction))
    st = TokenState(mint, created_ts_ms=swaps[0].ts_ms)
    h, l, c, t, feats_by_bar = [], [], [], [], []

    for s in swaps:
        bar = bb.push(s)
        if bar is None:
            continue
        st.update(bar)
        feats_by_bar.append(dict(st.features()))
        h.append(bar.high); l.append(bar.low); c.append(bar.close); t.append(bar.close_ts_ms)
        # Bars keep building past the decision point too -- triple_barrier
        # needs to see the tape AFTER the decision bar to resolve at all.

    if not feats_by_bar:
        if reasons is not None:
            reasons["no_bars_emitted"] += 1
        return None

    decision_bar_index = first_decision_bar_index(feats_by_bar, rails)
    if decision_bar_index is None:
        if reasons is not None:
            # The LAST bar is this token's best shot -- most bars/oldest age
            # it ever reached -- so whatever rail still blocks it there is
            # the one actually responsible, not whichever fired on bar 0.
            reason = evaluate_paper_rails(feats_by_bar[-1], rails)
            reasons[f"rail:{reason[0]}" if reason else "rail:unknown"] += 1
        return None  # rails never passed for this token -- no decision made
    decision_features = feats_by_bar[decision_bar_index]

    label = triple_barrier(h, l, c, t, decision_bar_index, cfg, mint=mint)
    if label.truncated:
        if reasons is not None:
            reasons["decision_found_but_tape_truncated"] += 1
        return None  # tape ended before the horizon resolved -- exclude, same as information_audit.py

    raw_pnl = _pnl_for_outcome(label.outcome, label.ret_at_horizon_pct, cfg)
    return {
        "decision_features": decision_features,
        "y": 1 if label.outcome == UP else 0,
        "outcome_name": _OUTCOME_NAME[label.outcome],
        "truncated": label.truncated,
        "raw_pnl": raw_pnl,
        "cost_adjusted_pnl": raw_pnl - cost.round_trip_pct(nominal_size),
        "decision_ts_ms": t[decision_bar_index],
        "bar_index": decision_bar_index,
    }


def apply_decision(prep: Dict, mint: str, online: OnlinePolicy,
                   random_baseline: OnlinePolicy, mode: str) -> List[Dict]:
    """D117: the SEQUENTIAL half -- both policies decide on the prepared
    decision point, then update (full feedback, see module docstring), and
    the two ledger rows are returned. Must be called once per mint in
    chronological order by a single process."""
    decision_features = prep["decision_features"]
    rows: List[Dict] = []
    for policy_name, policy in (("online", online), ("random", random_baseline)):
        decision = policy.decide(decision_features)
        policy.update(decision_features, prep["y"])  # full feedback -- see module docstring
        row = {
            "mode": mode, "policy": policy_name, "mint": mint,
            "decision_ts_ms": prep["decision_ts_ms"], "bar_index": prep["bar_index"],
            "action": decision["action"], "explore": decision["explore"],
            "p_raw": decision["p_raw"], "epsilon_at_decision": decision["epsilon_at_decision"],
            "min_probability": decision["min_probability"],
            "outcome": prep["outcome_name"], "truncated": prep["truncated"],
            "raw_pnl_pct": prep["raw_pnl"] if decision["action"] == "enter" else None,
            "cost_adjusted_pnl_pct": prep["cost_adjusted_pnl"] if decision["action"] == "enter" else None,
            "n_bars_at_decision": decision_features.get("n_bars"),
            "age_ms_at_decision": decision_features.get("age_ms"),
            "model_n_decisions": policy.n_decisions,
        }
        rows.append(row)
    return rows


def prepare_mint(mint: str, raw_swaps, cfg: BarrierConfig, bar_fraction: float,
                 rails: PaperRails, cost: CostModel, nominal_size: float,
                 reasons: Optional[Counter] = None) -> Optional[Dict]:
    """D117: everything `main()`'s loop did per mint BEFORE the policy --
    sanity filters (incl. D95's post-migration cut) + `prepare_decision`.
    Shared by the sequential `main()` and the parallel v2 script so the two
    cannot drift apart."""
    swaps, implausible = filter_implausible_swaps(list(raw_swaps))
    # D95 (docs/DECISIONS.md): drop everything from this mint's first
    # PumpSwap-touching swap onward BEFORE bars/features ever see it --
    # real evidence found the online policy entering trades priced off
    # post-migration data with no bonding-curve history behind it at
    # all, and losing on most of them. A mint with nothing left after
    # this cut is counted under its own reason, not silently merged into
    # "too_few_swaps_after_sanity_filter", per this project's "never
    # guess why a rail rejects everything; count it" rule (D85).
    swaps, post_migration = filter_post_migration_swaps(swaps)
    if post_migration and len(swaps) < MIN_SWAPS_PER_TOKEN:
        if reasons is not None:
            reasons["no_bonding_curve_swaps_pre_migration"] += 1
        return None
    return prepare_decision(mint, swaps, cfg, bar_fraction, rails, cost,
                            nominal_size, reasons)


def replay_one(mint: str, swaps, cfg: BarrierConfig, bar_fraction: float,
               rails: PaperRails, online: OnlinePolicy, random_baseline: OnlinePolicy,
               cost: CostModel, nominal_size: float, mode: str,
               reasons: Optional[Counter] = None) -> List[Dict]:
    """Build bars/features for one token, find the first rails-passing bar,
    get both policies' decisions there, resolve the outcome once, update
    both models (full feedback), and return 0-2 ledger rows (one per
    policy that ENTERED -- abstained decisions are logged too, at zero
    PnL rows, so the ledger's decision counts and the report's exploit-phase
    sample size are auditable, not just its winning trades).

    `reasons`, when given, is tallied with WHY a token contributed nothing
    -- added after a real 0/65-decisions run on the actual Store corpus
    (docs/DECISIONS.md D85) came back with no explanation otherwise. Never
    guess why a rail rejects everything; count it."""
    prep = prepare_decision(mint, swaps, cfg, bar_fraction, rails, cost,
                            nominal_size, reasons)
    if prep is None:
        return []
    return apply_decision(prep, mint, online, random_baseline, mode)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--real-creation-file", default=None)
    ap.add_argument("--max-observed-age-hours", type=float, default=6.0)
    ap.add_argument("--no-age-filter", action="store_true")
    # Same barrier defaults as information_audit.py / live_paper_monitor.py --
    # the fixed exit rule (Sec 3, docs/PAPER_TRADING_PLAN.md).
    ap.add_argument("--upper", type=float, default=1.6)
    ap.add_argument("--lower", type=float, default=-0.30)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--bar-fraction", type=float, default=0.01)
    ap.add_argument("--nominal-size", type=float, default=1.0,
                     help="paper position size, in the same units CostModel expects -- "
                          "purely nominal, no real capital, only affects the fixed-cost term")
    ap.add_argument("--epsilon-start", type=float, default=0.90)
    ap.add_argument("--epsilon-floor", type=float, default=0.15)
    ap.add_argument("--epsilon-decay-scale", type=float, default=60.0)
    ap.add_argument("--learning-rate", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model-out", default=None,
                     help="path to save the trained online policy's checkpoint "
                          "(default: <--data>/online_policy_state.json)")
    ap.add_argument("--model-in", default=None,
                     help="resume from an existing checkpoint instead of a fresh policy")
    ap.add_argument("--ledger-out", default=None,
                     help="path to append the decision ledger (default: <--data>/paper_trades.csv)")
    args = ap.parse_args()

    store = Store(args.data)
    cfg = BarrierConfig(args.upper, args.lower, int(args.horizon_min * 60_000))
    cost = CostModel()
    rails = PaperRails()

    universe = chronological_universe(store, args.since, args.until)
    requested = len(universe)
    eligible_by_swaps = [(m, ts) for m, ts, n in universe if n >= MIN_SWAPS_PER_TOKEN]

    real_creation_path = Path(args.real_creation_file) if args.real_creation_file \
        else Path(args.data) / "real_creation_times.json"
    excluded_too_old = excluded_no_real_creation = 0
    if not args.no_age_filter and real_creation_path.exists():
        cache = load_real_creation_cache(real_creation_path)
        eligible, excluded_too_old, excluded_no_real_creation = filter_by_real_creation(
            eligible_by_swaps, cache, args.max_observed_age_hours)
    else:
        eligible = eligible_by_swaps

    limited = eligible[: args.limit]
    all_mints = [m for m, _ in limited]

    # D89 (docs/DECISIONS.md): this script has no guard against re-deciding
    # on a mint it already logged a decision for in an earlier run -- a real
    # gap, since the corpus is meant to grow over time (widening the
    # universe via discover_pumpfun_launches.py / backfill_discovered_
    # launches.py) and re-running this script against a grown Store would
    # otherwise silently re-decide on the SAME already-decided mints too,
    # duplicating ledger rows and breaking the one-decision-per-token design
    # this whole build is premised on. Same idempotent-by-default pattern
    # `backfill.py`/`backfill_discovered_launches.py` already use ("skip a
    # mint already recorded unless told otherwise").
    ledger_path = Path(args.ledger_out) if args.ledger_out else Path(args.data) / "paper_trades.csv"
    already_decided = mints_already_decided(ledger_path, mode="replay")
    mints = [m for m in all_mints if m not in already_decided]
    n_skipped_already_decided = len(all_mints) - len(mints)

    print("=" * 78)
    print("PAPER TRADE REPLAY (Phase A, docs/PAPER_TRADING_PLAN.md) -- not a claim of edge.")
    print("=" * 78)
    print(f"  requested: {requested}  eligible-by-swaps: {len(eligible_by_swaps)}  "
          f"excluded(too old): {excluded_too_old}  excluded(no data): {excluded_no_real_creation}  "
          f"already decided in a prior replay run (skipped, see D89): {n_skipped_already_decided}  "
          f"considered: {len(mints)}")
    print(f"  exit rule (fixed, v1): upper={args.upper}x lower={args.lower*100:.0f}% "
          f"horizon={args.horizon_min:.0f}min")
    print(f"  exploration: epsilon {args.epsilon_start:.2f} -> {args.epsilon_floor:.2f} "
          f"(decay scale {args.epsilon_decay_scale:.0f} decisions)")

    feature_names = list(TokenState("probe").features().keys())

    if args.model_in:
        online = OnlinePolicy.load(Path(args.model_in))
        print(f"  resumed online policy from {args.model_in} "
              f"(n_decisions={online.n_decisions}, n_updates={online.n_updates})")
    else:
        online = OnlinePolicy(feature_names, upper_multiple=args.upper, lower_pct=args.lower,
                              learning_rate=args.learning_rate, epsilon_start=args.epsilon_start,
                              epsilon_floor=args.epsilon_floor,
                              epsilon_decay_scale=args.epsilon_decay_scale, seed=args.seed)
    random_baseline = OnlinePolicy(feature_names, upper_multiple=args.upper, lower_pct=args.lower,
                                   epsilon_start=1.0, epsilon_floor=1.0, seed=args.seed + 1)

    # D110 (docs/DECISIONS.md): same real problem as information_audit.py's
    # build() loop -- this does one store.iter_swaps(mint) query PER mint
    # (up to `--limit`, now routinely in the thousands post-pumpfundata-merge)
    # plus bar/feature/label/online-policy-update work on each, with zero
    # progress output until the whole loop finishes. Wall-clock-throttled
    # (not a fixed mint-count tick) for the same reason as information_audit.py:
    # per-mint cost varies hugely, so a fixed tick either spams or goes silent.
    total_mints = len(mints)
    _t0 = time.time()
    _last_print = _t0
    all_rows: List[Dict] = []
    n_no_decision = 0
    n_truncated = 0
    reason_counter: Counter = Counter()
    def _maybe_report_progress(i: int) -> None:
        nonlocal _last_print
        now = time.time()
        if now - _last_print >= 5.0 or i == total_mints:
            elapsed = now - _t0
            rate = i / elapsed if elapsed > 0 else 0.0
            eta_s = (total_mints - i) / rate if rate > 0 else float("nan")
            print(f"  [replay] {i}/{total_mints} mints ({i / total_mints:.1%})  "
                  f"elapsed={elapsed:.0f}s  ETA~{eta_s:.0f}s  "
                  f"decision_points={len(all_rows) // 2}  "
                  f"online_decisions={online.n_decisions}", flush=True)
            _last_print = now

    for _i, mint in enumerate(mints, start=1):
        prep = prepare_mint(mint, store.iter_swaps(mint), cfg, args.bar_fraction, rails,
                            cost, args.nominal_size, reasons=reason_counter)
        rows = apply_decision(prep, mint, online, random_baseline, "replay") if prep else []
        if not rows:
            n_no_decision += 1
        all_rows.extend(rows)
        _maybe_report_progress(_i)

    n_decision_points = len(all_rows) // 2  # one row per policy per decision point
    n_entered_online = sum(1 for r in all_rows if r["policy"] == "online" and r["action"] == "enter")
    n_entered_random = sum(1 for r in all_rows if r["policy"] == "random" and r["action"] == "enter")

    print(f"\n  tokens considered: {len(mints)}  decision points reached: {n_decision_points}  "
          f"no-decision (rails never passed / tape too short): {len(mints) - n_decision_points}")
    if reason_counter:
        print("  rejection reasons (one per no-decision token, evaluated at its LAST "
              "bar -- its best shot; counts sum to the no-decision total above; "
              "see docs/DECISIONS.md D85):")
        for reason, count in reason_counter.most_common():
            print(f"    {reason}: {count}")
    print(f"  online policy entered: {n_entered_online}/{n_decision_points}   "
          f"random baseline entered: {n_entered_random}/{n_decision_points}")
    print(f"  online policy after replay: n_decisions={online.n_decisions} "
          f"n_updates={online.n_updates} epsilon_now={online.epsilon:.3f}")

    write_header = not ledger_path.exists()
    with open(ledger_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
        if write_header:
            writer.writeheader()
        for row in all_rows:
            writer.writerow(row)
    print(f"\n  appended {len(all_rows)} row(s) to {ledger_path}")

    model_out = Path(args.model_out) if args.model_out else Path(args.data) / "online_policy_state.json"
    online.save(model_out)
    print(f"  saved online policy checkpoint to {model_out}")
    print("\nRun scripts/paper_trading_report.py next -- this script does not itself judge "
          "whether anything worked (docs/PAPER_TRADING_PLAN.md Sec 6: pre-registered evaluation, "
          "not an eyeballed equity curve).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
