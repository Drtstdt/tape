#!/usr/bin/env python3
"""D128 / registry T004: what did family A actually find? (discovery only, ~1 min)

Hypothesis to check (stated before running, D128): A002-A006 are one factor --
how far up the bonding curve the token is at the decision -- and their
'improvement' is the curve FLOOR: the virtual SOL reserve cannot fall below
its start, so near the start the other holders can take out little more than
they put in, our downside is bounded and an uneventful trade costs ~fees.

Reported:
  * Spearman correlation matrix of the 7 family-A features
  * worst_case_net: point-in-time, from the entry state alone -- our net
    return if EVERY other holder sold after our 2-SOL buy (standard curve)
  * by decile of q_gain_sol (whole discovery universe): P(TP), P(SL), P(TIME),
    mean / median net, mean worst_case_net, share of |net| < 5%
  * for each family-A rule (direction/threshold from results.json, second
    half): outcome mix of the selected rows
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_family_A as rfa  # noqa: E402
from tape import trials as tr  # noqa: E402
from tape import curve as cv  # noqa: E402
from tape.registry import Registry  # noqa: E402


def worst_case_net(q0: np.ndarray, size: float = 2.0, fee_b: float = 0.0125, fee_s: float = 0.0125,
                   k: float = cv.K_STD, v: float = cv.V_STD) -> np.ndarray:
    """Every token outside the curve belongs to someone; on the standard curve
    the others hold k/v - k/q0 tokens. After our buy (sol_in), they all sell,
    then we sell: our gross = q_after_them - v."""
    sol_in = size / (1 + fee_b)
    t_after_us = k / (q0 + sol_in)
    others = k / v - k / q0
    q_after_them = k / (t_after_us + others)
    gross = q_after_them - v
    return gross / (1 + fee_s) / size - 1.0


def main() -> int:
    import pandas as pd
    out = Path(r"E:\tape_research\trials\family_A")
    lf = open(out / "diag_T004.txt", "w", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")

    try:
        df = rfa.load_universe(Path(r"E:\tape_research\sets_v2\discovery"), [10], log)
        import pyarrow.parquet as pq
        parts = sorted(Path(r"E:\tape_research\sets_v2\discovery").glob("part-b*.parquet"))
        more = pd.concat([pq.read_table(p, columns=["mint", "K", "o_base_entry_q", "o_base_impact",
                                                    "o_base_hold_s", "o_base_min_net", "fee_buy_ratio",
                                                    "fee_sell_ratio"]).to_pandas() for p in parts])
        more = more[more["K"] == 10].drop(columns="K")
        u = df[(df["K"] == 10) & (df["is_mayhem"] == 0) & (~df["chk_curve_fitted"].astype(bool))
               & (~df["o_base_status"].isin(["CENS", "NOENTRY"]))].merge(more, on="mint", how="left")
        u = u.sort_values("create_ts").reset_index(drop=True)
        log(f"T004 universe (K=10, as family A): {len(u):,}")
        fb = u["fee_buy_ratio"].fillna(0.0125).to_numpy()
        fs = u["fee_sell_ratio"].fillna(pd.Series(fb)).to_numpy()
        u["worst_case_net"] = worst_case_net(u["o_base_entry_q"].to_numpy(float), 2.0, fb, fs)
        y = u[rfa.TARGET].to_numpy(float)

        log("\n-- Spearman correlation among family-A features (+ worst_case_net) --")
        cols = rfa.FEATURES + ["worst_case_net"]
        mat = pd.DataFrame(index=cols, columns=cols, dtype=float)
        for a_ in cols:
            for b_ in cols:
                mat.loc[a_, b_] = tr.spearman(u[a_].to_numpy(float), u[b_].to_numpy(float))
        for line in mat.round(3).to_string().splitlines():
            log("  " + line)
        log(f"\n  rho(worst_case_net, net) = {tr.spearman(u['worst_case_net'].to_numpy(float), y):+.4f}  "
            f"(family A best |rho| was 0.392)")

        log("\n-- by decile of q_gain_sol (whole discovery universe) --")
        u["dec"] = pd.qcut(u["q_gain_sol"].rank(method="first"), 10, labels=False)
        g = u.groupby("dec").agg(q_gain_med=("q_gain_sol", "median"),
                                 p_tp=("o_base_status", lambda s: (s == "TP").mean()),
                                 p_sl=("o_base_status", lambda s: (s == "SL").mean()),
                                 p_time=("o_base_status", lambda s: (s == "TIME").mean()),
                                 mean_net=(rfa.TARGET, "mean"), median_net=(rfa.TARGET, "median"),
                                 worst_case=("worst_case_net", "mean"),
                                 small_move=(rfa.TARGET, lambda s: (s.abs() < 0.05).mean()),
                                 impact=("o_base_impact", "median"))
        for line in g.round(4).to_string().splitlines():
            log("  " + line)

        res = json.loads((out / "results.json").read_text(encoding="utf-8"))["results"]
        first = np.arange(len(u)) < len(u) // 2
        sec = u[~first]
        log("\n-- outcome mix of each family-A rule's selection (second half) --")
        rows = {}
        for f in rfa.FEATURES:
            r = res[f]
            x = sec[f].to_numpy(float)
            sel = (x >= r["threshold"]) if r["direction"] > 0 else (x <= r["threshold"])
            s_ = sec[sel]
            rows[f] = {"n": int(sel.sum()), "p_tp": float((s_["o_base_status"] == "TP").mean()),
                       "p_sl": float((s_["o_base_status"] == "SL").mean()),
                       "p_time": float((s_["o_base_status"] == "TIME").mean()),
                       "mean_net": float(s_[rfa.TARGET].mean()), "median_net": float(s_[rfa.TARGET].median()),
                       "worst_case": float(s_["worst_case_net"].mean())}
            log(f"  {f:20s} n={rows[f]['n']:,} P(TP)={rows[f]['p_tp']:.3f} P(SL)={rows[f]['p_sl']:.3f} "
                f"P(TIME)={rows[f]['p_time']:.3f} mean={rows[f]['mean_net']:+.4f} median={rows[f]['median_net']:+.4f} "
                f"worst-case mean={rows[f]['worst_case']:+.4f}")
        allsec = {"p_tp": float((sec["o_base_status"] == "TP").mean()),
                  "p_sl": float((sec["o_base_status"] == "SL").mean()), "mean_net": float(sec[rfa.TARGET].mean())}
        log(f"  {'ALL second half':20s} P(TP)={allsec['p_tp']:.3f} P(SL)={allsec['p_sl']:.3f} mean={allsec['mean_net']:+.4f}")

        reg = Registry(r"E:\tape_research\registry")
        reg.register("T", "T004 family A decomposition", "A002-A006 = curve position; gain = curve floor",
                     {"source": "diag_family_A.py", "K": 10}, zone="discovery", status="diagnostic",
                     metrics={"corr": mat.round(4).to_dict(), "deciles": g.round(5).to_dict(),
                              "rules": rows, "all_second": allsec,
                              "rho_worst_case": tr.spearman(u["worst_case_net"].to_numpy(float), y)})
        log(f"\n  registered T004 -> {out / 'diag_T004.txt'}")
        return 0
    finally:
        lf.close()


if __name__ == "__main__":
    raise SystemExit(main())
