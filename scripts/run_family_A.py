#!/usr/bin/env python3
"""D127: family A (tempo / age / curve progress) -- registered single-feature
trials on the DISCOVERY zone, research set v2.

Everything below is fixed BEFORE the first run (docs/DECISIONS.md D127):
  universe  K = 10 (decision at the 10th dollar bar, <= 60 min after create),
            non-mayhem tokens (flag known at create), standard curve,
            outcome status not CENS / NOENTRY
  target    o_base_net: realised net return of 2 SOL, 1 s latency,
            TP +60% / SL -30% / 30 min, anchored curve model (D126)
  trials    A000..A006 = the 7 features in FEATURES, nothing else
  stats     tape/trials.py; 1000 within-block permutations (6 equal-count
            chronological blocks); quintile rule fitted on the first half,
            evaluated on the second half; day-block bootstrap
  pass      p_fwer < 0.05 AND BH q < 0.10 (all non-diagnostic trials in the
            registry) AND same sign in >= 5 of 6 blocks AND the second-half
            rule's improvement over all second-half rows has a 95% CI > 0
A trial that passes goes to VALIDATION; passing does not mean profitable --
the rule's own mean net (and its CI) is reported next to it.

Refuses to run twice (the registry already holds these specs).

D133: `--replicate` re-runs the SAME specs on research set v3 (corrected
dedup) -- no new trials; results mapped onto A000..A006 (tape/replication.py),
output in trials/family_A_v3.

    python scripts\\run_family_A.py
    python scripts\\run_family_A.py --replicate
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

from tape import trials as tr  # noqa: E402
from tape import zones as zn  # noqa: E402
from tape import replication as rp  # noqa: E402
from tape.registry import Registry, spec_hash  # noqa: E402

FEATURES = ["secs_since_create", "swaps_per_min", "q_gain_sol", "net_flow_sol",
            "ret_since_first", "drawdown_from_peak", "buy_share_sol"]
K = 10
TARGET = "o_base_net"
N_BLOCKS = 6
N_PERM = 1000
QUINTILE = 0.2
UNIVERSE = "K=10, non-mayhem, standard curve, status not CENS/NOENTRY, discovery"


def load_universe(sets_dir: Path, ks, log):
    import pandas as pd
    import pyarrow.parquet as pq
    cols = ["mint", "K", "create_ts", "decision_ts", "is_mayhem", "chk_curve_fitted", "chk_chain_match",
            "outcome_usable", "o_base_status", "o_base_net"] + FEATURES
    parts = sorted(sets_dir.glob("part-b*.parquet"))
    df = pd.concat([pq.read_table(p, columns=cols).to_pandas() for p in parts], ignore_index=True)
    df = df[df["K"].isin(ks)]
    log(f"  loaded {len(df):,} rows (K in {ks}) from {len(parts)} parts")
    return df


def main() -> int:
    """Wrapper: always closes the log (Windows cannot delete an open file)."""
    lf_holder = []
    try:
        return _main(lf_holder)
    finally:
        for f in lf_holder:
            f.close()


def _main(lf_holder) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sets", default=None, help="default sets_v2 (sets_v3 with --replicate)")
    ap.add_argument("--zones", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--out", default=None, help="default trials/family_A (family_A_v3 with --replicate)")
    ap.add_argument("--replicate", action="store_true", help="D133: same specs on sets_v3, no new trials")
    a = ap.parse_args()
    rep_mode = a.replicate
    a.sets = a.sets or (r"E:\tape_research\sets_v3\discovery" if rep_mode else r"E:\tape_research\sets_v2\discovery")
    a.out = a.out or (r"E:\tape_research\trials\family_A_v3" if rep_mode else r"E:\tape_research\trials\family_A")
    import pandas as pd

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lf = open(out / "log.txt", "a", encoding="utf-8")
    lf_holder.append(lf)

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    reg = Registry(a.registry)
    specs = {f: {"family": "A", "feature": f, "K": K, "target": TARGET, "universe": UNIVERSE,
                 "rule": f"top/bottom {QUINTILE} quantile, fitted on first half", "n_perm": N_PERM,
                 "n_blocks": N_BLOCKS, "set": "sets_v2"} for f in FEATURES}
    existing = {t.get("spec_hash") for t in reg.trials().values()}
    if rep_mode:
        orig = rp.original_ids(reg, "A", "feature")
        if rp.already_replicated(reg, "A"):
            log("REFUSED: family A was already replicated on sets_v3 (see registry).")
            return 2
        if set(FEATURES) - set(orig):
            log(f"REFUSED: nothing to replicate -- original trials missing for {sorted(set(FEATURES) - set(orig))}")
            return 2
    elif any(spec_hash(s) in existing for s in specs.values()):
        log("REFUSED: family A trials with these specs are already registered (no re-rolls).")
        return 2

    t0 = time.time()
    log(f"\n=== run_family_A {datetime.now(timezone.utc).isoformat()}{' REPLICATION (D133)' if rep_mode else ''} "
        f"sets={a.sets} ===")
    disc = set(zn.load_zone(a.zones, "discovery"))
    df = load_universe(Path(a.sets), [5, 10, 20], log)
    if not set(df["mint"]).issubset(disc):
        log("REFUSED: research set contains mints outside the frozen discovery zone")
        return 3

    # -- universe accounting (all K, for the record) ------------------------
    def universe(d):
        return d[(d["is_mayhem"] == 0) & (~d["chk_curve_fitted"].astype(bool))
                 & (~d["o_base_status"].isin(["CENS", "NOENTRY"]))]

    base_rec = {}
    for k in (5, 10, 20):
        d = df[df["K"] == k]
        u = universe(d)
        base_rec[k] = {"rows": int(len(d)), "mayhem": int((d["is_mayhem"] == 1).sum()),
                       "mayhem_nan": int(d["is_mayhem"].isna().sum()),
                       "fitted_nonmayhem": int(((d["is_mayhem"] == 0) & d["chk_curve_fitted"].astype(bool)).sum()),
                       "cens_or_noentry": int(d["o_base_status"].isin(["CENS", "NOENTRY"]).sum()),
                       "universe": int(len(u)), "p_tp": float((u["o_base_status"] == "TP").mean()),
                       "mean_net": float(u[TARGET].mean()), "median_net": float(u[TARGET].median()),
                       "chain_lt_099_in_universe": float((u["chk_chain_match"] < 0.99).mean())}
        log(f"  K={k:2d} rows={len(d):,} -> universe {len(u):,}: P(TP)={base_rec[k]['p_tp']:.4f} "
            f"mean net={base_rec[k]['mean_net']:+.4f} median={base_rec[k]['median_net']:+.4f} "
            f"(mayhem {base_rec[k]['mayhem']:,}, fitted non-mayhem {base_rec[k]['fitted_nonmayhem']:,}, "
            f"CENS/NOENTRY {base_rec[k]['cens_or_noentry']:,}, chain<0.99 inside {base_rec[k]['chain_lt_099_in_universe']:.3%})")
    if not rep_mode:
        reg.register("T", "T003 zone-wide baselines (enter every token), sets_v2",
                     "reference for every trial: no selection", {"universe": UNIVERSE, "set": "sets_v2"},
                     zone="discovery", status="diagnostic", metrics={str(k): v for k, v in base_rec.items()})

    u = universe(df[df["K"] == K]).sort_values("create_ts").reset_index(drop=True)
    y = u[TARGET].to_numpy(float)
    blocks = tr.block_ids(u["create_ts"].to_numpy(), N_BLOCKS)
    first = np.arange(len(u)) < len(u) // 2
    day = (u["create_ts"].to_numpy() // tr.DAY_MS)
    feats = {f: u[f].to_numpy(float) for f in FEATURES}
    nan_share = {f: float(np.mean(~np.isfinite(v))) for f, v in feats.items()}
    log(f"\n  family A universe: {len(u):,} tokens, {len(np.unique(day))} days; NaN share per feature {nan_share}")

    log(f"  permutation null: {N_PERM} within-block shuffles x {len(FEATURES)} features ...")
    t1 = time.time()
    null = tr.permutation_null(feats, y, blocks, N_PERM, seed=0,
                               progress=lambda p: log(f"    {p}/{N_PERM}  elapsed={time.time() - t1:.0f}s "
                                                      f"ETA~{(time.time() - t1) / p * (N_PERM - p):.0f}s"))
    m = np.isfinite(y)
    for v in feats.values():
        m &= np.isfinite(v)
    max_null = np.max(np.vstack([null[f] for f in FEATURES]), axis=0)

    results = {}
    for f in FEATURES:
        x = feats[f]
        rho = tr.spearman(x[m], y[m])
        p_raw = (1 + np.sum(null[f] >= abs(rho))) / (1 + N_PERM)
        p_fwer = (1 + np.sum(max_null >= abs(rho))) / (1 + N_PERM)
        brho = [tr.spearman(x[blocks == b], y[blocks == b]) for b in range(N_BLOCKS)]
        same = int(sum(1 for r in brho if np.sign(r) == np.sign(rho)))
        rule = tr.quintile_rule(x, y, first, QUINTILE)
        sel = rule.pop("sel_mask_second")
        boot = tr.day_block_bootstrap(y[~first], sel, day[~first], 2000, seed=1)
        results[f] = {"rho": rho, "p_raw": float(p_raw), "p_fwer": float(p_fwer), "block_rho": brho,
                      "blocks_same_sign": same, **rule, **boot}
        log(f"  {f:20s} rho={rho:+.4f} p_raw={p_raw:.4f} p_fwer={p_fwer:.4f} blocks {same}/{N_BLOCKS} | "
            f"rule dir={rule['direction']:+.0f}: sel mean net {rule['mean_sel_second']:+.4f} "
            f"CI{[round(c, 4) for c in boot['sel_mean_ci']]} vs all {rule['mean_all_second']:+.4f}; "
            f"diff CI{[round(c, 4) for c in boot['diff_ci']]} (n_sel={rule['n_selected']:,})")

    ids = {}
    if rep_mode:
        ids = {f: orig[f] for f in FEATURES}
        q = rp.replicated_q(reg, {ids[f]: results[f]["p_raw"] for f in FEATURES})
    else:
        for f in FEATURES:
            r = results[f]
            ids[f] = reg.register("A", f"A {f} @K={K}", f"{f} at the 10th bar predicts the realised 2-SOL net return",
                                  specs[f], zone="discovery", status="registered",
                                  metrics={k: v for k, v in r.items()}, p_value=r["p_raw"])
        q = reg.bh_qvalues()
    log("\n  verdicts (pass = p_fwer<0.05 AND q<0.10 AND >=5/6 blocks AND diff CI > 0):")
    per_trial = {}
    before = reg.trials()
    for f in FEATURES:
        r = results[f]
        ok = (r["p_fwer"] < 0.05 and q[ids[f]] < 0.10 and r["blocks_same_sign"] >= 5 and r["diff_ci"][0] > 0)
        st = "passed_discovery" if ok else "screened_out"
        if rep_mode:
            per_trial[ids[f]] = {"status": st, "p_value": r["p_raw"], "q_bh": q[ids[f]], "p_fwer": r["p_fwer"],
                                 "rho": r["rho"], "mean_sel_second": r["mean_sel_second"], "diff_ci": r["diff_ci"]}
            was = before[ids[f]].get("status")
        else:
            reg.update(ids[f], status=st, q_bh=q[ids[f]])
            was = None
        log(f"    {ids[f]} {f:20s} q={q[ids[f]]:.4f} -> {'PASSED discovery' if ok else 'screened out'}"
            f"{'  (rule mean net still <= 0)' if ok and r['mean_sel_second'] <= 0 else ''}"
            f"{'' if was is None else ('  [v2: same]' if was == st else f'  [v2: {was} -> CHANGED]')}")
    res_out = {"baselines": base_rec, "results": results, "ids": ids, "q": {f: q[ids[f]] for f in FEATURES},
               "set": rp.set_name_of(a.sets), "replication": rep_mode}
    if rep_mode:
        res_out["diagnostic"] = rp.record(reg, "A", per_trial, {"universe_K10": base_rec[K]})
        log(f"  replication recorded as {res_out['diagnostic']}")
    (out / "results.json").write_text(json.dumps(res_out, indent=1, default=float), encoding="utf-8")
    log(f"  done in {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
