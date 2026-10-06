#!/usr/bin/env python3
"""D131: family F -- exit variants on the family-E entries.

Fixed BEFORE the first run (docs/DECISIONS.md D131):
  entries   the out-of-fold selection of trial E000 (LightGBM prediction > 0),
            REPRODUCED deterministically with run_family_E.walk_forward; the run
            stops unless the reproduction matches the registered E000 numbers
            (n = 16,392, mean net -0.0104 +- 0.0005) -- the entry set is NOT
            re-chosen here
  trade     2 SOL, latency 1 s, anchored curve model, token fee ratios,
            graduation exits as before (tape/outcomes.simulate_anchored)
  grid      TP {+30, +60, +100, +200%} x SL {-15, -30, -50%} x horizon
            {10, 30 min} = 24 trials F000..F023 (budget 24, nothing else)
  sample    identical for every config: the E000 rows, whose 30-min hold
            windows are already known to lie in collected hours
  pass      pooled mean net day-block bootstrap 95% CI > 0 AND positive in
            >= 60% of test weeks AND BH q < 0.10 (one-sided bootstrap p)
Refuses to run twice.

D133 (before any F trial was registered -- the first F run stopped at the
consistency check): runs on research set v3; the entry set must reproduce the
E000 set of the v3 REPLICATION exactly (trials/family_E_v3/results.json:
same n, same sha256 of the mint list, mean within 5e-4) -- no hard-coded
numbers any more.

    python scripts\\run_family_F.py --workers 4
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
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
from tape import zones as zn  # noqa: E402
from tape.outcomes import TradeConfig, simulate_anchored  # noqa: E402
from tape.registry import Registry, spec_hash  # noqa: E402

TPS = (0.3, 0.6, 1.0, 2.0)
SLS = (-0.15, -0.30, -0.50)
HORIZONS_MIN = (10, 30)
GRID = [(tp, sl, h) for h, tp, sl in itertools.product(HORIZONS_MIN, TPS, SLS)]
MEAN_TOL = 0.0005


def expected_e000(path):
    """The E000 entry set recorded by `run_family_E.py --replicate` (D133)."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if not d.get("replication") or d.get("set") != "sets_v3" or "e000_entry" not in d:
        raise SystemExit(f"STOP: {path} is not the sets_v3 replication of family E -- run "
                         f"`python scripts\\run_family_E.py --replicate` first")
    return d["e000_entry"]


def entry_hash(mints) -> str:
    return hashlib.sha256("\n".join(sorted(str(m) for m in mints)).encode()).hexdigest()
MAX_DEC_MS = 60 * 60_000
PAD_MS = 30 * 60_000 + 10 * 60_000      # identical fetch window to build_research_set_v2


def cfg_of(tp, sl, h):
    return TradeConfig(size_sol=2.0, take_profit=tp, stop_loss=sl, horizon_ms=int(h * 60_000), latency_s=1)


_W = {}


def _init(store, mem, tmp):
    _W["store"] = store
    _W["con"] = ps.open_connection(memory_limit=mem, threads=1, temp_dir=tmp)


def work_bucket(task):
    """task: (bucket, DataFrame mint, create_ts, decision_ts, fee_b, fee_s, bonding_ts)."""
    b, meta = task
    import build_research_set_v2 as b2
    con = _W["con"]
    files = ps.bucket_files(_W["store"], b)
    flist = "[" + ", ".join(ps._q(f.as_posix()) for f in files) + "]"
    con.register("want_df", meta[["mint", "create_ts"]])
    df = con.execute(
        f"WITH j AS (SELECT s.* FROM read_parquet({flist}, union_by_name=true) s "
        f"JOIN want_df w ON s.mint = w.mint WHERE s.ts_ms <= w.create_ts + {MAX_DEC_MS + PAD_MS}) "
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM j) WHERE rn = 1 "
        f"ORDER BY {ps._FETCH_ORDER}").df()
    con.unregister("want_df")
    m_index = meta.set_index("mint")
    out = []
    mism = 0
    if len(df):
        m_arr = df["mint"].to_numpy()
        cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
        for s_, e_ in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(df)]])):
            mint = m_arr[s_]
            info = m_index.loc[mint]
            g, q_mkt, params, _ = b2.prepare_token(df.iloc[s_:e_])
            ts = g["ts_ms"].to_numpy(dtype="int64")
            closes = b2.bar_close_indices(g["quote_amount"].to_numpy(dtype=float),
                                          float(g["quote_reserve_after"].to_numpy()[0]))
            if len(closes) < 10 or int(ts[closes[9]]) != int(info["decision_ts"]):
                mism += 1
                continue
            d = closes[9]
            bt = info["bonding_ts"]
            bt = None if bt != bt else int(bt)
            row = {"mint": mint}
            for i, (tp, sl, h) in enumerate(GRID):
                o = simulate_anchored(ts, q_mkt, d, params["k"], float(info["fee_b"]), float(info["fee_s"]),
                                      cfg_of(tp, sl, h), None, bt)
                row[f"net_{i}"] = o.net_ret
                row[f"st_{i}"] = o.status
            out.append(row)
    return b, out, mism


def main() -> int:
    holder = []
    try:
        return _main(holder)
    finally:
        for f in holder:
            f.close()


def _main(holder) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sets", default=r"E:\tape_research\sets_v3\discovery")
    ap.add_argument("--expect-from", default=r"E:\tape_research\trials\family_E_v3\results.json")
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--zones", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--out", default=r"E:\tape_research\trials\family_F_v3")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--temp-dir", default=r"E:\tape_store_v2\_duckdb_tmp")
    a = ap.parse_args()
    import pandas as pd
    import run_family_E as rfe

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lf = open(out / "log.txt", "a", encoding="utf-8")
    holder.append(lf)

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    reg = Registry(a.registry)
    specs = [{"family": "F", "entries": "E000 out-of-fold (pred > 0), reproduced", "tp": tp, "sl": sl,
              "horizon_min": h, "size_sol": 2.0, "latency_s": 1, "set": "sets_v3"} for tp, sl, h in GRID]
    if any(spec_hash(s) in {t.get("spec_hash") for t in reg.trials().values()} for s in specs):
        log("REFUSED: family F trials with these specs are already registered (no re-rolls).")
        return 2
    expect = expected_e000(a.expect_from)
    t0 = time.time()
    log(f"\n=== run_family_F {datetime.now(timezone.utc).isoformat()} sets={a.sets} ===")

    # -- 1. reproduce the E000 entry set ------------------------------------
    disc = set(zn.load_zone(a.zones, "discovery"))
    df = rfe.load(Path(a.sets), log)
    df = df[df["K"] == rfe.K]
    if not set(df["mint"]).issubset(disc):
        log("REFUSED: research set contains mints outside the frozen discovery zone")
        return 3
    fit, model_name = rfe.make_model()
    u, cols, X, y, ts, week, day, weeks = rfe.prepare_universe(df)
    pred, thr, _, test_weeks = rfe.walk_forward(u, X, y, ts, week, weeks, fit, log)
    sel = np.isfinite(pred) & (pred > 0)
    n_sel, mean_sel = int(sel.sum()), float(np.nanmean(y[sel]))
    h_sel = entry_hash(u["mint"].to_numpy()[sel])
    log(f"  reproduced E000: n={n_sel:,} mean net {mean_sel:+.4f} sha {h_sel[:16]} (replication: n={expect['n']:,}, "
        f"mean {expect['mean']:+.4f}, sha {expect['mints_sha256'][:16]}) model={model_name}")
    if n_sel != expect["n"] or h_sel != expect["mints_sha256"] or abs(mean_sel - expect["mean"]) > MEAN_TOL:
        log("STOP: the E000 entry set did not reproduce -- family F is not run on a different set.")
        return 5
    ent = u[sel][["mint", "create_ts", "decision_ts", "fee_buy_ratio", "fee_sell_ratio", "fee_ratio",
                  "o_base_net"]].copy()
    ent["week"] = week[sel]
    ent["day"] = day[sel]
    # exactly the fee fallback of build_research_set_v2.process_token_rows
    ent["fee_b"] = ent["fee_buy_ratio"].where(ent["fee_buy_ratio"].notna(),
                                              ent["fee_ratio"].where(ent["fee_ratio"].notna(), 0.0125))
    ent["fee_s"] = ent["fee_sell_ratio"].where(ent["fee_sell_ratio"].notna(), ent["fee_b"])
    ev = ps.load_events(a.store)
    bc = ev[(ev["event_type"] == "bonding_complete") & ev["ts_ms"].notna()].groupby("mint")["ts_ms"].min()
    ent["bonding_ts"] = ent["mint"].map(bc)
    ent["bucket"] = [ps.bucket_of(m) for m in ent["mint"]]
    del df, X, ev

    # -- 2. replay the 24 exit configs --------------------------------------
    tasks = [(int(b), g[["mint", "create_ts", "decision_ts", "fee_b", "fee_s", "bonding_ts"]].reset_index(drop=True))
             for b, g in ent.groupby("bucket")]
    rows, mism = [], 0
    t1 = time.time()
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                             initargs=(a.store, "2GB", a.temp_dir)) as ex:
        for i, (b, r_, m_) in enumerate(ex.map(work_bucket, tasks), start=1):
            rows.extend(r_)
            mism += m_
            el = time.time() - t1
            if i % 4 == 0 or i == len(tasks):
                log(f"  [replay] {i}/{len(tasks)} buckets  tokens={len(rows):,}  elapsed={el:.0f}s "
                    f"ETA~{el / i * (len(tasks) - i):.0f}s")
    res = pd.DataFrame(rows).merge(ent[["mint", "week", "day", "o_base_net"]], on="mint")
    log(f"  replayed {len(res):,} of {len(ent):,} entries; decision mismatches skipped: {mism}")
    base_i = GRID.index((0.6, -0.30, 30))
    diff = np.abs(res[f"net_{base_i}"] - res["o_base_net"])
    agree = np.nanmax(diff)
    log(f"  consistency: config (+60/-30/30) vs the research set's o_base_net, max |diff| = {agree:.2e}")
    if not agree < 1e-6:
        bad = res.assign(diff=diff, replay=res[f"net_{base_i}"], replay_status=res[f"st_{base_i}"])
        bad = bad[bad["diff"] > 1e-6].sort_values("diff", ascending=False)
        bad[["mint", "o_base_net", "replay", "replay_status", "diff"]].to_csv(out / "consistency_diff.csv", index=False)
        log(f"  tokens with |diff| > 1e-6: {len(bad):,} of {len(res):,} ({len(bad) / len(res):.2%}); "
            f"|diff| quantiles p50={bad['diff'].median():.2e} p90={bad['diff'].quantile(.9):.2e}")
        for _, r in bad.head(10).iterrows():
            log(f"    {r['mint']} set={r['o_base_net']:+.6f} replay={r['replay']:+.6f} ({r['replay_status']})")
        log(f"  -> {out / 'consistency_diff.csv'}")
        log("STOP: the replay does not reproduce the research set's outcome for the base config.")
        return 6
    res.to_parquet(out / "replay.parquet", index=False)

    # -- 3. stats + registry ---------------------------------------------------
    yday = res["day"].to_numpy()
    ywk = res["week"].to_numpy()
    allsel = np.ones(len(res), dtype=bool)
    rng = np.random.default_rng(7)
    days_u = np.unique(yday)
    idx_by_day = [np.flatnonzero(yday == d) for d in days_u]
    ids, results = {}, {}
    for i, (tp, sl, h) in enumerate(GRID):
        yv = res[f"net_{i}"].to_numpy(float)
        boot = tr.day_block_bootstrap(yv, allsel, yday, 2000, seed=11)
        means = [np.nanmean(yv[np.concatenate([idx_by_day[j] for j in rng.integers(0, len(days_u), len(days_u))])])
                 for _ in range(2000)]
        p_one = float((1 + np.sum(np.array(means) <= 0)) / 2001)
        wk = [float(np.nanmean(yv[ywk == w])) for w in np.unique(ywk)]
        st = res[f"st_{i}"].value_counts(normalize=True).round(4).to_dict()
        r = {"tp": tp, "sl": sl, "horizon_min": h, "n": int(np.isfinite(yv).sum()), "mean": float(np.nanmean(yv)),
             "median": float(np.nanmedian(yv)), "ci": boot["sel_mean_ci"], "p_one_sided": p_one,
             "weeks_positive": int(sum(1 for v in wk if v > 0)), "weeks": len(wk), "status": st}
        results[i] = r
        ids[i] = reg.register("F", f"F tp{tp:+.2f} sl{sl:+.2f} h{h}m on E000", "an exit rule turns the E000 "
                              "entries profitable", specs[i], zone="discovery", metrics=r, p_value=p_one)
        log(f"  {ids[i]} TP{tp:+.0%} SL{sl:+.0%} {h:2d}m: mean {r['mean']:+.4f} CI{[round(c, 4) for c in r['ci']]} "
            f"median {r['median']:+.4f} weeks>0 {r['weeks_positive']}/{r['weeks']} p={p_one:.4f} {st}")
    q = reg.bh_qvalues()
    log("\n  verdicts (CI>0 AND >=60% weeks positive AND q<0.10):")
    for i, r in results.items():
        ok = r["ci"][0] > 0 and r["weeks_positive"] >= 0.6 * r["weeks"] and q[ids[i]] < 0.10
        reg.update(ids[i], status="passed_discovery" if ok else "screened_out", q_bh=q[ids[i]])
        if ok:
            log(f"    {ids[i]} PASSED discovery")
    log(f"    passed: {sum(1 for i, r in results.items() if reg.trials()[ids[i]]['status'] == 'passed_discovery')} of {len(GRID)}")
    (out / "results.json").write_text(json.dumps({"results": results, "ids": ids}, indent=1, default=float),
                                      encoding="utf-8")
    log(f"  done in {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
