#!/usr/bin/env python3
"""D130: family E -- LightGBM over ALL point-in-time features, walk-forward
inside the DISCOVERY zone. Asks: does a COMBINATION of features find a slice
with positive realised net return, where no single feature did (A, C)?

Fixed BEFORE the first run (docs/DECISIONS.md D130):
  universe   as families A/C (K=10, non-mayhem, standard curve, status not
             CENS/NOENTRY); target o_base_net (2 SOL, 1 s, +60/-30, 30 min)
  features   tape.pit_features.FEATURE_NAMES + every st_* column +
             worst_case_pit (point-in-time); NEVER create_ts / decision_ts /
             chk_* / o_* / dec_idx
  model      LightGBM regression, fixed params (PARAMS below), no tuning
  folds      calendar weeks of create_ts (UTC, Monday start); for each test
             week w with >= 4 earlier weeks: model_b fit on weeks < w minus a
             1-day embargo, predicts week w; model_a fit on weeks < w-1 (same
             embargo) predicts week w-1 = calibration week for thresholds
  trials     E000 enter iff prediction > 0
             E001 enter iff prediction >= 90th pct of the calibration week's
                  predictions (top 10%, threshold known before week w)
             E002 same with top 2%
  baseline   the curve-floor rule: worst_case_pit in its best quintile, fitted
             on weeks < w (reported; a trial must beat it)
  pass       pooled out-of-fold selected mean net: day-block bootstrap 95%
             CI > 0 AND positive in >= 60% of test weeks AND above the
             baseline's pooled mean AND BH q < 0.10 (p = one-sided bootstrap)
Refuses to run twice. Needs `pip install lightgbm` (never substituted).

D133: `--replicate` re-runs the SAME specs on research set v3 (corrected
dedup) -- no new trials; results mapped onto E000..E002, output in
trials/family_E_v3 (its results.json carries the E000 entry set that families
F and H start from).

    python scripts\\run_family_E.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tape import trials as tr  # noqa: E402
from tape import zones as zn  # noqa: E402
from tape.pit_features import FEATURE_NAMES  # noqa: E402
from tape import replication as rp  # noqa: E402
from tape.registry import Registry, spec_hash  # noqa: E402

K = 10
TARGET = "o_base_net"
MIN_TRAIN_WEEKS = 4
EMBARGO_MS = 86_400_000
WEEK_MS = 7 * 86_400_000
MONDAY0 = 4 * 86_400_000          # 1970-01-05 was a Monday
PARAMS = {"objective": "regression", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 200,
          "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
          "seed": 0, "verbose": -1, "num_threads": 4}
N_ROUNDS = 400
RULES = {"E000": ("pred > 0", None), "E001": ("top 10% (calibration-week threshold)", 0.10),
         "E002": ("top 2% (calibration-week threshold)", 0.02)}
UNIVERSE = "K=10, non-mayhem, standard curve, status not CENS/NOENTRY, discovery"
FORBIDDEN = ("create_ts", "decision_ts", "dec_idx", "mint", "K")


def worst_case_net(q0, size=2.0, fee_b=0.0125, fee_s=0.0125, k=30.0 * 1.073e9, v=30.0):
    sol_in = size / (1 + fee_b)
    return (k / (k / (q0 + sol_in) + k / v - k / q0) - v) / (1 + fee_s) / size - 1.0


def make_model():
    """Returns (fit(X, y) -> predictor, name). LightGBM only -- a missing
    package stops the run instead of silently changing the pre-registered model."""
    import lightgbm as lgb

    def fit(X, y):
        b = lgb.train(PARAMS, lgb.Dataset(X, label=y), num_boost_round=N_ROUNDS)
        return b.predict
    return fit, f"lightgbm {lgb.__version__}"


def load(sets_dir: Path, log):
    import pandas as pd
    import pyarrow.parquet as pq
    parts = sorted(sets_dir.glob("part-b*.parquet"))
    df = pd.concat([pq.read_table(p, filters=[("K", "=", K)]).to_pandas() for p in parts], ignore_index=True)
    log(f"  loaded {len(df):,} rows x {df.shape[1]} cols from {len(parts)} parts")
    return df


def feature_columns(df):
    cols = [c for c in FEATURE_NAMES if c in df.columns] + sorted(c for c in df.columns if c.startswith("st_"))
    cols.append("worst_case_pit")
    bad = [c for c in cols if c in FORBIDDEN or c.startswith(("o_", "chk_"))]
    if bad:
        raise RuntimeError(f"forbidden feature columns: {bad}")
    return cols


def prepare_universe(df):
    """Family-E universe and design matrix (shared with export_family_E_oof.py)."""
    u = df[(df["is_mayhem"] == 0) & (~df["chk_curve_fitted"].astype(bool))
           & (~df["o_base_status"].isin(["CENS", "NOENTRY"]))].sort_values("create_ts").reset_index(drop=True)
    fb = u["fee_buy_ratio"].fillna(0.0125).to_numpy(float)
    fs = np.where(np.isfinite(u["fee_sell_ratio"].to_numpy(float)), u["fee_sell_ratio"].to_numpy(float), fb)
    u["worst_case_pit"] = worst_case_net(u["q_now"].to_numpy(float), 2.0, fb, fs)
    cols = feature_columns(u)
    X = u[cols].to_numpy(np.float32)
    y = u[TARGET].to_numpy(float)
    ts = u["create_ts"].to_numpy()
    week = (ts - MONDAY0) // WEEK_MS
    day = ts // tr.DAY_MS
    return u, cols, X, y, ts, week, day, np.unique(week)


def walk_forward(u, X, y, ts, week, weeks, fit, log):
    """Weekly walk-forward (D130). Returns (pred, thresholds per rule, floor-baseline
    selection, test weeks). Deterministic for a deterministic `fit`."""
    pred = np.full(len(u), np.nan)
    thr = {tid: np.full(len(u), np.nan) for tid in RULES}
    base_sel = np.zeros(len(u), dtype=bool)
    test_weeks = [w for w in weeks if np.sum(weeks < w) >= MIN_TRAIN_WEEKS]
    t1 = time.time()
    for i, w in enumerate(test_weeks, start=1):
        start_w = w * WEEK_MS + MONDAY0
        test = week == w
        tr_b = ts < start_w - EMBARGO_MS
        cal = week == (w - 1)
        tr_a = ts < (start_w - WEEK_MS) - EMBARGO_MS
        assert not np.any(tr_b & test) and ts[tr_b].max() < start_w - EMBARGO_MS   # no look-ahead
        assert not np.any(tr_a & cal) and not np.any(tr_a & test)
        pb = fit(X[tr_b], y[tr_b])
        pred[test] = pb(X[test])
        pa = fit(X[tr_a], y[tr_a])
        pcal = pa(X[cal])
        for tid, (_, top) in RULES.items():
            thr[tid][test] = 0.0 if top is None else float(np.quantile(pcal, 1 - top))
        wc = u["worst_case_pit"].to_numpy(float)
        rho = tr.spearman(wc[tr_b], y[tr_b])
        cut = np.nanquantile(wc[tr_b], 0.8 if rho >= 0 else 0.2)
        base_sel[test] = (wc[test] >= cut) if rho >= 0 else (wc[test] <= cut)
        el = time.time() - t1
        log(f"  week {i}/{len(test_weeks)} (n_test={int(test.sum()):,}, n_train={int(tr_b.sum()):,}) "
            f"elapsed={el:.0f}s ETA~{el / i * (len(test_weeks) - i):.0f}s")
    return pred, thr, base_sel, test_weeks


def main() -> int:
    holder = []
    try:
        return _main(holder)
    finally:
        for f in holder:
            f.close()


def _main(holder, model_factory=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sets", default=None, help="default sets_v2 (sets_v3 with --replicate)")
    ap.add_argument("--zones", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--out", default=None, help="default trials/family_E (family_E_v3 with --replicate)")
    ap.add_argument("--replicate", action="store_true", help="D133: same specs on sets_v3, no new trials")
    a = ap.parse_args()
    rep_mode = a.replicate
    a.sets = a.sets or (r"E:\tape_research\sets_v3\discovery" if rep_mode else r"E:\tape_research\sets_v2\discovery")
    a.out = a.out or (r"E:\tape_research\trials\family_E_v3" if rep_mode else r"E:\tape_research\trials\family_E")
    import pandas as pd

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lf = open(out / "log.txt", "a", encoding="utf-8")
    holder.append(lf)

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    reg = Registry(a.registry)
    specs = {tid: {"family": "E", "rule": desc, "top": top, "model": "lightgbm", "params": PARAMS,
                   "rounds": N_ROUNDS, "K": K, "target": TARGET, "universe": UNIVERSE,
                   "folds": "weekly walk-forward, >=4 train weeks, 1-day embargo, calibration week",
                   "set": "sets_v2"} for tid, (desc, top) in RULES.items()}
    if rep_mode:
        orig = rp.original_ids(reg, "E", "rule")
        if rp.already_replicated(reg, "E"):
            log("REFUSED: family E was already replicated on sets_v3 (see registry).")
            return 2
        if not orig:
            log("REFUSED: nothing to replicate -- no original family E trials in the registry")
            return 2
    elif any(spec_hash(s) in {t.get("spec_hash") for t in reg.trials().values()} for s in specs.values()):
        log("REFUSED: family E trials with these specs are already registered (no re-rolls).")
        return 2
    try:
        fit, model_name = (model_factory or make_model)()
    except ImportError:
        log("STOP: lightgbm is not installed -- run `pip install lightgbm` (the model is pre-registered; "
            "no substitute is used).")
        return 4

    t0 = time.time()
    log(f"\n=== run_family_E {datetime.now(timezone.utc).isoformat()} model={model_name}"
        f"{' REPLICATION (D133)' if rep_mode else ''} sets={a.sets} ===")
    disc = set(zn.load_zone(a.zones, "discovery"))
    df = load(Path(a.sets), log)
    df = df[df["K"] == K]
    if not set(df["mint"]).issubset(disc):
        log("REFUSED: research set contains mints outside the frozen discovery zone")
        return 3
    u, cols, X, y, ts, week, day, weeks = prepare_universe(df)
    log(f"  universe {len(u):,} tokens, {len(cols)} features, {len(weeks)} calendar weeks")
    pred, thr, base_sel, test_weeks = walk_forward(u, X, y, ts, week, weeks, fit, log)

    oof = np.isfinite(pred)
    yo, do, wo = y[oof], day[oof], week[oof]
    allm = float(np.nanmean(yo))
    bsel = base_sel[oof]
    base_mean = float(np.nanmean(yo[bsel])) if bsel.any() else float("nan")
    base_ci = tr.day_block_bootstrap(yo, bsel, do, 1000, seed=3)["sel_mean_ci"] if bsel.any() else [np.nan, np.nan]
    log(f"\n  out-of-fold rows {int(oof.sum()):,} over {len(test_weeks)} weeks; all mean net {allm:+.4f}; "
        f"floor-rule baseline mean {base_mean:+.4f} CI{[round(v, 4) for v in base_ci]} (n={int(bsel.sum()):,})")
    log(f"  rank corr(pred, net) out-of-fold: {tr.spearman(pred[oof], yo):+.4f}")
    dec = np.full(int(oof.sum()), -1)
    order = np.argsort(pred[oof])
    dec[order] = (np.arange(len(order)) * 10) // len(order)
    log("  out-of-fold mean net by prediction decile: " +
        " ".join(f"{d}:{np.nanmean(yo[dec == d]):+.3f}" for d in range(10)))

    results, ids = {}, {}
    rng = np.random.default_rng(5)
    for tid, (desc, top) in RULES.items():
        sel = pred[oof] > thr[tid][oof]
        n_sel = int(sel.sum())
        if n_sel == 0:
            results[tid] = {"rule": desc, "n_selected": 0}
            log(f"  {tid} {desc}: nothing selected")
            continue
        boot = tr.day_block_bootstrap(yo, sel, do, 2000, seed=4)
        means = []
        days_u = np.unique(do)
        idx_by_day = [np.flatnonzero(do == dd) for dd in days_u]
        for _ in range(2000):
            idx = np.concatenate([idx_by_day[j] for j in rng.integers(0, len(days_u), len(days_u))])
            s_ = sel[idx]
            if s_.any():
                means.append(np.nanmean(yo[idx][s_]))
        p_one = float((1 + np.sum(np.array(means) <= 0)) / (1 + len(means)))
        wk = [float(np.nanmean(yo[(wo == w) & sel])) for w in test_weeks if np.any((wo == w) & sel)]
        res = {"rule": desc, "n_selected": n_sel, "mean_sel": float(np.nanmean(yo[sel])),
               "sel_mean_ci": boot["sel_mean_ci"], "p_one_sided": p_one,
               "p_tp": float(np.mean(u["o_base_status"].to_numpy()[oof][sel] == "TP")),
               "weeks_positive": int(sum(1 for v in wk if v > 0)), "weeks_with_selection": len(wk),
               "week_means": wk, "baseline_mean": base_mean, "all_mean": allm}
        results[tid] = res
        log(f"  {tid} {desc}: n={n_sel:,} mean net {res['mean_sel']:+.4f} CI{[round(v, 4) for v in boot['sel_mean_ci']]} "
            f"P(TP)={res['p_tp']:.3f} weeks>0 {res['weeks_positive']}/{len(wk)} p(one-sided)={p_one:.4f}")
        if rep_mode:
            if desc not in orig:
                log(f"REFUSED: rule {desc!r} was never registered (no new trials in a replication)")
                return 2
            ids[tid] = orig[desc]
        else:
            ids[tid] = reg.register("E", f"E {desc} @K={K}", "a LightGBM combination of all PIT features selects "
                                    f"a slice with positive realised 2-SOL net", specs[tid], zone="discovery",
                                    metrics=res, p_value=p_one)
    if rep_mode:
        q = rp.replicated_q(reg, {i_: results[t_]["p_one_sided"] for t_, i_ in ids.items()})
    else:
        q = reg.bh_qvalues()
    log("\n  verdicts (CI>0 AND >=60% weeks positive AND > floor baseline AND q<0.10):")
    per_trial = {}
    before = reg.trials()
    for tid, i_ in ids.items():
        r = results[tid]
        ok = (r["sel_mean_ci"][0] > 0 and r["weeks_positive"] >= 0.6 * r["weeks_with_selection"]
              and r["mean_sel"] > base_mean and q[i_] < 0.10)
        st = "passed_discovery" if ok else "screened_out"
        if rep_mode:
            per_trial[i_] = {"status": st, "p_value": r["p_one_sided"], "q_bh": q[i_], "n_selected": r["n_selected"],
                             "mean_sel": r["mean_sel"], "sel_mean_ci": r["sel_mean_ci"],
                             "weeks_positive": r["weeks_positive"], "weeks_with_selection": r["weeks_with_selection"]}
            was = before[i_].get("status")
        else:
            reg.update(i_, status=st, q_bh=q[i_])
            was = None
        log(f"    {i_} {r['rule']}: q={q[i_]:.4f} -> {'PASSED discovery' if ok else 'screened out'}"
            f"{'' if was is None else ('  [v2: same]' if was == st else f'  [v2: {was} -> CHANGED]')}")
    # the E000 entry set (pred > 0, out of fold) -- the fixed starting set of families F and H
    e0 = pred[oof] > thr["E000"][oof]
    entry = {"n": int(e0.sum()), "mean": float(np.nanmean(yo[e0])) if e0.any() else float("nan"),
             "mints_sha256": hashlib.sha256("\n".join(sorted(
                 u["mint"].to_numpy()[oof][e0].astype(str))).encode()).hexdigest()}
    log(f"  E000 entry set: n={entry['n']:,} mean net {entry['mean']:+.4f} sha256 {entry['mints_sha256'][:16]}")
    res_out = {"results": results, "ids": ids, "features": cols, "baseline_mean": base_mean, "all_mean": allm,
               "e000_entry": entry, "set": rp.set_name_of(a.sets), "replication": rep_mode}
    if rep_mode:
        res_out["diagnostic"] = rp.record(reg, "E", per_trial, {"all_mean": allm, "baseline_mean": base_mean,
                                                                "e000_entry": entry})
        log(f"  replication recorded as {res_out['diagnostic']}")
    (out / "results.json").write_text(json.dumps(res_out, indent=1, default=float), encoding="utf-8")
    log(f"  done in {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
