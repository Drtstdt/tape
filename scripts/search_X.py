#!/usr/bin/env python3
"""D142: family X -- brute-force search over entry selection x model target x
size x exit, with an honest correction for the size of the search.

Requested by the user (2026-10-05) AFTER the pre-registered stop rule (D135)
had been reached; documented as an explicit override in D142. Rules that keep
it honest:
  * DISCOVERY zone only; every configuration ever evaluated -- grid and loop --
    is kept, and the inference is over ALL of them:
      - White's Reality Check (day-block bootstrap of the maximum mean, recentred)
      - Probability of Backtest Overfitting (CSCV, 10 day-blocks, 252 splits)
  * realistic execution: delay 2 SLOTS (D140/D141), a fixed 0.001 SOL priority
    fee/tip PER TRANSACTION, the anchored curve model (tape/fastexit.py ==
    simulate_anchored, tested)
  * entries are out-of-fold only (weekly walk-forward, 1-day embargo,
    calibration-week thresholds -- the family-E protocol)
  * the output is a FROZEN list of candidates (the D141 candidate + the top 3
    of the search) written with its sha256 and registered as X000..X003 BEFORE
    the validation zone is touched; scripts/validate_X.py looks at validation
    exactly once.

Search space
  K (decision bar)    5, 10, 20
  model target        net (o_base_net), up30 (max net >= +30%), net_clip ([-0.3, 1])
  selection           pred > 0, or top q of the calibration week, q in
                      0.5% 1% 2% 5% 10% 20% 50% 100%  (+ any q the loop proposes)
  size (SOL)          0.25 0.5 1 2
  take-profit         +10 15 20 30 40 60 100 200 %, none
  stop-loss           -5 10 15 20 30 50 %, none
  horizon (min)       1 2 5 10 20 30
  = 3 x 3 x 9 x 4 x 378 = 122,472 grid configurations, then a refinement loop
  (neighbours of the current best in exit parameters and q) until it stops
  improving or the time budget ends. Everything is resumable from its cache.

    python scripts\\search_X.py --workers 5 --hours 24
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tape import trials as tr  # noqa: E402
from tape.fastexit import NO_SL, NO_TP, exit_grid  # noqa: E402
from tape.registry import Registry  # noqa: E402

KS = (5, 10, 20)
TARGETS = ("net", "up30", "net_clip")
QS = ("pos", 0.005, 0.01, 0.02, 0.05, 0.10, 0.20, 0.50, 1.0)
SIZES = (0.25, 0.5, 1.0, 2.0)
TPS = (0.10, 0.15, 0.20, 0.30, 0.40, 0.60, 1.0, 2.0, NO_TP)
SLS = (-0.05, -0.10, -0.15, -0.20, -0.30, -0.50, NO_SL)
HS_MIN = (1, 2, 5, 10, 20, 30)
LATENCY_SLOTS = 2
TIP_SOL = 0.001
MIN_TRADES_POOL = 300
MIN_TRADES_CAND = 500
D141 = {"K": 10, "target": "net", "q": "pos", "size": 2.0, "tp": 0.30, "sl": -0.30, "h_min": 10.0}
EXITS = list(itertools.product(TPS, SLS, HS_MIN))                     # order of the grid's last axis


def target_values(u, name):
    y = u["o_base_net"].to_numpy(float)
    if name == "net":
        return y
    if name == "up30":
        return (u["o_base_max_net"].to_numpy(float) >= 0.30).astype(float)
    if name == "net_clip":
        return np.clip(y, -0.3, 1.0)
    raise ValueError(name)


def cfg_key(c):
    return (c["K"], c["target"], str(c["q"]), round(c["size"], 4), round(c["tp"], 4), round(c["sl"], 4),
            round(c["h_min"], 3))


# ---------------------------------------------------------------------------
# stage A: out-of-fold predictions per (K, target)
# ---------------------------------------------------------------------------

def load_universe(sets_dir: Path, K: int):
    import pandas as pd
    import pyarrow.parquet as pq
    import run_family_E as rfe
    parts = sorted(sets_dir.glob("part-b*.parquet"))
    df = pd.concat([pq.read_table(p, filters=[("K", "=", K)]).to_pandas() for p in parts], ignore_index=True)
    return rfe.prepare_universe(df)


def walk_forward_any(X, y, ts, week, weeks, fit, log, tag):
    """Family-E protocol; returns OOF predictions and, per test week, the sorted
    calibration-week predictions (threshold for ANY top-q, known before the week)."""
    import run_family_E as rfe
    pred = np.full(len(y), np.nan)
    cal_sorted = {}
    test_weeks = [w for w in weeks if np.sum(weeks < w) >= rfe.MIN_TRAIN_WEEKS]
    t1 = time.time()
    for i, w in enumerate(test_weeks, start=1):
        start_w = w * rfe.WEEK_MS + rfe.MONDAY0
        test = week == w
        tr_b = ts < start_w - rfe.EMBARGO_MS
        cal = week == (w - 1)
        tr_a = ts < (start_w - rfe.WEEK_MS) - rfe.EMBARGO_MS
        assert ts[tr_b].max() < start_w - rfe.EMBARGO_MS and not np.any(tr_a & cal)
        pred[test] = fit(X[tr_b], y[tr_b])(X[test])
        cal_sorted[int(w)] = np.sort(fit(X[tr_a], y[tr_a])(X[cal]))
        el = time.time() - t1
        log(f"    [{tag}] week {i}/{len(test_weeks)} elapsed={el:.0f}s ETA~{el / i * (len(test_weeks) - i):.0f}s")
    return pred, cal_sorted


def selection(pred, week, cal_sorted, q):
    oof = np.isfinite(pred)
    if q == "pos":
        return oof & (pred > 0)
    q = float(q)
    if q >= 1.0:
        return oof
    thr = np.full(len(pred), np.inf)
    for w, cs in cal_sorted.items():
        thr[week == w] = np.quantile(cs, 1 - q)
    return oof & (pred >= thr)


# ---------------------------------------------------------------------------
# stage B: net grid per decision (workers, one bucket each)
# ---------------------------------------------------------------------------

def read_meta(paths_dir, b):
    import pandas as pd
    return pd.read_parquet(Path(paths_dir) / f"meta-b{b:02d}.parquet")


def grid_bucket(task):
    import pandas as pd
    paths_dir, out_dir, b = task
    out = Path(out_dir) / f"grid-b{b:02d}.npy"
    if out.exists():
        return b, "skipped", 0
    meta = read_meta(paths_dir, b)
    with np.load(Path(paths_dir) / f"path-b{b:02d}.npz") as arr:      # closed at once (Windows file locks)
        rel, slo, q = arr["rel_ts"], arr["slot_rel"], arr["q"].astype(float)
    tps, sls = np.array(TPS), np.array(SLS)
    hs = np.array(HS_MIN, dtype=np.int64) * 60_000
    G = np.full((len(meta), len(SIZES), len(EXITS)), np.nan, dtype=np.float32)
    for i, r in enumerate(meta.itertuples(index=False)):
        o, n = int(r.offset), int(r.length)
        for s, size in enumerate(SIZES):
            g = exit_grid(rel[o:o + n].astype(np.int64), slo[o:o + n].astype(np.int64), q[o:o + n], int(r.grad),
                          float(r.k), float(r.fee_b), float(r.fee_s), size, LATENCY_SLOTS, TIP_SOL, tps, sls, hs)
            G[i, s, :] = g.reshape(-1)
    tmp = out.with_suffix(".tmp.npy")
    np.save(tmp, G)
    os.replace(tmp, out)
    return b, "done", len(meta)


# ---------------------------------------------------------------------------
# inference over everything evaluated
# ---------------------------------------------------------------------------

def reality_check(S, N, n_boot=1000, seed=0, chunk=100):
    """White's Reality Check on the maximum mean; S, N: (configs, days)."""
    rng = np.random.default_rng(seed)
    D = S.shape[1]
    m = S.sum(1) / np.maximum(N.sum(1), 1)
    T = float(m.max())
    exceed, tstar = 0, []
    for b0 in range(0, n_boot, chunk):
        B = min(chunk, n_boot - b0)
        W = np.zeros((B, D), dtype=np.float32)
        for i in range(B):
            np.add.at(W[i], rng.integers(0, D, D), 1.0)
        num = W @ S.T
        den = W @ N.T
        mb = num / np.maximum(den, 1)
        t = (mb - m[None, :]).max(1)
        tstar.extend(t.tolist())
        exceed += int(np.sum(t >= T))
    return {"T_max_mean": T, "p_value": (1 + exceed) / (1 + n_boot),
            "null_q95": float(np.quantile(tstar, 0.95))}


def pbo_cscv(S, N, n_blocks=10):
    """Probability of Backtest Overfitting (Bailey et al.), contiguous day blocks."""
    D = S.shape[1]
    edges = np.linspace(0, D, n_blocks + 1).astype(int)
    Sb = np.stack([S[:, edges[i]:edges[i + 1]].sum(1) for i in range(n_blocks)], 1)
    Nb = np.stack([N[:, edges[i]:edges[i + 1]].sum(1) for i in range(n_blocks)], 1)
    lam = []
    for ins in itertools.combinations(range(n_blocks), n_blocks // 2):
        oos = [i for i in range(n_blocks) if i not in ins]
        mi = Sb[:, ins].sum(1) / np.maximum(Nb[:, ins].sum(1), 1)
        mo = Sb[:, oos].sum(1) / np.maximum(Nb[:, oos].sum(1), 1)
        best = int(np.argmax(mi))
        omega = (np.sum(mo < mo[best]) + 0.5) / (len(mo) + 1)
        lam.append(np.log(omega / (1 - omega)))
    lam = np.array(lam)
    return {"pbo": float(np.mean(lam <= 0)), "logit_median": float(np.median(lam)), "splits": len(lam)}


def config_stats(s_row, n_row, day_week, n_boot=1000, seed=7):
    m = s_row.sum() / max(n_row.sum(), 1)
    rng = np.random.default_rng(seed)
    D = len(s_row)
    bs = []
    for _ in range(n_boot):
        idx = rng.integers(0, D, D)
        nn = n_row[idx].sum()
        if nn > 0:
            bs.append(s_row[idx].sum() / nn)
    bs = np.array(bs)
    wk = {}
    for d in range(D):
        if n_row[d] > 0:
            a = wk.setdefault(int(day_week[d]), [0.0, 0.0])
            a[0] += s_row[d]
            a[1] += n_row[d]
    wpos = sum(1 for v in wk.values() if v[0] / v[1] > 0)
    return {"n": int(n_row.sum()), "mean": float(m), "ci": [float(np.quantile(bs, .025)), float(np.quantile(bs, .975))],
            "p_one_sided": float((1 + np.sum(bs <= 0)) / (1 + len(bs))), "weeks_positive": wpos, "weeks": len(wk)}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sets", default=r"E:\tape_research\sets_v3\discovery")
    ap.add_argument("--paths", default=r"E:\tape_research\paths_v1\discovery")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--out", default=r"E:\tape_research\trials\family_X")
    ap.add_argument("--workers", type=int, default=4, help="0 = no process pool (in-process)")
    ap.add_argument("--hours", type=float, default=24.0, help="time budget for the refinement loop")
    ap.add_argument("--max-rounds", type=int, default=400)
    ap.add_argument("--patience", type=int, default=8, help="stop after this many rounds without improvement")
    a = ap.parse_args()
    import pandas as pd

    out = Path(a.out)
    (out / "cache").mkdir(parents=True, exist_ok=True)
    (out / "grid").mkdir(parents=True, exist_ok=True)
    lf = open(out / "log.txt", "a", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    try:
        t0 = time.time()
        reg = Registry(a.registry)
        if (out / "candidates.json").exists() or any(t["family"] == "X" for t in reg.trials().values()):
            log("REFUSED: the family-X search already produced its frozen candidates (no re-rolls).")
            return 2
        log(f"\n=== search_X {datetime.now(timezone.utc).isoformat()} latency={LATENCY_SLOTS} slots "
            f"tip={TIP_SOL} SOL/tx budget={a.hours} h ===")
        import run_family_E as rfe
        fit, model_name = rfe.make_model()

        # ---- stage A ---------------------------------------------------------
        log("\n[A] out-of-fold predictions (weekly walk-forward) per K x target")
        uni = {}
        for K in KS:
            u, cols, X, y, ts, week, day, weeks = load_universe(Path(a.sets), K)
            uni[K] = {"mint": u["mint"].to_numpy(), "week": week, "day": day}
            for tg in TARGETS:
                cp = out / "cache" / f"pred_K{K}_{tg}.npz"
                if cp.exists():
                    log(f"  K={K} {tg}: cached")
                    continue
                pred, cal = walk_forward_any(X, target_values(u, tg), ts, week, weeks, fit, log, f"K{K} {tg}")
                np.savez(cp, pred=pred, **{f"cal_{w}": v for w, v in cal.items()})
            log(f"  K={K}: universe {len(u):,}; elapsed {time.time() - t0:.0f}s")
            del X, u

        # ---- stage B ---------------------------------------------------------
        log("\n[B] exit grid per decision (fastexit, 4 sizes x 378 exits)")
        n_b = len(list(Path(a.paths).glob("path-b*.npz")))
        if n_b < 64:
            log(f"STOP: paths incomplete ({n_b} buckets) -- run build_paths.py --zone discovery first")
            return 4
        t1 = time.time()
        tasks_b = [(a.paths, str(out / "grid"), b) for b in range(64)]

        def _grid_progress(results):
            for i, (b, st, n) in enumerate(results, start=1):
                el = time.time() - t1
                if i % 4 == 0 or i == 64:
                    log(f"  [grid] {i}/64 buckets elapsed={el:.0f}s ETA~{el / i * (64 - i):.0f}s")

        if a.workers <= 0:                       # in-process (tests; also a fallback if a pool cannot start)
            _grid_progress(map(grid_bucket, tasks_b))
        else:
            with ProcessPoolExecutor(max_workers=a.workers) as ex:
                _grid_progress(ex.map(grid_bucket, tasks_b))

        # ---- stage C ---------------------------------------------------------
        log("\n[C] grid evaluation (all K x target x q x size x exit)")
        metas = []
        for b in range(64):
            mb = read_meta(a.paths, b)
            metas.append(mb.assign(bkt=b, row_i=np.arange(len(mb))))
        meta = pd.concat(metas, ignore_index=True)
        all_days = np.unique(np.concatenate([uni[K]["day"] for K in KS]))
        day_pos = {int(d): i for i, d in enumerate(all_days)}
        D = len(all_days)
        day_week = np.array([(int(d) * tr.DAY_MS - rfe.MONDAY0) // rfe.WEEK_MS for d in all_days])
        configs, S_list, N_list = [], [], []
        groups = {}                                                     # (K, target, q, size) -> row index arrays

        def add_block(base_cfg, nets, days_idx, exits):
            """nets: (rows, len(exits)); one config per exit."""
            Sd = np.zeros((nets.shape[1], D), np.float32)
            Nd = np.zeros((nets.shape[1], D), np.float32)
            if len(days_idx):
                order = np.argsort(days_idx, kind="stable")
                dd = days_idx[order]
                nv = nets[order]
                fin = np.isfinite(nv)
                starts = np.flatnonzero(np.r_[True, dd[1:] != dd[:-1]])
                Sd[:, dd[starts]] = np.add.reduceat(np.where(fin, nv, 0.0), starts, axis=0).T
                Nd[:, dd[starts]] = np.add.reduceat(fin.astype(np.float32), starts, axis=0).T
            for j, (tp, sl, h) in enumerate(exits):
                configs.append({**base_cfg, "tp": float(tp), "sl": float(sl), "h_min": float(h)})
            S_list.append(Sd)
            N_list.append(Nd)

        rows_by_K, preds = {}, {}
        for K in KS:
            mk = meta[meta["K"] == K]
            pos_in_u = pd.Series(np.arange(len(uni[K]["mint"])), index=uni[K]["mint"])
            mk = mk[mk["mint"].isin(pos_in_u.index)]
            Gk = np.concatenate([np.load(out / "grid" / f"grid-b{b:02d}.npy")[g["row_i"].to_numpy()]
                                 for b, g in mk.groupby("bkt")])
            mk = pd.concat([g for _, g in mk.groupby("bkt")]).reset_index(drop=True)   # same order as Gk
            ui = pos_in_u.loc[mk["mint"]].to_numpy()
            rows_by_K[K] = (mk, ui)
            dpos = np.array([day_pos[int(d)] for d in uni[K]["day"][ui]])
            for tg in TARGETS:
                with np.load(out / "cache" / f"pred_K{K}_{tg}.npz") as z:
                    cal = {int(k_[4:]): z[k_] for k_ in z.files if k_.startswith("cal_")}
                    pred_u = z["pred"]
                preds[(K, tg)] = (pred_u, cal)
                for q in QS:
                    sel = selection(pred_u[ui], uni[K]["week"][ui], cal, q)
                    idx = np.flatnonzero(sel)
                    groups[(K, tg, str(q), None)] = idx
                    for s, size in enumerate(SIZES):
                        add_block({"K": K, "target": tg, "q": q, "size": size}, np.asarray(Gk[idx, s, :], float),
                                  dpos[idx], EXITS)
                log(f"  K={K} {tg}: {len(QS) * len(SIZES) * len(EXITS):,} configs; total {len(configs):,}; "
                    f"elapsed {time.time() - t0:.0f}s")
            del Gk
        seen = {cfg_key(c) for c in configs}

        def pool_means():
            S = np.concatenate(S_list)
            N = np.concatenate(N_list)
            n = N.sum(1)
            m = np.where(n >= MIN_TRADES_POOL, S.sum(1) / np.maximum(n, 1), -np.inf)
            return S, N, n, m

        S, N, n_tr, m = pool_means()
        best0 = float(m.max())
        log(f"  grid best mean (n>={MIN_TRADES_POOL}): {best0:+.4f}  config {configs[int(np.argmax(m))]}")

        # ---- stage D: refinement loop ---------------------------------------------
        log(f"\n[D] refinement loop (budget {a.hours} h, patience {a.patience} rounds)")
        path_cache = {}

        def paths_for(K):
            """All paths (every K shares the bucket files), native dtypes, loaded once."""
            if not path_cache:
                for b in range(64):
                    with np.load(Path(a.paths) / f"path-b{b:02d}.npz") as v:
                        path_cache[b] = (v["rel_ts"], v["slot_rel"], v["q"])
            return path_cache

        rng = np.random.default_rng(123)
        best, stale, rnd = best0, 0, 0
        t_loop = time.time()
        while rnd < a.max_rounds and stale < a.patience and (time.time() - t_loop) < a.hours * 3600:
            rnd += 1
            order = np.argsort(-m)[:8]
            added = 0
            for ci in order:
                c = configs[int(ci)]
                K = c["K"]
                mk, ui = rows_by_K[K]
                pred_u, cal = preds[(K, c["target"])]
                q_opts = [c["q"]]
                if c["q"] not in ("pos", 1.0):
                    q_opts += [float(np.clip(float(c["q"]) * f, 0.001, 0.99)) for f in (0.7, 1.4)]
                tp0 = c["tp"] if c["tp"] < NO_TP else 3.0
                sl0 = c["sl"] if c["sl"] > NO_SL else -0.8
                tps = np.unique(np.round(np.clip(tp0 * np.exp(rng.normal(0, 0.15, 4)), 0.03, 5.0), 3).tolist() + [c["tp"]])
                sls = np.unique(np.round(np.clip(sl0 * np.exp(rng.normal(0, 0.15, 4)), -0.95, -0.02), 3).tolist() + [c["sl"]])
                hs = np.unique(np.round(np.clip(c["h_min"] * np.exp(rng.normal(0, 0.25, 3)), 0.5, 30.0), 2).tolist()
                               + [c["h_min"]])
                exits = [e for e in itertools.product(tps, sls, hs)]
                P = paths_for(K)
                for qv in q_opts:
                    sel = np.flatnonzero(selection(pred_u[ui], uni[K]["week"][ui], cal, qv))
                    new = [e for e in exits if cfg_key({**c, "q": qv, "tp": e[0], "sl": e[1], "h_min": e[2]}) not in seen]
                    if not new or len(sel) < MIN_TRADES_POOL:
                        continue
                    tps_n = np.unique([e[0] for e in new])
                    sls_n = np.unique([e[1] for e in new])
                    hs_n = np.unique([e[2] for e in new])
                    full = list(itertools.product(tps_n, sls_n, hs_n))
                    nets = np.full((len(sel), len(full)), np.nan)
                    rows = mk.iloc[sel]
                    for r_i, r in enumerate(rows.itertuples(index=False)):
                        rel, slo, qq = P[int(r.bkt)]
                        o, ln = int(r.offset), int(r.length)
                        g = exit_grid(rel[o:o + ln].astype(np.int64), slo[o:o + ln].astype(np.int64),
                                      qq[o:o + ln].astype(float), int(r.grad), float(r.k),
                                      float(r.fee_b), float(r.fee_s), c["size"], LATENCY_SLOTS, TIP_SOL,
                                      tps_n, sls_n, (hs_n * 60_000).astype(np.int64))
                        nets[r_i] = g.reshape(-1)
                    keep = [j for j, e in enumerate(full)
                            if cfg_key({**c, "q": qv, "tp": e[0], "sl": e[1], "h_min": e[2]}) not in seen]
                    dpos = np.array([day_pos[int(d)] for d in uni[K]["day"][ui[sel]]])
                    add_block({"K": K, "target": c["target"], "q": qv, "size": c["size"]}, nets[:, keep], dpos,
                              [full[j] for j in keep])
                    for j in keep:
                        e = full[j]
                        seen.add(cfg_key({**c, "q": qv, "tp": e[0], "sl": e[1], "h_min": e[2]}))
                    added += len(keep)
            S, N, n_tr, m = pool_means()
            cur = float(m.max())
            if cur > best + 1e-4:
                best, stale = cur, 0
            else:
                stale += 1
            el = time.time() - t_loop
            log(f"  round {rnd}: +{added:,} configs (total {len(configs):,}); best mean {cur:+.4f} "
                f"(stale {stale}/{a.patience}); loop elapsed {el / 3600:.2f} h of {a.hours} h")
            if added == 0:
                stale = a.patience

        # ---- stage E: inference ------------------------------------------------
        log(f"\n[E] inference over ALL {len(configs):,} evaluated configurations (n >= {MIN_TRADES_POOL})")
        ok = n_tr >= MIN_TRADES_POOL
        if not ok.any():
            log(f"STOP: no configuration reached {MIN_TRADES_POOL} trades -- nothing to infer, nothing registered.")
            return 5
        So, No = S[ok], N[ok]
        idx_ok = np.flatnonzero(ok)
        rc = reality_check(So, No)
        pbo = pbo_cscv(So, No)
        log(f"  Reality Check: max mean {rc['T_max_mean']:+.4f}; p = {rc['p_value']:.4f} "
            f"(95% of the luck-only maximum excess: {rc['null_q95']:+.4f})")
        log(f"  PBO (CSCV, {pbo['splits']} splits): {pbo['pbo']:.3f}; median logit {pbo['logit_median']:+.2f}")
        top = idx_ok[np.argsort(-m[ok])[:20]]
        log("  top 20 by discovery mean:")
        stats = {}
        for r_i, ci in enumerate(top, start=1):
            st = config_stats(S[ci], N[ci], day_week)
            stats[int(ci)] = st
            c = configs[int(ci)]
            log(f"   {r_i:2d}. K={c['K']} {c['target']:8s} q={c['q']!s:6s} size={c['size']:<4} TP={c['tp']:g} "
                f"SL={c['sl']:g} H={c['h_min']:g}m | n={st['n']:,} mean {st['mean']:+.4f} "
                f"CI{[round(v, 4) for v in st['ci']]} weeks>0 {st['weeks_positive']}/{st['weeks']}")
        # candidates: D141 + top 3 (n >= MIN_TRADES_CAND and >= 60% weeks positive, else by mean)
        d141_i = next(i for i, c in enumerate(configs) if cfg_key(c) == cfg_key(D141))
        cands = [("X000", d141_i, "D141 candidate (pre-specified)")]
        elig = [int(ci) for ci in idx_ok[np.argsort(-m[ok])] if n_tr[ci] >= MIN_TRADES_CAND and int(ci) != d141_i]
        # one candidate per (K, target, q, size) group -- near-duplicate exits of the same
        # trades would waste validation slots; >= 60% positive weeks preferred
        def grp(ci):
            c = configs[ci]
            return (c["K"], c["target"], str(c["q"]), c["size"])
        chosen, used = [], {grp(d141_i)}
        for need_weeks in (True, False):
            for ci in elig:
                if len(chosen) == 3:
                    break
                if grp(ci) in used:
                    continue
                st = stats.get(ci) or config_stats(S[ci], N[ci], day_week)
                if need_weeks and st["weeks_positive"] < 0.6 * st["weeks"]:
                    continue
                chosen.append(ci)
                used.add(grp(ci))
        cands += [(f"X00{j + 1}", ci, "search top") for j, ci in enumerate(chosen)]
        cand_out = []
        for tid_hint, ci, why in cands:
            st = config_stats(S[ci], N[ci], day_week)
            cand_out.append({"slot": tid_hint, "why": why, "config": configs[ci], "discovery": st})
            log(f"  candidate {tid_hint} ({why}): {configs[ci]} | n={st['n']:,} mean {st['mean']:+.4f} "
                f"CI{[round(v, 4) for v in st['ci']]} weeks>0 {st['weeks_positive']}/{st['weeks']}")
        blob = json.dumps({"candidates": cand_out, "latency_slots": LATENCY_SLOTS, "tip_sol": TIP_SOL,
                           "reality_check": rc, "pbo": pbo, "n_configs": len(configs),
                           "n_configs_tested": int(ok.sum()), "model": model_name,
                           "utc": datetime.now(timezone.utc).isoformat()}, indent=1, default=float, sort_keys=True)
        (out / "candidates.json").write_text(blob, encoding="utf-8")
        sha = hashlib.sha256(blob.encode()).hexdigest()
        (out / "candidates.sha256").write_text(sha, encoding="utf-8")
        tid_t = reg.register("T", "family X search summary (D142)", "how much of the best result is search luck?",
                             {"diagnostic": "D142", "candidates_sha256": sha}, zone="discovery", status="diagnostic",
                             metrics={"reality_check": rc, "pbo": pbo, "n_configs": len(configs)})
        for cd in cand_out:
            p = cd["discovery"]["p_one_sided"] if cd["slot"] == "X000" else rc["p_value"]
            tid = reg.register("X", f"X {cd['why']}", "a brute-force-selected configuration is profitable out of sample",
                               {**cd["config"], "latency_slots": LATENCY_SLOTS, "tip_sol": TIP_SOL,
                                "candidates_sha256": sha}, zone="discovery", status="registered",
                               metrics=cd["discovery"], p_value=p,
                               notes="search-wide Reality Check p" if cd["slot"] != "X000" else "own bootstrap p")
            cd["trial_id"] = tid
        (out / "candidates_registered.json").write_text(json.dumps(cand_out, indent=1, default=float), encoding="utf-8")
        log(f"  frozen candidates sha256 {sha[:16]}; registry {tid_t} + {[c['trial_id'] for c in cand_out]}")
        log(f"  done in {(time.time() - t0) / 3600:.2f} h -> {out}")
        log("  NEXT: build_research_set_v2.py --zone validation, build_paths.py --zone validation, validate_X.py "
            "(one look)")
        return 0
    finally:
        lf.close()


if __name__ == "__main__":
    raise SystemExit(main())
