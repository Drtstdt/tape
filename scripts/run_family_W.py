#!/usr/bin/env python3
"""D135/D136: family W -- copy the first buy of a wallet with a strong
point-in-time track record (tape/wallets.py), DISCOVERY zone.

Fixed BEFORE the first run (docs/DECISIONS.md D135, amended in D136 before any
W number was computed):
  ledger     scripts/build_wallet_ledger.py (wallet x token, first 24 h,
             leftover marked at the last price; usable from create + 24 h)
  snapshots  weekly, Monday 00:00 UTC; score = sum(pnl) / (n + 10), n >= 10
  classes    top1 (>= 99th pct of eligible), top5 (>= 95th), placebo (40-60th)
  trigger    first buy >= 0.1 SOL by a class wallet within 60 min of create,
             membership taken at the snapshot of that swap's week; one
             decision per token per class
  universe   discovery tokens, non-mayhem, standard curve; outcome window
             collected (CENS / NOENTRY dropped) -- as families A/C/E
  trade      anchored curve model (D126), our position on top of the observed
             path, entry 1 s after the leader's swap, point-in-time fee ratios
  exits      mirror = the leader's first sell that brings its sells since the
             trigger to >= 50% of its holdings at the trigger (+1 s), else
             time at 30 min; fixed = +60% / -30% / 30 min; graduation exits
  trials     W000..W007 = {top1, top5} x {mirror, fixed} x {0.5, 2 SOL}
  refutation placebo class with the same exit/size; curve-position-matched
             difference (entry-state deciles) reported; latency 3 s reported
  pass       pooled mean net day-block bootstrap 95% CI > 0 AND >= 60% of
             weeks (with trades) positive AND BH q < 0.10 (whole registry,
             one-sided bootstrap p) AND mean(W) - mean(placebo) CI > 0
Refuses to run twice.

    python scripts\\run_family_W.py --workers 4
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tape import pf_store as ps  # noqa: E402
from tape import trials as tr  # noqa: E402
from tape import wallets as wl  # noqa: E402
from tape import zones as zn  # noqa: E402
from tape.outcomes import TradeConfig, simulate_anchored  # noqa: E402
from tape.registry import Registry, spec_hash  # noqa: E402

TRIAL_CLASSES = ("top1", "top5")
EXITS = ("mirror", "fixed")
SIZES = (0.5, 2.0)
GRID = [(c, e, s) for c in TRIAL_CLASSES for e in EXITS for s in SIZES]
LATENCIES = (1, 3)                       # 1 s = the trial; 3 s = robustness, reported only
HORIZON_MS = 30 * 60_000
FETCH_AFTER_MS = wl.MAX_TRIGGER_MS + HORIZON_MS + 10 * 60_000
UNIVERSE = "discovery, non-mayhem, standard curve, outcome window collected"


def cfg_of(exit_rule: str, size: float, latency: int) -> TradeConfig:
    if exit_rule == "fixed":
        return TradeConfig(size_sol=size, take_profit=0.60, stop_loss=-0.30, horizon_ms=HORIZON_MS,
                           latency_s=latency)
    return TradeConfig(size_sol=size, take_profit=1e9, stop_loss=-1e9, horizon_ms=HORIZON_MS, latency_s=latency)


_W = {}


def _init(store, membership, snap_pos, collected, mem, tmp):
    _W.update(store=store, membership=membership, snap_pos=snap_pos, collected=set(collected))
    _W["con"] = ps.open_connection(memory_limit=mem, threads=1, temp_dir=tmp)


def token_rows(g, info, membership, snap_pos, collected):
    """All W rows for one token (pure apart from the imported model code)."""
    import build_research_set_v2 as b2
    create_ts = int(info["create_ts"])
    g, q_mkt, params, _ = b2.prepare_token(g)
    if params["fitted"] or not params["usable"]:
        return [], "nonstandard"
    ts = g["ts_ms"].to_numpy(dtype="int64")
    buy = g["side"].to_numpy() == "buy"
    qa = g["quote_amount"].to_numpy(dtype=float)
    ba = g["base_amount"].to_numpy(dtype=float)
    fee = g["fee_sol"].to_numpy(dtype=float)
    wal = g["wallet"].to_numpy(dtype=object)
    bt = info["bonding_ts"]
    bt = None if bt is None or bt != bt else int(bt)

    def pos_of(t):
        return snap_pos.get(int(wl.week_start(t)))

    rows = []
    for cls in (*TRIAL_CLASSES, "placebo"):
        trig = wl.find_trigger(ts, buy, qa, wal, create_ts, membership, cls, pos_of)
        if trig is None:
            continue
        fb, fs = wl.pit_fee_ratios(buy, qa, fee, trig)
        mir = wl.mirror_exit_index(buy, ba, wal, trig)
        for e in EXITS:
            for s in SIZES:
                for lat in LATENCIES:
                    o = simulate_anchored(ts, q_mkt, trig, params["k"], fb, fs, cfg_of(e, s, lat), collected, bt,
                                          exit_idx=(mir if e == "mirror" else None))
                    rows.append({"mint": info.name, "cls": cls, "exit": e, "size": s, "lat": lat,
                                 "status": o.status, "net": o.net_ret, "trig_ts": int(ts[trig]),
                                 "trig_after_create_s": (int(ts[trig]) - create_ts) / 1000.0,
                                 "leader": str(wal[trig]), "leader_sol": float(qa[trig]),
                                 "entry_q": o.entry_q, "hold_s": o.hold_s, "mirror_found": mir is not None,
                                 "fee_b": fb, "fee_s": fs})
    return rows, "ok"


def work_bucket(task):
    b, meta = task
    con = _W["con"]
    files = ps.bucket_files(_W["store"], b)
    flist = "[" + ", ".join(ps._q(f.as_posix()) for f in files) + "]"
    con.register("want_df", meta[["mint", "create_ts"]])
    df = con.execute(
        f"WITH j AS (SELECT s.* FROM read_parquet({flist}, union_by_name=true) s "
        f"JOIN want_df w ON s.mint = w.mint WHERE s.ts_ms <= w.create_ts + {FETCH_AFTER_MS}) "
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM j) WHERE rn = 1 "
        f"ORDER BY {ps._FETCH_ORDER}").df()
    con.unregister("want_df")
    m_index = meta.set_index("mint")
    out, cnt = [], {"ok": 0, "nonstandard": 0}
    if len(df):
        m_arr = df["mint"].to_numpy()
        cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
        for s_, e_ in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(df)]])):
            r, st = token_rows(df.iloc[s_:e_], m_index.loc[m_arr[s_]], _W["membership"], _W["snap_pos"],
                               _W["collected"])
            out.extend(r)
            cnt[st] += 1
    return b, out, cnt


def evaluate(res, log, n_boot=2000):
    """Per-trial statistics (no registry)."""
    import pandas as pd
    base = res[(res["lat"] == 1) & ~res["status"].isin(["CENS", "NOENTRY"])].copy()
    base["day"] = base["trig_ts"] // tr.DAY_MS
    base["week"] = wl.week_start(base["trig_ts"].to_numpy())
    rob = res[(res["lat"] == 3) & ~res["status"].isin(["CENS", "NOENTRY"])]
    out = {}
    rng = np.random.default_rng(31)
    for i, (c, e, s) in enumerate(GRID):
        a = base[(base["cls"] == c) & (base["exit"] == e) & (base["size"] == s)]
        p = base[(base["cls"] == "placebo") & (base["exit"] == e) & (base["size"] == s)]
        if len(a) == 0:
            out[i] = {"cls": c, "exit": e, "size": s, "n": 0}
            continue
        y, d = a["net"].to_numpy(float), a["day"].to_numpy()
        boot = tr.day_block_bootstrap(y, np.ones(len(y), bool), d, n_boot, seed=41)
        days_u = np.unique(d)
        idx = [np.flatnonzero(d == x) for x in days_u]
        means = np.array([np.nanmean(y[np.concatenate([idx[j] for j in rng.integers(0, len(days_u), len(days_u))])])
                          for _ in range(n_boot)])
        p_one = float((1 + np.sum(means <= 0)) / (1 + n_boot))
        wk = a.groupby("week")["net"].mean()
        two = (tr.two_sample_day_bootstrap(y, d, p["net"].to_numpy(float), p["day"].to_numpy(), n_boot, seed=43)
               if len(p) else {"diff": float("nan"), "ci": [float("nan")] * 2, "p_le0": float("nan")})
        pooled = pd.concat([a.assign(_w=True), p.assign(_w=False)], ignore_index=True)
        strata = pd.qcut(pooled["entry_q"].rank(method="first"), 10, labels=False).to_numpy() if len(pooled) >= 20 \
            else np.zeros(len(pooled), dtype=int)
        md = tr.matched_diff(pooled["net"].to_numpy(float), pooled["_w"].to_numpy(bool), strata)
        r3 = rob[(rob["cls"] == c) & (rob["exit"] == e) & (rob["size"] == s)]
        lead = a["leader"].value_counts()
        out[i] = {"cls": c, "exit": e, "size": s, "n": int(len(a)), "mean": float(np.nanmean(y)),
                  "median": float(np.nanmedian(y)), "p_win": float(np.mean(y > 0)), "ci": boot["sel_mean_ci"],
                  "p_one_sided": p_one, "weeks_positive": int((wk > 0).sum()), "weeks": int(len(wk)),
                  "status": a["status"].value_counts(normalize=True).round(4).to_dict(),
                  "placebo_n": int(len(p)), "placebo_mean": float(p["net"].mean()) if len(p) else float("nan"),
                  "diff_vs_placebo": two["diff"], "diff_ci": two["ci"], "matched_diff_vs_placebo": md,
                  "lat3_mean": float(r3["net"].mean()) if len(r3) else float("nan"),
                  "leaders": int(len(lead)), "top_leader_share": float(lead.iloc[0] / len(a)),
                  "trig_after_create_median_s": float(a["trig_after_create_s"].median()),
                  "mirror_found_share": float(a["mirror_found"].mean())}
        r = out[i]
        log(f"  [{c:4s} {e:6s} {s:3.1f} SOL] n={r['n']:,} mean {r['mean']:+.4f} CI{[round(v, 4) for v in r['ci']]} "
            f"median {r['median']:+.4f} win {r['p_win']:.3f} weeks>0 {r['weeks_positive']}/{r['weeks']} | "
            f"placebo n={r['placebo_n']:,} {r['placebo_mean']:+.4f} diff {r['diff_vs_placebo']:+.4f} "
            f"CI{[round(v, 4) for v in r['diff_ci']]} matched {md:+.4f} | lat3 {r['lat3_mean']:+.4f} | "
            f"leaders {r['leaders']:,} top share {r['top_leader_share']:.3f}")
    return out


def main() -> int:
    holder = []
    try:
        return _main(holder)
    finally:
        for f in holder:
            f.close()


def _main(holder) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--zones", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--ledger", default=r"E:\tape_research\wallets_v1\ledger")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--out", default=r"E:\tape_research\trials\family_W")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--worker-memory", default="2GB")
    ap.add_argument("--temp-dir", default=r"E:\tape_store_v2\_duckdb_tmp")
    a = ap.parse_args()
    import pandas as pd
    from explore_facts_v2 import collected_hours

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lf = open(out / "log.txt", "a", encoding="utf-8")
    holder.append(lf)

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    reg = Registry(a.registry)
    specs = [{"family": "W", "leader_class": c, "exit": e, "size_sol": s, "latency_s": 1,
              "trigger": f"first buy >= {wl.MIN_TRIGGER_SOL} SOL within {wl.MAX_TRIGGER_MS // 60_000} min of create",
              "score": f"sum(pnl)/(n+{wl.PRIOR_N}), n>={wl.MIN_N}, weekly PIT snapshots, 24 h settle",
              "mirror": f">= {wl.MIRROR_FRACTION} of holdings sold" if e == "mirror" else None,
              "universe": UNIVERSE, "ledger": "wallets_v1", "decision": "D135/D136"} for c, e, s in GRID]
    if any(spec_hash(s) in {t.get("spec_hash") for t in reg.trials().values()} for s in specs):
        log("REFUSED: family W trials with these specs are already registered (no re-rolls).")
        return 2
    parts = sorted(Path(a.ledger).glob("part-b*.parquet"))
    if len(parts) < ps.N_BUCKETS:
        log(f"STOP: ledger incomplete ({len(parts)} parts in {a.ledger}) -- run build_wallet_ledger.py first")
        return 4

    t0 = time.time()
    log(f"\n=== run_family_W {datetime.now(timezone.utc).isoformat()} ===")
    disc = zn.load_zone(a.zones, "discovery")
    log(f"  discovery zone: {len(disc):,} tokens (sha256 verified)")
    ev = ps.load_events(a.store)
    cr = (ev[(ev["event_type"] == "create") & ev["ts_ms"].notna()].sort_values("ts_ms")
          .drop_duplicates("mint")[["mint", "ts_ms", "is_mayhem_mode"]].rename(columns={"ts_ms": "create_ts"}))
    bc = ev[(ev["event_type"] == "bonding_complete") & ev["ts_ms"].notna()].groupby("mint")["ts_ms"].min()
    cr["bonding_ts"] = cr["mint"].map(bc)
    cr = cr[cr["mint"].isin(set(disc))].copy()
    cr["create_ts"] = cr["create_ts"].astype("int64")
    n_all = len(cr)
    cr = cr[cr["is_mayhem_mode"].fillna(True).astype(bool) == False].copy()  # noqa: E712 (unknown flag = excluded)
    log(f"  tokens with create: {n_all:,}; non-mayhem: {len(cr):,}")
    del ev

    led = pd.concat([pd.read_parquet(p, columns=["wallet", "settle_ts", "pnl"]) for p in parts], ignore_index=True)
    snaps = list(range(int(wl.week_start(cr["create_ts"].min())),
                       int(wl.week_start(cr["create_ts"].max() + wl.MAX_TRIGGER_MS)) + wl.WEEK_MS, wl.WEEK_MS))
    log(f"  ledger rows {len(led):,}; weekly snapshots {len(snaps)} "
        f"({datetime.fromtimestamp(snaps[0] / 1000, timezone.utc):%Y-%m-%d} .. "
        f"{datetime.fromtimestamp(snaps[-1] / 1000, timezone.utc):%Y-%m-%d})")
    t1 = time.time()
    membership, stats = wl.weekly_classes(led["wallet"].to_numpy(dtype=object), led["settle_ts"].to_numpy("int64"),
                                          led["pnl"].to_numpy(float), snaps)
    del led
    for s in stats:
        log(f"    snapshot {datetime.fromtimestamp(s['snapshot'] / 1000, timezone.utc):%Y-%m-%d}: eligible "
            f"{s['n_eligible']:,}  top1 {s.get('n_top1', 0):,} (score >= {s.get('min_score_top1', float('nan')):+.3f})  "
            f"top5 {s.get('n_top5', 0):,}  placebo {s.get('n_placebo', 0):,}")
    log(f"  classes built in {time.time() - t1:.0f}s; wallets in any class: {len(membership):,}")
    snap_pos = {s: k for k, s in enumerate(snaps)}

    cov = ps.load_coverage(a.store)
    collected, _ = collected_hours(cov.to_dict("records"))
    cr["bucket"] = [ps.bucket_of(m) for m in cr["mint"]]
    tasks = [(int(b), g.drop(columns=["bucket"]).reset_index(drop=True)) for b, g in cr.groupby("bucket")]
    rows, cnt = [], {"ok": 0, "nonstandard": 0}
    t2 = time.time()
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                             initargs=(a.store, membership, snap_pos, sorted(collected), a.worker_memory,
                                       a.temp_dir)) as ex:
        for i, (b, r_, c_) in enumerate(ex.map(work_bucket, tasks), start=1):
            rows.extend(r_)
            for k in cnt:
                cnt[k] += c_[k]
            el = time.time() - t2
            log(f"  [bucket {b:02d}] {i}/{len(tasks)}  rows={len(rows):,}  elapsed={el:.0f}s "
                f"ETA~{el / i * (len(tasks) - i):.0f}s")
    res = pd.DataFrame(rows)
    res.to_parquet(out / "replay.parquet", index=False)
    log(f"  tokens replayed: {cnt['ok']:,} standard curve, {cnt['nonstandard']:,} non-standard skipped; rows {len(res):,}")
    if len(res) == 0:
        log("STOP: no triggers at all -- nothing to register.")
        return 5
    tok = res[res["lat"] == 1].drop_duplicates(["mint", "cls"])
    log(f"  tokens with a trigger per class: {tok['cls'].value_counts().to_dict()}; "
        f"status (all classes, lat 1): {res[res['lat'] == 1]['status'].value_counts().to_dict()}")

    log("\n  per trial (latency 1 s; CENS/NOENTRY dropped):")
    results = evaluate(res, log)
    ids = {}
    for i, (c, e, s) in enumerate(GRID):
        r = results[i]
        ids[i] = reg.register("W", f"W {c} {e} {s} SOL", "copying the first buy of a wallet with a strong "
                              "point-in-time track record is profitable after costs", specs[i], zone="discovery",
                              metrics=r, p_value=r.get("p_one_sided"))
    q = reg.bh_qvalues()
    log("\n  verdicts (CI>0 AND >=60% weeks positive AND q<0.10 AND diff vs placebo CI>0):")
    for i, r in results.items():
        ok = (r.get("n", 0) > 0 and r["ci"][0] > 0 and r["weeks_positive"] >= 0.6 * r["weeks"]
              and q.get(ids[i], 1.0) < 0.10 and r["diff_ci"][0] > 0)
        reg.update(ids[i], status="passed_discovery" if ok else "screened_out", q_bh=q.get(ids[i]))
        log(f"    {ids[i]} {r['cls']} {r['exit']} {r['size']} SOL: q={q.get(ids[i], float('nan')):.4f} -> "
            f"{'PASSED discovery' if ok else 'screened out'}")
    (out / "results.json").write_text(json.dumps({"results": results, "ids": ids, "snapshots": stats},
                                                 indent=1, default=float), encoding="utf-8")
    log(f"  done in {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
