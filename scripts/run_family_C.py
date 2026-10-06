#!/usr/bin/env python3
"""D129: family C (wallet microstructure, creator history, regime) --
registered single-feature trials on the DISCOVERY zone, research set v2.

Fixed BEFORE the first run (docs/DECISIONS.md D129), learning from D128:
  universe  identical to family A (K=10, non-mayhem, standard curve, status
            not CENS/NOENTRY)
  target    o_base_net (2 SOL, 1 s, +60/-30, 30 min, anchored model)
  control   worst_case_pit = worst_case_net(q_now) -- the curve-floor factor
            that explained family A (T004, rho 0.443 with the target);
            computed from the state AT the decision (point-in-time)
  trials    C000..C016 = the 17 features in FEATURES, nothing else
  stats     PARTIAL association controlling for worst_case_pit
            (tape/trials.partial_permutation_null, 1000 within-block
            permutations, 6 chronological blocks, max-statistic p_fwer);
            quintile rule fitted on the first half, evaluated on the second
  pass      p_fwer < 0.05 AND BH q < 0.10 (all non-diagnostic trials) AND
            partial rho has the same sign in >= 5 of 6 blocks AND
            ** the rule's OWN second-half mean net has a 95% CI > 0 **
            (D128 amendment: improving on a losing baseline is not enough)
  reported  also the curve-position-matched improvement (worst-case deciles)
Refuses to run twice.
D133: `--replicate` re-runs the SAME specs on research set v3 (corrected
dedup) -- no new trials; results mapped onto C000..C016, output in
trials/family_C_v3.
    python scripts\\run_family_C.py --replicate

    python scripts\\run_family_C.py
"""

from __future__ import annotations

import argparse
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
from tape import replication as rp  # noqa: E402
from tape.registry import Registry, spec_hash  # noqa: E402

FEATURES = ["unique_buyers", "unique_sellers", "repeat_buyer_share", "first_slot_buyers", "first_slot_sol",
            "same_slot_max_buyers", "top1_hold_share", "top5_hold_share", "dev_buy_sol", "dev_sold_sol",
            "dev_hold_share", "largest_buy_sol", "median_buy_sol", "creator_prior_launches",
            "creator_prior_grad_rate", "creator_launches_24h", "regime_creates_prev_hour"]
K = 10
TARGET = "o_base_net"
N_BLOCKS = 6
N_PERM = 1000
QUINTILE = 0.2
UNIVERSE = "K=10, non-mayhem, standard curve, status not CENS/NOENTRY, discovery"
CONTROL = "worst_case_pit = worst_case_net(q_now, 2 SOL, token fee ratios)"


def worst_case_net(q0, size=2.0, fee_b=0.0125, fee_s=0.0125, k=30.0 * 1.073e9, v=30.0):
    sol_in = size / (1 + fee_b)
    q_after_them = k / (k / (q0 + sol_in) + k / v - k / q0)
    return (q_after_them - v) / (1 + fee_s) / size - 1.0


def load(sets_dir: Path, log):
    import pandas as pd
    import pyarrow.parquet as pq
    cols = ["mint", "K", "create_ts", "is_mayhem", "chk_curve_fitted", "o_base_status", TARGET,
            "q_now", "fee_buy_ratio", "fee_sell_ratio"] + FEATURES
    parts = sorted(sets_dir.glob("part-b*.parquet"))
    df = pd.concat([pq.read_table(p, columns=cols).to_pandas() for p in parts], ignore_index=True)
    log(f"  loaded {len(df):,} rows from {len(parts)} parts")
    return df


def main() -> int:
    holder = []
    try:
        return _main(holder)
    finally:
        for f in holder:
            f.close()


def _main(holder) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sets", default=None, help="default sets_v2 (sets_v3 with --replicate)")
    ap.add_argument("--zones", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--out", default=None, help="default trials/family_C (family_C_v3 with --replicate)")
    ap.add_argument("--replicate", action="store_true", help="D133: same specs on sets_v3, no new trials")
    a = ap.parse_args()
    rep_mode = a.replicate
    a.sets = a.sets or (r"E:\tape_research\sets_v3\discovery" if rep_mode else r"E:\tape_research\sets_v2\discovery")
    a.out = a.out or (r"E:\tape_research\trials\family_C_v3" if rep_mode else r"E:\tape_research\trials\family_C")
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
    specs = {f: {"family": "C", "feature": f, "K": K, "target": TARGET, "universe": UNIVERSE,
                 "control": CONTROL, "rule": f"top/bottom {QUINTILE} quantile fitted on first half",
                 "gate": "p_fwer<0.05, q<0.10, >=5/6 blocks (partial), rule mean net CI>0",
                 "n_perm": N_PERM, "n_blocks": N_BLOCKS, "set": "sets_v2"} for f in FEATURES}
    if rep_mode:
        orig = rp.original_ids(reg, "C", "feature")
        if rp.already_replicated(reg, "C"):
            log("REFUSED: family C was already replicated on sets_v3 (see registry).")
            return 2
        if not orig:
            log("REFUSED: nothing to replicate -- no original family C trials in the registry")
            return 2
    elif any(spec_hash(s) in {t.get("spec_hash") for t in reg.trials().values()} for s in specs.values()):
        log("REFUSED: family C trials with these specs are already registered (no re-rolls).")
        return 2

    t0 = time.time()
    log(f"\n=== run_family_C {datetime.now(timezone.utc).isoformat()}{' REPLICATION (D133)' if rep_mode else ''} "
        f"sets={a.sets} ===")
    disc = set(zn.load_zone(a.zones, "discovery"))
    df = load(Path(a.sets), log)
    df = df[df["K"] == K]
    if not set(df["mint"]).issubset(disc):
        log("REFUSED: research set contains mints outside the frozen discovery zone")
        return 3
    u = df[(df["is_mayhem"] == 0) & (~df["chk_curve_fitted"].astype(bool))
           & (~df["o_base_status"].isin(["CENS", "NOENTRY"]))].sort_values("create_ts").reset_index(drop=True)
    fb = u["fee_buy_ratio"].fillna(0.0125).to_numpy(float)
    fs = np.where(np.isfinite(u["fee_sell_ratio"].to_numpy(float)), u["fee_sell_ratio"].to_numpy(float), fb)
    c = worst_case_net(u["q_now"].to_numpy(float), 2.0, fb, fs)
    y = u[TARGET].to_numpy(float)
    blocks = tr.block_ids(u["create_ts"].to_numpy(), N_BLOCKS)
    first = np.arange(len(u)) < len(u) // 2
    day = u["create_ts"].to_numpy() // tr.DAY_MS
    strata = pd.qcut(pd.Series(c).rank(method="first"), 10, labels=False).to_numpy()
    feats = {f: u[f].to_numpy(float) for f in FEATURES}
    log(f"  universe {len(u):,} tokens; rho(control, target)={tr.spearman(c, y):+.4f}")
    log(f"  finite share per feature: { {f: round(float(np.isfinite(v).mean()), 3) for f, v in feats.items()} }")

    log(f"  partial permutation null: {N_PERM} x {len(FEATURES)} features ...")
    t1 = time.time()
    null = tr.partial_permutation_null(feats, y, c, blocks, N_PERM, seed=0,
                                       progress=lambda p: log(f"    {p}/{N_PERM} elapsed={time.time() - t1:.0f}s "
                                                              f"ETA~{(time.time() - t1) / p * (N_PERM - p):.0f}s"))
    obs = null["_obs"]
    tested = [f for f in FEATURES if f in obs]
    max_null = np.max(np.vstack([null[f] for f in tested]), axis=0)

    results = {}
    for f in FEATURES:
        if f not in obs:
            log(f"  {f:24s} too few finite rows -- not tested")
            continue
        x = feats[f]
        prho = obs[f]
        p_raw = (1 + np.sum(null[f] >= abs(prho))) / (1 + N_PERM)
        p_fwer = (1 + np.sum(max_null >= abs(prho))) / (1 + N_PERM)
        brho = [tr.partial_spearman(x[blocks == b], y[blocks == b], c[blocks == b]) for b in range(N_BLOCKS)]
        same = int(sum(1 for r in brho if np.sign(r) == np.sign(prho)))
        raw_rho = tr.spearman(x, y)
        rule = tr.quintile_rule(x, y, first, QUINTILE)
        sel = rule.pop("sel_mask_second")
        boot = tr.day_block_bootstrap(y[~first], sel, day[~first], 1000, seed=1)
        mdiff = tr.matched_diff(y[~first], sel, strata[~first])
        mci = tr.matched_bootstrap(y[~first], sel, strata[~first], day[~first], 300, seed=2)
        results[f] = {"partial_rho": prho, "raw_rho": raw_rho, "n": null["_n"][f], "p_raw": float(p_raw),
                      "p_fwer": float(p_fwer), "block_partial_rho": brho, "blocks_same_sign": same,
                      **rule, **boot, "matched_diff": mdiff, "matched_diff_ci": mci}
        log(f"  {f:24s} n={null['_n'][f]:,} partial={prho:+.4f} (raw {raw_rho:+.4f}) p_fwer={p_fwer:.4f} "
            f"blocks {same}/{N_BLOCKS} | rule {rule['direction']:+.0f}: sel mean {rule['mean_sel_second']:+.4f} "
            f"CI{[round(v, 4) for v in boot['sel_mean_ci']]}; matched diff {mdiff:+.4f} CI{[round(v, 4) for v in mci]}")

    ids = {}
    if rep_mode:
        untested = sorted(set(orig) & set(FEATURES) - set(results))
        if untested:
            log(f"  NOTE: original trials not testable on this set (too few finite rows): {untested}")
        unknown = sorted(set(results) - set(orig))
        if unknown:
            log(f"REFUSED: features tested now but never registered: {unknown} (no new trials in a replication)")
            return 2
        ids = {f: orig[f] for f in results}
        q = rp.replicated_q(reg, {ids[f]: r["p_raw"] for f, r in results.items()})
    else:
        for f, r in results.items():
            ids[f] = reg.register("C", f"C {f} @K={K}", f"{f} at the 10th bar predicts realised 2-SOL net, "
                                  f"beyond curve position", specs[f], zone="discovery",
                                  metrics=r, p_value=r["p_raw"])
        q = reg.bh_qvalues()
    log("\n  verdicts (p_fwer<0.05 AND q<0.10 AND >=5/6 blocks AND rule mean net CI > 0):")
    per_trial = {}
    before = reg.trials()
    for f, r in results.items():
        ok = r["p_fwer"] < 0.05 and q[ids[f]] < 0.10 and r["blocks_same_sign"] >= 5 and r["sel_mean_ci"][0] > 0
        assoc = r["p_fwer"] < 0.05 and q[ids[f]] < 0.10 and r["blocks_same_sign"] >= 5
        st = "passed_discovery" if ok else "screened_out"
        if rep_mode:
            per_trial[ids[f]] = {"status": st, "p_value": r["p_raw"], "q_bh": q[ids[f]], "p_fwer": r["p_fwer"],
                                 "partial_rho": r["partial_rho"], "mean_sel_second": r["mean_sel_second"],
                                 "sel_mean_ci": r["sel_mean_ci"], "association": bool(assoc)}
            was = before[ids[f]].get("status")
        else:
            reg.update(ids[f], status=st, q_bh=q[ids[f]],
                       notes=("association beyond curve position, rule not profitable" if (assoc and not ok) else ""))
            was = None
        log(f"    {ids[f]} {f:24s} q={q[ids[f]]:.4f} -> {'PASSED discovery' if ok else 'screened out'}"
            f"{'  (association real, rule mean net CI not > 0)' if assoc and not ok else ''}"
            f"{'' if was is None else ('  [v2: same]' if was == st else f'  [v2: {was} -> CHANGED]')}")
    res_out = {"results": results, "ids": ids, "q": {f: q[i] for f, i in ids.items()},
               "set": rp.set_name_of(a.sets), "replication": rep_mode}
    if rep_mode:
        res_out["diagnostic"] = rp.record(reg, "C", per_trial, {"universe": int(len(u)),
                                                                "n_association": int(sum(v["association"] for v in per_trial.values()))})
        log(f"  replication recorded as {res_out['diagnostic']}")
    (out / "results.json").write_text(json.dumps(res_out, indent=1, default=float), encoding="utf-8")
    log(f"  done in {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
