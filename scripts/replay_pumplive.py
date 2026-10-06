#!/usr/bin/env python3
"""D145: replay the live paper bot (tape/pumplive) on the DISCOVERY zone -- family R, stage 1
(on-chain rails only; metadata/Twitter rails are stage 2).

Phase 1 (workers, one bucket each, resumable): for every decision of the frozen
family-X universe (paths_v1: K 5/10/20 within 60 min, non-mayhem, standard
curve, outcome window collected) feed the token's stored swaps to the LIVE
TokenState, record every rail separately, and the net of all 36 live exits
(TP x SL x horizon, 2 slots, 0.5 SOL, 0.001 SOL tip).
Phase 2 (analysis): pre-registered trials R000-R002 (registered before any
outcome statistic is computed):
  R000  the live learner, walked forward week by week: champion from trades
        closed before the week (30 d window, n >= 200, >= 7 days, Bonferroni-144
        LCB > 0, else ABSTAIN), traded in that week only. Gate: pooled net of
        the champion's trades, day-block 95% CI > 0 and one-sided p < 0.05/3.
  R001  rails effect: mean over the 36 exits per decision, kept-by-all-rails
        minus all decisions (day-block CI of the difference).
  R002  best fixed config with rails on over the whole zone (in-sample, the
        Bonferroni-144 p of its LCB) -- an upper bound, not a strategy.

    python scripts\\replay_pumplive.py --workers 4
    python scripts\\replay_pumplive.py --analyze-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tape import pf_store as ps  # noqa: E402
from tape.fastexit import exit_grid  # noqa: E402
from tape.pumplive import replay as rp  # noqa: E402
from tape.pumplive.engine import ENTRY_RULES, Settings  # noqa: E402

N_TRIALS = 3
FETCH_COLS = "mint, ts_ms, slot, sig, side, base_amount, quote_amount, wallet, real_quote_reserve_after, fee_sol"

_W = {}


def _init(store, paths_dir, out_dir, settings, mem, tmp):
    _W.update(store=store, paths_dir=paths_dir, out_dir=out_dir, s=Settings(**settings))
    _W["con"] = ps.open_connection(memory_limit=mem, threads=1, temp_dir=tmp)


def process_bucket_frames(s: Settings, meta, info, df, rel, slo, q_path):
    """Pure core of one bucket (tested): meta = paths meta rows (mint, K, decision_ts,
    k, fee_b, fee_s, grad, offset, length, create_ts, day, week); info = per-mint
    creator, create_slot, creator history; df = deduped swaps sorted by fetch order.
    Returns a list of row dicts."""
    import build_research_set_v2 as b2
    tps, sls = np.array(s.tps, dtype=float), np.array(s.sls, dtype=float)    # exit_grid order == exit_combos
    hs = np.array(s.horizons_min, dtype=np.int64) * 60_000
    by_mint = {m: g for m, g in meta.groupby("mint")}
    rows, mism = [], {"decision": 0, "bars": 0, "no_swaps": 0, "no_info": 0}
    seen = set()
    if len(df):
        m_arr = df["mint"].to_numpy()
        cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
        for s_, e_ in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(df)]])):
            mint = m_arr[s_]
            if mint not in by_mint:
                continue
            if mint not in info:
                mism["no_info"] += 1
                continue
            seen.add(mint)
            g, q_mkt, params, _ = b2.prepare_token(df.iloc[s_:e_])
            ts = g["ts_ms"].to_numpy(dtype="int64")
            closes = b2.bar_close_indices(g["quote_amount"].to_numpy(dtype=float),
                                          float(g["quote_reserve_after"].to_numpy()[0]))
            mrows = by_mint[mint]
            dec = {}
            for r in mrows.itertuples(index=False):
                K = int(r.K)
                if len(closes) >= K and int(ts[closes[K - 1]]) == int(r.decision_ts):
                    dec[K] = closes[K - 1]
                else:
                    mism["decision"] += 1
            inf = info[mint]
            snaps, dev_init = rp.replay_token(
                s, mint, inf["creator"], int(inf["create_ts"]), int(inf["create_slot"]),
                g["side"].to_numpy() == "buy", g["quote_amount"].to_numpy(dtype=float),
                g["base_amount"].to_numpy(dtype=float), g["wallet"].to_numpy(dtype=object), ts,
                g["slot"].to_numpy(dtype="int64"), q_mkt, float(params["k"]), dec)
            c_rails = rp.onchain_create_rails(s, dev_init, inf["prior_launches"], inf["prior_grads"],
                                              inf["launches_24h"])
            for r in mrows.itertuples(index=False):
                K = int(r.K)
                if K not in snaps:
                    continue
                sn = snaps[K]
                if sn["bars"] != K:
                    mism["bars"] += 1
                o, ln = int(r.offset), int(r.length)
                grid = exit_grid(rel[o:o + ln].astype(np.int64), slo[o:o + ln].astype(np.int64),
                                 q_path[o:o + ln].astype(float), int(r.grad), float(r.k), float(r.fee_b),
                                 float(r.fee_s), s.size_sol, s.latency_slots, s.tip_sol, tps, sls, hs)
                why = set(c_rails) | set(sn["rails"])
                row = {"mint": mint, "K": K, "create_ts": int(r.create_ts), "decision_ts": int(r.decision_ts),
                       "day": int(r.day), "week": int(r.week), "q_gain": float(sn["q_gain"]),
                       "n_trades": int(sn["n_trades"]), "dev_initial_share": dev_init / 1e9,
                       "dev_sold_tokens": float(sn["dev_sold_tokens"]), "dev_hold_share": float(sn["dev_hold_share"]),
                       "top10_share": float(sn["top10_share"]), "first_slot_share": float(sn["first_slot_share"]),
                       "creator_prior_launches": float(inf["prior_launches"]),
                       "creator_prior_grads": float(inf["prior_grads"]),
                       "creator_launches_24h": float(inf["launches_24h"])}
                for rail in rp.ALL_RAILS:
                    row[f"r_{rail}"] = rail in why
                for j, v in enumerate(grid.reshape(-1)):
                    row[f"n{j:02d}"] = float(v)
                rows.append(row)
    mism["no_swaps"] = len(set(by_mint) - seen)
    return rows, mism


def work_bucket(b):
    import pandas as pd
    out_path = Path(_W["out_dir"]) / f"part-b{b:02d}.parquet"
    if out_path.exists():
        return b, "skipped", {}
    t0 = time.time()
    meta = pd.read_parquet(Path(_W["paths_dir"]) / f"meta-b{b:02d}.parquet")
    info = pd.read_parquet(Path(_W["out_dir"]) / "_info" / f"info-b{b:02d}.parquet").set_index("mint")
    info = info.to_dict("index")
    with np.load(Path(_W["paths_dir"]) / f"path-b{b:02d}.npz") as arr:
        rel, slo, q_path = arr["rel_ts"], arr["slot_rel"], arr["q"]
    con = _W["con"]
    files = ps.bucket_files(_W["store"], b)
    flist = "[" + ", ".join(ps._q(f.as_posix()) for f in files) + "]"
    want = meta.groupby("mint", as_index=False)["decision_ts"].max()
    con.register("want_df", want)
    df = con.execute(
        f"WITH j AS (SELECT s.* FROM read_parquet({flist}, union_by_name=true) s "
        f"JOIN want_df w ON s.mint = w.mint WHERE s.ts_ms <= w.decision_ts) "
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM j) WHERE rn = 1 "
        f"ORDER BY {ps._FETCH_ORDER}").df()
    con.unregister("want_df")
    t_q = time.time() - t0
    rows, mism = process_bucket_frames(_W["s"], meta, info, df, rel, slo, q_path)
    out = pd.DataFrame(rows)
    tmp = out_path.with_suffix(".tmp")
    out.to_parquet(tmp, index=False)
    os.replace(tmp, out_path)
    return b, "done", {"rows": len(rows), "decisions": len(meta), **{f"mism_{k}": v for k, v in mism.items()},
                       "query_s": t_q, "total_s": time.time() - t0}


# ---------------------------------------------------------------------------
# phase 2
# ---------------------------------------------------------------------------

def build_matrix(df, s: Settings):
    """nets (n, 144) and eligibility (n, 144) in config_ids order; close_ms (n, 144)."""
    E = len(rp.exit_combos(s))
    nets36 = df[[f"n{j:02d}" for j in range(E)]].to_numpy(dtype=np.float64)
    rails_ok = ~df[[f"r_{r}" for r in rp.ALL_RAILS]].to_numpy().any(axis=1)
    K = df["K"].to_numpy()
    rule_mask = {"K5_all": K == 5, "K10_all": K == 10,
                 "K10_floor": (K == 10) & (df["q_gain"].to_numpy() <= s.floor_q_gain), "K20_all": K == 20}
    assert list(rule_mask) == list(ENTRY_RULES)
    hz = np.array([h for _, _, h in rp.exit_combos(s)], dtype=np.int64) * 60_000
    dts = df["decision_ts"].to_numpy(dtype=np.int64)
    nets = np.concatenate([nets36] * len(rule_mask), axis=1)
    elig_all = np.concatenate([np.repeat(m[:, None], E, axis=1) for m in rule_mask.values()], axis=1)
    close = np.concatenate([dts[:, None] + hz[None, :]] * len(rule_mask), axis=1)
    return nets, elig_all, rails_ok, close


def day_boot(y, day, n_boot=2000, seed=0):
    """(mean, 95% CI, one-sided p of mean <= 0), resampling days."""
    rng = np.random.default_rng(seed)
    du = np.unique(day)
    idx = [np.flatnonzero(day == d) for d in du]
    ms = np.array([y[np.concatenate([idx[j] for j in rng.integers(0, len(du), len(du))])].mean()
                   for _ in range(n_boot)])
    return float(y.mean()), [float(np.quantile(ms, .025)), float(np.quantile(ms, .975))], \
        float((1 + np.sum(ms <= 0)) / (n_boot + 1))


def analyze(a, s: Settings, log):
    import pandas as pd
    from statistics import NormalDist
    from tape.registry import Registry
    from tape import trials as tr

    out_dir = Path(a.out) / "discovery"
    parts = sorted(out_dir.glob("part-b*.parquet"))
    if len(parts) < ps.N_BUCKETS:
        log(f"STOP: only {len(parts)} of {ps.N_BUCKETS} parts -- run phase 1 first")
        return 4
    t0 = time.time()
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df = df.sort_values(["decision_ts", "mint", "K"]).reset_index(drop=True)
    log(f"\n  phase 2: {len(df):,} decisions, {df['mint'].nunique():,} tokens, "
        f"K {df['K'].value_counts().sort_index().to_dict()} ({time.time() - t0:.0f}s)")
    cids = rp.config_ids(s)
    spec_common = {"settings": asdict(s), "universe": "paths_v1/discovery (family-X universe)",
                   "stage": 1, "rails": list(rp.ALL_RAILS)}
    reg = Registry(a.registry)
    have = {t["name"]: tid for tid, t in reg.trials().items() if t["family"] == "R"}
    names = {"R000": ("R learner walk-forward (on-chain rails)",
                      "the live learner's weekly champion, chosen only from earlier trades, nets > 0 the next week"),
             "R001": ("R rails effect (on-chain rails)",
                      "decisions kept by all on-chain rails have a higher mean net (avg over the 36 exits) than all"),
             "R002": ("R best fixed config rails on (in-sample)",
                      "the best of the 144 live configs with rails on has LCB > 0 over the whole zone (Bonferroni-144)")}
    ids = {}
    for key, (nm, hyp) in names.items():
        if nm in have:
            ids[key] = have[nm]
            log(f"  {key}: already registered as {have[nm]} (re-analysis of the same frozen spec)")
        else:
            ids[key] = reg.register("R", nm, hyp, dict(spec_common, trial=key), "discovery")
            log(f"  {key}: registered as {ids[key]} BEFORE any outcome statistic")

    nets, elig, rails_ok, close = build_matrix(df, s)
    day = df["day"].to_numpy()
    E = len(rp.exit_combos(s))
    # ---- universe & rails -------------------------------------------------
    log("\n  RAILS (share of decisions vetoed; y = mean net over the 36 exits of a decision):")
    y = np.nanmean(nets[:, :E], axis=1)
    okf = np.isfinite(y)
    log(f"    all decisions: n={okf.sum():,} mean y={np.nanmean(y):+.4f}")
    for r in rp.ALL_RAILS:
        v = df[f"r_{r}"].to_numpy()
        log(f"    {r:22s} veto {v.mean():6.2%}  y vetoed {np.nanmean(y[v]) if v.any() else float('nan'):+.4f}  "
            f"kept {np.nanmean(y[~v]):+.4f}")
    log(f"    ALL RAILS              keep {rails_ok.mean():6.2%}  y kept {np.nanmean(y[rails_ok]):+.4f}")
    bb = tr.day_block_bootstrap(y[okf], rails_ok[okf], day[okf], 2000, seed=71)
    m_k = float(np.nanmean(y[rails_ok]))
    diff = m_k - float(np.nanmean(y))
    rng = np.random.default_rng(72)
    du = np.unique(day[okf])
    idx = [np.flatnonzero(day[okf] == d) for d in du]
    yy, kk = y[okf], rails_ok[okf]
    dd = []
    for _ in range(2000):
        pick = np.concatenate([idx[j] for j in rng.integers(0, len(du), len(du))])
        dd.append(yy[pick][kk[pick]].mean() - yy[pick].mean())
    p1 = float((1 + np.sum(np.array(dd) <= 0)) / 2001)
    log(f"  R001 rails effect: kept {m_k:+.4f} vs all {np.nanmean(y):+.4f}, diff {diff:+.4f} "
        f"CI{[round(v, 4) for v in bb['diff_ci']]} p={p1:.4f}")
    reg.update(ids["R001"], status="passed_discovery" if bb["diff_ci"][0] > 0 and p1 < 0.05 / N_TRIALS
               else "screened_out", metrics={"kept_mean": m_k, "all_mean": float(np.nanmean(y)), "diff": diff,
                                             "diff_ci": bb["diff_ci"], "keep_share": float(rails_ok.mean())},
               p_value=p1)

    # ---- fixed configs, whole zone (in-sample) ---------------------------
    z = rp.learner_z(len(cids))
    take = elig & rails_ok[:, None]
    stats = []
    for c in range(len(cids)):
        m = take[:, c] & np.isfinite(nets[:, c])
        n, D, mean, se, lcb = rp.lcb_stats(nets[m, c], close[m, c] // rp.DAY_MS, z)
        stats.append((c, n, D, mean, se, lcb))
    stats.sort(key=lambda r: -r[5])
    log(f"\n  FIXED CONFIGS, rails on, whole discovery (in-sample; Bonferroni z={z:.2f}) -- top 10 by LCB:")
    for c, n, D, mean, se, lcb in stats[:10]:
        log(f"    {cids[c]:32s} n={n:6,d} days={D:3d} mean={mean:+.4f} se={se:.4f} LCB={lcb:+.4f}")
    pos = sum(1 for r in stats if r[3] > 0)
    log(f"    configs with mean > 0: {pos}/{len(stats)}; with LCB > 0: {sum(1 for r in stats if r[5] > 0)}")
    c, n, D, mean, se, lcb = stats[0]
    p2 = float(min(1.0, (1 - NormalDist().cdf(mean / se)) * len(cids))) if se > 0 and np.isfinite(se) else 1.0
    log(f"  R002 best fixed config {cids[c]}: mean {mean:+.4f} LCB {lcb:+.4f} p(Bonf-144)={p2:.4f}")
    reg.update(ids["R002"], status="passed_discovery" if lcb > 0 and p2 < 0.05 / N_TRIALS else "screened_out",
               metrics={"config": cids[c], "n": n, "days": D, "mean": mean, "se": se, "lcb": lcb}, p_value=p2)

    # ---- learner walk-forward ----------------------------------------------
    win = int(s.window_days * rp.DAY_MS)
    dts = df["decision_ts"].to_numpy(dtype=np.int64)
    weeks = sorted(set(((dts - rp.MONDAY0) // rp.WEEK_MS).tolist()))
    log(f"\n  LEARNER WALK-FORWARD ({len(weeks)} weeks; window {s.window_days:.0f} d, n >= {s.min_trades}, "
        f">= 7 days, LCB > 0 else ABSTAIN):")
    picked_net, picked_day, champs, nobrake_net, nobrake_day = [], [], [], [], []
    t1 = time.time()
    for i, w in enumerate(weeks, start=1):
        ws = w * rp.WEEK_MS + rp.MONDAY0
        cidx, rows = rp.champion_at(ws, close, nets, take, z, win, s.min_trades)
        in_w = (dts >= ws) & (dts < ws + rp.WEEK_MS)
        msg = f"    week {i}/{len(weeks)} {datetime.fromtimestamp(ws / 1000, tz=timezone.utc).date()}: "
        if cidx is None:
            msg += "ABSTAIN"
            best = rows[0] if rows else None
            if best:
                msg += f" (best LCB {best[5]:+.4f} {cids[best[0]]} n={best[1]})"
        else:
            m = in_w & take[:, cidx] & np.isfinite(nets[:, cidx])
            picked_net.extend(nets[m, cidx].tolist())
            picked_day.extend(day[m].tolist())
            champs.append(cids[cidx])
            lcb_c = next(r[5] for r in rows if r[0] == cidx)
            msg += (f"champion {cids[cidx]} (LCB {lcb_c:+.4f}) -> next week n={m.sum()} "
                    f"mean {np.nanmean(nets[m, cidx]) if m.any() else float('nan'):+.4f}")
        # diagnostic only: the same learner WITHOUT the brake (best mean, n >= min_trades)
        nb = [r for r in rows if r[1] >= s.min_trades]
        if nb:
            cb = max(nb, key=lambda r: r[3])[0]
            m = in_w & take[:, cb] & np.isfinite(nets[:, cb])
            nobrake_net.extend(nets[m, cb].tolist())
            nobrake_day.extend(day[m].tolist())
        el = time.time() - t1
        log(msg + f"  [elapsed {el:.0f}s ETA~{el / i * (len(weeks) - i):.0f}s]")
    if len(picked_net) >= 30:
        mean0, ci0, p0 = day_boot(np.array(picked_net), np.array(picked_day), seed=73)
        passed = ci0[0] > 0 and p0 < 0.05 / N_TRIALS
        log(f"  R000 learner: {len(champs)} weeks with a champion, n={len(picked_net)} mean {mean0:+.4f} "
            f"CI{[round(v, 4) for v in ci0]} p={p0:.4f} -> {'PASS' if passed else 'fail'}")
        reg.update(ids["R000"], status="passed_discovery" if passed else "screened_out",
                   metrics={"weeks_with_champion": len(champs), "champions": champs, "n": len(picked_net),
                            "mean": mean0, "ci": ci0}, p_value=p0)
    else:
        log(f"  R000 learner: ABSTAIN in {len(weeks) - len(champs)} of {len(weeks)} weeks; "
            f"{len(picked_net)} trades -> no strategy (the brake found no config worth trading)")
        reg.update(ids["R000"], status="screened_out", metrics={"weeks_with_champion": len(champs),
                                                                "n": len(picked_net)}, p_value=1.0)
    if len(nobrake_net) >= 30:
        mb, cib, pb = day_boot(np.array(nobrake_net), np.array(nobrake_day), seed=74)
        log(f"  diagnostic (NOT a trial): learner WITHOUT the brake (best mean, n >= {s.min_trades}): "
            f"n={len(nobrake_net)} mean {mb:+.4f} CI{[round(v, 4) for v in cib]}")
    summary = {"utc": datetime.now(timezone.utc).isoformat(), "trials": ids,
               "registry": {k: reg.trials()[v] for k, v in ids.items()}}
    (out_dir / "replay_summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    log(f"  summary -> {out_dir / 'replay_summary.json'}; phase 2 done in {time.time() - t0:.0f}s")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--paths", default=r"E:\tape_research\paths_v1")
    ap.add_argument("--out", default=r"E:\tape_research\replay_R")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--temp-dir", default=r"E:\tape_store_v2\_duckdb_tmp")
    ap.add_argument("--analyze-only", action="store_true")
    a = ap.parse_args(argv)
    import pandas as pd
    from tape import pit_features as pf

    s = Settings()                       # FROZEN: the live defaults (D144), never the json override
    out_dir = Path(a.out) / "discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    lf = open(out_dir / "replay_log.txt", "a", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    try:
        t0 = time.time()
        log(f"\n=== replay_pumplive {datetime.now(timezone.utc).isoformat()} zone=discovery stage 1 ===")
        if not a.analyze_only:
            paths_dir = Path(a.paths) / "discovery"
            if len(list(paths_dir.glob("meta-b*.parquet"))) < ps.N_BUCKETS:
                log(f"STOP: {paths_dir} incomplete -- run build_paths.py --zone discovery first")
                return 4
            info_dir = out_dir / "_info"
            if len(list(info_dir.glob("info-b*.parquet"))) < ps.N_BUCKETS:
                log("  creator history (point-in-time) and create slots from the event table ...")
                ev = ps.load_events(a.store)
                cr = (ev[(ev["event_type"] == "create") & ev["ts_ms"].notna()].sort_values("ts_ms")
                      .drop_duplicates("mint")[["mint", "ts_ms", "slot", "creator"]]
                      .rename(columns={"ts_ms": "create_ts", "slot": "create_slot"}))
                bc = ev[(ev["event_type"] == "bonding_complete") & ev["ts_ms"].notna()].groupby("mint")["ts_ms"].min()
                del ev
                cr["create_ts"] = cr["create_ts"].astype("int64")
                ch = pf.creator_history(cr["create_ts"].to_numpy(), cr["creator"].to_numpy(dtype=object),
                                        cr["mint"].map(bc).to_numpy(dtype=float))
                cr["prior_launches"] = ch["creator_prior_launches"]
                cr["prior_grads"] = ch["creator_prior_grads"]
                cr["launches_24h"] = ch["creator_launches_24h"]
                cr["create_slot"] = cr["create_slot"].fillna(-1).astype("int64")
                cr["creator"] = cr["creator"].where(cr["creator"].notna(), "").astype(str)
                info_dir.mkdir(exist_ok=True)
                for b in range(ps.N_BUCKETS):
                    mints = set(pd.read_parquet(paths_dir / f"meta-b{b:02d}.parquet", columns=["mint"])["mint"])
                    cr[cr["mint"].isin(mints)].to_parquet(info_dir / f"info-b{b:02d}.parquet", index=False)
                log(f"  creates {len(cr):,}; per-bucket info written ({time.time() - t0:.0f}s)")
                del cr
            done = sum(1 for b in range(ps.N_BUCKETS) if (out_dir / f"part-b{b:02d}.parquet").exists())
            log(f"  phase 1: {ps.N_BUCKETS} buckets, {done} already done; workers={a.workers}")
            t1 = time.time()
            tot = {}
            todo = [b for b in range(ps.N_BUCKETS)]
            with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                                     initargs=(a.store, str(paths_dir), str(out_dir), asdict(s), "2GB",
                                               a.temp_dir)) as ex:
                for i, (b, st, info) in enumerate(ex.map(work_bucket, todo), start=1):
                    for k, v in info.items():
                        if k.startswith(("rows", "decisions", "mism")):
                            tot[k] = tot.get(k, 0) + v
                    el = time.time() - t1
                    log(f"  [bucket {b:02d} {st}] {i}/{len(todo)} rows={tot.get('rows', 0):,} "
                        f"mismatch dec/bars/noswaps/noinfo={tot.get('mism_decision', 0)}/{tot.get('mism_bars', 0)}/"
                        f"{tot.get('mism_no_swaps', 0)}/{tot.get('mism_no_info', 0)} ({info.get('total_s', 0):.0f}s) "
                        f"elapsed={el:.0f}s ETA~{el / i * (len(todo) - i):.0f}s")
            log(f"  phase 1 done: {tot} in {time.time() - t1:.0f}s")
        return analyze(a, s, log)
    finally:
        lf.close()


if __name__ == "__main__":
    raise SystemExit(main())
