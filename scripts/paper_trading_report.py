#!/usr/bin/env python3
"""D84 (docs/DECISIONS.md) -- the evaluation step for paper trading
(docs/PAPER_TRADING_PLAN.md Sec 5-6). Reads the decision ledger written by
`scripts/paper_trade_replay.py` and/or `scripts/paper_trade_live.py`
(`data/paper_trades.csv` by default) and reports a PRE-REGISTERED verdict --
never an eyeballed equity curve.

Pre-registered criteria (docs/PAPER_TRADING_PLAN.md Sec 6, fixed BEFORE
this script existed):

  1. Minimum sample: >= 300 resolved EXPLOIT-phase online decisions
     (entered or abstained -- both are resolved decisions the model formed
     a belief about), spanning >= 5 distinct calendar days.
  2. If (1) isn't met: status is INSUFFICIENT_SAMPLE, not a pass or a fail
     -- the honest "keep running, this isn't a verdict yet" answer, same
     spirit as Store.mints() returning [] meaning "collecting_data", not
     "no signal".
  3. If (1) IS met: PASS requires BOTH (a) the exploit-phase, cost-adjusted
     PnL's token-cluster bootstrap CI (on ENTERED decisions only) excludes
     zero on the positive side, AND (b) that mean beats the parallel
     random-policy baseline's mean over the same decisions. Either alone is
     not enough -- a positive CI a random policy would have matched too is
     not evidence of learning (Sec 6's whole point).

Explore-phase decisions are reported (so the "idiotic mistakes" are visible
and auditable) but never count toward the verdict -- that's the entire
reason explore/exploit is logged per-row in the first place.

D90 (docs/DECISIONS.md) ADDITION -- a regime-robustness check, not part of
the pre-registered verdict: the first real EDGE_FOUND result (680 exploit
decisions, 8 days) coincided with the random baseline ALSO clearing
breakeven by a wide margin, because the whole token population's base
pump-rate drifted upward over that week (a real market-regime effect, not
something either policy caused). A pooled online-vs-random mean comparison
can't tell "the policy is discriminating" apart from "the whole population
got easier that week." This report therefore also prints each policy's
ENTERED-vs-ABSTAINED gap in the decision's own fixed-barrier payoff
(UP/DOWN are deterministic from the barrier config; TIMEOUT is treated as a
wash/0, same convention `OnlinePolicy.breakeven_p` already uses) -- this
comparison holds the calendar day fixed implicitly (both groups are drawn
from the exact same decisions), so a regime getting hotter or colder moves
both groups together and cancels out of the GAP, unlike a pooled mean.
`random`'s own gap is reported alongside as a built-in placebo: its actions
never depend on `p_raw`, so its gap should sit at noise around zero by
construction -- if it doesn't, something is wrong with this diagnostic
itself, not with the online policy.

    python scripts/paper_trading_report.py --data data
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from tape.cv import token_bootstrap

MIN_EXPLOIT_DECISIONS = 300
MIN_DISTINCT_DAYS = 5

_FIXED_PAYOFF = {}  # populated from --upper/--lower in main(), read by _fixed_payoff


def _fixed_payoff(outcome: str) -> float:
    if outcome == "UP":
        return _FIXED_PAYOFF["upper_multiple"] - 1.0
    if outcome == "DOWN":
        return _FIXED_PAYOFF["lower_pct"]
    return 0.0  # TIMEOUT treated as a wash -- same convention as OnlinePolicy.breakeven_p


def _discrimination_gap(df: pd.DataFrame, label: str, n_boot: int = 2000, seed: int = 0) -> dict:
    """Entered-vs-abstained gap in fixed-barrier payoff, token-cluster
    bootstrapped. Resamples MINTS (not rows) and recomputes the gap on each
    resampled pool, so the CI reflects the real effective sample size (D3 /
    tape/cv.py::token_bootstrap's own stated reasoning), not the row count."""
    d = df.copy()
    d["_payoff"] = d["outcome"].map(_fixed_payoff)
    entered = d[d["action"] == "enter"]
    abstained = d[d["action"] == "abstain"]
    out = {"label": label, "n_entered": len(entered), "n_abstained": len(abstained)}
    if len(entered) == 0 or len(abstained) == 0:
        out.update(gap=float("nan"), ci_lo=float("nan"), ci_hi=float("nan"), n_mints=d["mint"].nunique())
        return out
    out["gap"] = float(entered["_payoff"].mean() - abstained["_payoff"].mean())
    mints = d["mint"].unique()
    out["n_mints"] = len(mints)
    if len(mints) < 2:
        out["ci_lo"] = out["ci_hi"] = float("nan")
        return out
    # Vectorized, numpy-only bootstrap (mirrors tape/cv.py::token_bootstrap's own
    # pattern) -- a pandas concat-per-replicate loop is far too slow at
    # n_boot=2000 over hundreds of mints (timed out in practice building this).
    rng = np.random.default_rng(seed)
    is_enter = (d["action"].to_numpy() == "enter")
    payoff = d["_payoff"].to_numpy()
    mint_arr = d["mint"].to_numpy()
    by_mint_enter = {m: payoff[(mint_arr == m) & is_enter] for m in mints}
    by_mint_abstain = {m: payoff[(mint_arr == m) & ~is_enter] for m in mints}
    gaps = np.empty(n_boot)
    for b in range(n_boot):
        picked = rng.choice(mints, size=len(mints), replace=True)
        e = np.concatenate([by_mint_enter[m] for m in picked])
        a = np.concatenate([by_mint_abstain[m] for m in picked])
        gaps[b] = (e.mean() - a.mean()) if (len(e) and len(a)) else np.nan
    out["ci_lo"] = float(np.nanpercentile(gaps, 2.5))
    out["ci_hi"] = float(np.nanpercentile(gaps, 97.5))
    return out


def _print_discrimination(g: dict) -> None:
    print(f"  {g['label']:38s} n_entered={g['n_entered']:5d}  n_abstained={g['n_abstained']:5d}  "
          f"n_mints={g['n_mints']:4d}")
    if g["n_entered"] == 0 or g["n_abstained"] == 0:
        print("    (need both entered and abstained rows to compute a gap)")
        return
    print(f"    entered-minus-abstained fixed-payoff gap: {g['gap']:+.1%}")
    if g["n_mints"] >= 2:
        print(f"    95% token-cluster bootstrap CI: [{g['ci_lo']:+.1%}, {g['ci_hi']:+.1%}]  "
              f"excludes 0: {g['ci_lo'] > 0 or g['ci_hi'] < 0}")
    else:
        print("    (fewer than 2 distinct mints -- no bootstrap CI possible yet)")


def _distinct_days(ts_ms_series: pd.Series) -> int:
    dates = pd.to_datetime(ts_ms_series, unit="ms", utc=True).dt.date
    return dates.nunique()


def _pnl_stats(df: pd.DataFrame, label: str) -> dict:
    entered = df[df["action"] == "enter"]
    n = len(df)
    n_entered = len(entered)
    out = {"label": label, "n_decisions": n, "n_entered": n_entered}
    if n_entered == 0:
        out.update(win_rate=float("nan"), mean_raw_pnl=float("nan"),
                   mean_cost_adjusted_pnl=float("nan"), ci_lo=float("nan"), ci_hi=float("nan"),
                   n_mints=0)
        return out
    wins = (entered["cost_adjusted_pnl_pct"] > 0).sum()
    out["win_rate"] = wins / n_entered
    out["mean_raw_pnl"] = float(entered["raw_pnl_pct"].mean())
    out["mean_cost_adjusted_pnl"] = float(entered["cost_adjusted_pnl_pct"].mean())
    out["n_mints"] = entered["mint"].nunique()
    if out["n_mints"] >= 2:
        lo, hi = token_bootstrap(entered["cost_adjusted_pnl_pct"].to_numpy(),
                                 entered["mint"].to_numpy())
        out["ci_lo"], out["ci_hi"] = lo, hi
    else:
        out["ci_lo"] = out["ci_hi"] = float("nan")
    return out


def _print_stats(s: dict) -> None:
    print(f"  {s['label']:38s} n_decisions={s['n_decisions']:5d}  n_entered={s['n_entered']:5d}  "
          f"n_mints={s['n_mints']:4d}")
    if s["n_entered"] == 0:
        print("    (no entered decisions -- nothing to report)")
        return
    print(f"    win_rate={s['win_rate']:.1%}  mean_raw_pnl={s['mean_raw_pnl']:+.1%}  "
          f"mean_cost_adjusted_pnl={s['mean_cost_adjusted_pnl']:+.1%}")
    if s["n_mints"] >= 2:
        print(f"    95% token-cluster bootstrap CI (cost-adjusted): "
              f"[{s['ci_lo']:+.1%}, {s['ci_hi']:+.1%}]  excludes 0 on the positive side: "
              f"{s['ci_lo'] > 0}")
    else:
        print("    (fewer than 2 distinct mints -- no bootstrap CI possible yet)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--ledger", default=None,
                     help="path to the decision ledger (default: <--data>/paper_trades.csv)")
    ap.add_argument("--mode", choices=["all", "replay", "live"], default="all",
                     help="restrict to rows from a single mode, or 'all' (default) to pool both")
    ap.add_argument("--upper", type=float, default=1.6,
                     help="must match the barrier config the ledger was produced with "
                          "(paper_trade_replay.py / paper_trade_live.py's own --upper default)")
    ap.add_argument("--lower", type=float, default=-0.30,
                     help="must match the barrier config the ledger was produced with")
    args = ap.parse_args()
    _FIXED_PAYOFF["upper_multiple"] = args.upper
    _FIXED_PAYOFF["lower_pct"] = args.lower

    ledger_path = Path(args.ledger) if args.ledger else Path(args.data) / "paper_trades.csv"
    if not ledger_path.exists():
        print(f"{ledger_path} does not exist -- run scripts/paper_trade_replay.py "
              f"(or paper_trade_live.py) first. status=collecting_data", file=sys.stderr)
        return 2

    df = pd.read_csv(ledger_path)
    if args.mode != "all":
        df = df[df["mode"] == args.mode]
    if df.empty:
        print(f"No rows for mode={args.mode!r} in {ledger_path}. status=collecting_data")
        return 2

    df["explore"] = df["explore"].astype(str).str.lower().isin(("true", "1"))
    df["truncated"] = df["truncated"].astype(str).str.lower().isin(("true", "1"))

    online = df[df["policy"] == "online"]
    random_all = df[df["policy"] == "random"]
    online_exploit = online[~online["explore"]]
    online_explore = online[online["explore"]]

    print("=" * 88)
    print("PAPER TRADING REPORT (docs/PAPER_TRADING_PLAN.md Sec 6) -- pre-registered verdict,")
    print("not an eyeballed equity curve.")
    print("=" * 88)
    print(f"  ledger: {ledger_path}  mode={args.mode}  total rows: {len(df)}")

    n_exploit = len(online_exploit)
    n_days = _distinct_days(online["decision_ts_ms"]) if len(online) else 0
    print(f"\n  online exploit-phase resolved decisions: {n_exploit}  "
          f"(need >= {MIN_EXPLOIT_DECISIONS})")
    print(f"  distinct calendar days covered (online, all decisions): {n_days}  "
          f"(need >= {MIN_DISTINCT_DAYS})")
    print(f"  online explore-phase decisions (excluded from verdict, shown for audit): "
          f"{len(online_explore)}")

    print("\n" + "-" * 88)
    print("BREAKDOWN (explore-phase shown for visibility only -- never part of the verdict)")
    print("-" * 88)
    stats_exploit = _pnl_stats(online_exploit, "online (exploit only -- the verdict population)")
    _print_stats(stats_exploit)
    stats_explore = _pnl_stats(online_explore, "online (explore only -- audit/visibility)")
    _print_stats(stats_explore)
    stats_random = _pnl_stats(random_all, "random baseline (all rows -- epsilon=1 by construction)")
    _print_stats(stats_random)

    print("\n" + "-" * 88)
    print("BELIEF DISCRIMINATION CHECK (D90, docs/DECISIONS.md) -- regime-robustness, NOT part of")
    print("the pre-registered verdict below. See this script's own module docstring for why.")
    print("-" * 88)
    gap_online = _discrimination_gap(online_exploit, "online (exploit only)")
    _print_discrimination(gap_online)
    gap_random = _discrimination_gap(random_all, "random baseline (placebo -- should be ~0)")
    _print_discrimination(gap_random)

    print("\n" + "=" * 88)
    print("VERDICT")
    print("=" * 88)
    if n_exploit < MIN_EXPLOIT_DECISIONS or n_days < MIN_DISTINCT_DAYS:
        print(f"  status=INSUFFICIENT_SAMPLE -- {n_exploit}/{MIN_EXPLOIT_DECISIONS} exploit "
              f"decisions, {n_days}/{MIN_DISTINCT_DAYS} distinct days.")
        print("  This is not a pass or a fail. Keep running paper_trade_replay.py / "
              "paper_trade_live.py and re-run this report later -- per docs/PAPER_TRADING_PLAN.md "
              "Sec 6, no verdict is drawn below the pre-registered minimum sample.")
        return 0

    ci_excludes_zero = stats_exploit["n_mints"] >= 2 and stats_exploit["ci_lo"] > 0
    beats_random = (stats_exploit["n_entered"] > 0 and stats_random["n_entered"] > 0
                    and stats_exploit["mean_cost_adjusted_pnl"] > stats_random["mean_cost_adjusted_pnl"])
    passed = ci_excludes_zero and beats_random

    print(f"  CI excludes zero (positive side): {ci_excludes_zero}")
    print(f"  beats parallel random baseline (mean cost-adjusted PnL): {beats_random}")
    if passed:
        print("\n  status=EDGE_FOUND (paper, online policy) -- both pre-registered conditions met.")
        print("  This is real evidence worth taking seriously, not a claim to deploy real capital")
        print("  on -- see docs/PAPER_TRADING_PLAN.md Sec 7 for how this relates to docs/PLAN.md's")
        print("  own Stage 5/6 gates.")
    else:
        print("\n  status=NO_EDGE_FOUND (paper, online policy) -- sample was large enough to judge,")
        print("  but did not clear both pre-registered conditions. Per docs/PAPER_TRADING_PLAN.md")
        print("  Sec 6: do not loosen the criteria to make this look like a pass. Keep running for")
        print("  more calendar-diverse data, or revisit the feature set / exit rule -- log either")
        print("  as a new D-numbered decision, same as every other real finding in this project.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
