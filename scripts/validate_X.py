#!/usr/bin/env python3
"""D142: the ONE look at the VALIDATION zone for the frozen family-X candidates.

Reads trials/family_X/candidates_registered.json (sha256-checked against the
registry), re-creates each candidate's entries on the validation weeks with the
same protocol (weekly walk-forward: train on everything before the week minus a
1-day embargo -- discovery + earlier validation weeks; calibration-week
thresholds), replays the candidate's exit on the validation paths (2 slots,
0.001 SOL per transaction) and applies the gate:
  mean net day-block 95% CI > 0  AND  one-sided p < 0.05 / (number of candidates)
  AND  >= 60% of validation weeks positive
Robustness (reported only): 1 and 4 slots. Writes validation_done.json before
the outcome statistics are computed; a second run is refused.

    python scripts\\validate_X.py
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

import search_X as sx  # noqa: E402
from tape import trials as tr  # noqa: E402
from tape.fastexit import exit_grid  # noqa: E402
from tape.registry import Registry  # noqa: E402

ROBUST_SLOTS = (1, 4)


def walk_forward_weeks(X, y, ts, week, test_weeks, fit, log, tag):
    import run_family_E as rfe
    pred = np.full(len(y), np.nan)
    cal_sorted = {}
    t1 = time.time()
    for i, w in enumerate(test_weeks, start=1):
        start_w = w * rfe.WEEK_MS + rfe.MONDAY0
        test = week == w
        tr_b = ts < start_w - rfe.EMBARGO_MS
        cal = week == (w - 1)
        tr_a = ts < (start_w - rfe.WEEK_MS) - rfe.EMBARGO_MS
        assert ts[tr_b].max() < start_w - rfe.EMBARGO_MS and not np.any(tr_a & cal)
        pred[test] = fit(X[tr_b], y[tr_b])(X[test])
        cal_sorted[int(w)] = np.sort(fit(X[tr_a], y[tr_a])(X[cal])) if cal.any() else np.array([np.inf])
        el = time.time() - t1
        log(f"    [{tag}] validation week {i}/{len(test_weeks)} elapsed={el:.0f}s "
            f"ETA~{el / i * (len(test_weeks) - i):.0f}s")
    return pred, cal_sorted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sets", default=r"E:\tape_research\sets_v3")
    ap.add_argument("--paths", default=r"E:\tape_research\paths_v1\validation")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--xdir", default=r"E:\tape_research\trials\family_X")
    a = ap.parse_args()
    import pandas as pd
    import run_family_E as rfe

    xdir = Path(a.xdir)
    lf = open(xdir / "validation_log.txt", "a", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    try:
        t0 = time.time()
        done = xdir / "validation_done.json"
        if done.exists():
            log(f"REFUSED: the validation zone was already looked at ({done.read_text(encoding='utf-8')[:200]})")
            return 2
        blob = (xdir / "candidates.json").read_text(encoding="utf-8")
        sha = hashlib.sha256(blob.encode()).hexdigest()
        if sha != (xdir / "candidates.sha256").read_text(encoding="utf-8").strip():
            log("REFUSED: candidates.json does not match its frozen sha256")
            return 3
        cands = json.loads((xdir / "candidates_registered.json").read_text(encoding="utf-8"))
        reg = Registry(a.registry)
        trials = reg.trials()
        for c in cands:
            t = trials.get(c["trial_id"])
            if not t or t["spec"].get("candidates_sha256") != sha:
                log(f"REFUSED: {c['trial_id']} is not the registered frozen candidate")
                return 3
        if len(list(Path(a.paths).glob("path-b*.npz"))) < 64:
            log("STOP: validation paths missing -- run build_research_set_v2.py --zone validation and "
                "build_paths.py --zone validation first")
            return 4
        log(f"\n=== validate_X {datetime.now(timezone.utc).isoformat()} candidates sha {sha[:16]} ===")
        fit, model_name = rfe.make_model()
        meta = pd.concat([sx.read_meta(a.paths, b).assign(bkt=b) for b in range(64)], ignore_index=True)
        arrs = {}
        preds = {}
        for K, tg in sorted({(c["config"]["K"], c["config"]["target"]) for c in cands}):
            ud = sx.load_universe(Path(a.sets) / "discovery", K)
            uv = sx.load_universe(Path(a.sets) / "validation", K)
            if ud[1] != uv[1]:
                log("STOP: feature columns differ between zones")
                return 5
            X = np.concatenate([ud[2], uv[2]])
            y = np.concatenate([sx.target_values(ud[0], tg), sx.target_values(uv[0], tg)])
            ts = np.concatenate([ud[4], uv[4]])
            week = np.concatenate([ud[5], uv[5]])
            is_val = np.r_[np.zeros(len(ud[4]), bool), np.ones(len(uv[4]), bool)]
            test_weeks = sorted(set(week[is_val].tolist()))
            pred, cal = walk_forward_weeks(X, y, ts, week, test_weeks, fit, log, f"K{K} {tg}")
            preds[(K, tg)] = {"mint": uv[0]["mint"].to_numpy(), "pred": pred[is_val], "week": week[is_val],
                              "day": uv[6], "cal": cal}
        done.write_text(json.dumps({"utc": datetime.now(timezone.utc).isoformat(), "candidates_sha256": sha}),
                        encoding="utf-8")
        log("  validation look recorded (validation_done.json); computing outcomes ...")
        n_c = len(cands)
        results = {}
        for c in cands:
            cfg = c["config"]
            P = preds[(cfg["K"], cfg["target"])]
            sel = sx.selection(P["pred"], P["week"], P["cal"], cfg["q"])
            mints = P["mint"][sel]
            dsel = dict(zip(mints, P["day"][sel]))
            mk = meta[(meta["K"] == cfg["K"]) & meta["mint"].isin(set(mints))]
            out = {L: [] for L in (sx.LATENCY_SLOTS, *ROBUST_SLOTS)}
            days = []
            for r in mk.itertuples(index=False):
                if r.bkt not in arrs:
                    with np.load(Path(a.paths) / f"path-b{r.bkt:02d}.npz") as v:
                        arrs[r.bkt] = (v["rel_ts"], v["slot_rel"], v["q"])
                rel, slo, qq = arrs[r.bkt]
                o, ln = int(r.offset), int(r.length)
                for L in out:
                    g = exit_grid(rel[o:o + ln].astype(np.int64), slo[o:o + ln].astype(np.int64),
                                  qq[o:o + ln].astype(float), int(r.grad), float(r.k), float(r.fee_b), float(r.fee_s),
                                  cfg["size"], L, sx.TIP_SOL, np.array([cfg["tp"]]), np.array([cfg["sl"]]),
                                  np.array([int(cfg["h_min"] * 60_000)]))
                    out[L].append(float(g.reshape(-1)[0]))
                days.append(int(dsel[r.mint]))
            y = np.array(out[sx.LATENCY_SLOTS])
            d = np.array(days)
            ok = np.isfinite(y)
            res = {"n": int(ok.sum())}
            if ok.sum() >= 30:
                boot = tr.day_block_bootstrap(y[ok], np.ones(int(ok.sum()), bool), d[ok], 2000, seed=61)
                rng = np.random.default_rng(62)
                du = np.unique(d[ok])
                idx = [np.flatnonzero(d[ok] == x) for x in du]
                yy = y[ok]
                ms = np.array([yy[np.concatenate([idx[j] for j in rng.integers(0, len(du), len(du))])].mean()
                               for _ in range(2000)])
                wk = pd.Series(yy).groupby((d[ok] * tr.DAY_MS - rfe.MONDAY0) // rfe.WEEK_MS).mean()
                res.update(mean=float(yy.mean()), ci=boot["sel_mean_ci"], p_one_sided=float((1 + np.sum(ms <= 0)) / 2001),
                           weeks_positive=int((wk > 0).sum()), weeks=int(len(wk)),
                           robust={f"{L}slot": float(np.nanmean(out[L])) for L in ROBUST_SLOTS})
                passed = (res["ci"][0] > 0 and res["p_one_sided"] < 0.05 / n_c
                          and res["weeks_positive"] >= 0.6 * res["weeks"])
            else:
                passed = False
            res["passed"] = bool(passed)
            results[c["trial_id"]] = res
            reg.update(c["trial_id"], status="passed_validation" if passed else "failed_validation",
                       validation=res)
            log(f"  {c['trial_id']} {cfg}: " + (f"n={res['n']:,} mean {res['mean']:+.4f} CI{[round(v, 4) for v in res['ci']]} "
                                                f"p={res['p_one_sided']:.4f} weeks>0 {res['weeks_positive']}/{res['weeks']} "
                                                f"| 1 slot {res['robust']['1slot']:+.4f} 4 slots {res['robust']['4slot']:+.4f}"
                                                if "mean" in res else f"n={res['n']} (too few)")
                + f" -> {'PASSED validation' if passed else 'failed'}")
        (xdir / "validation_results.json").write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
        n_pass = sum(r["passed"] for r in results.values())
        log(f"  passed: {n_pass} of {n_c}; done in {time.time() - t0:.0f}s")
        log("  NEXT: " + ("pre-register the survivors and run the single SEALED look (Registry.open_sealed)."
                          if n_pass else "nothing survived validation -- final NO-GO stands (D139)."))
        return 0
    finally:
        lf.close()


if __name__ == "__main__":
    raise SystemExit(main())
