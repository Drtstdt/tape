#!/usr/bin/env python3
"""D140: latency sensitivity -- is the 1 s reaction delay what kills the edge?

DIAGNOSTIC (registry family T), not a hypothesis trial: the entry sets and
exits are the ones already registered; only the reaction delay changes.
  sets     E000 and FLOOR (from trials/family_H_v3/replay.parquet), W top1 /
           top5 first-buy triggers (trials/family_W/replay.parquet, 2 SOL)
  exits    E000/FLOOR: base +60/-30/30 min and the best F config +30/-30/10 min;
           W: mirror and fixed (as registered)
  delays   1 s (as registered -- must reproduce the registered outcome), 0 s
           (same timestamp second), and SLOT-counted: 0 slots (same block, after
           the trigger), 1 slot (~0.4 s), 2 slots, 4 slots
For every set x exit x delay: mean net with a day-block CI, and the PAIRED
change versus 1 s (same tokens) with its CI. Also: how many slots 1 s really is.

    python scripts\\diag_latency.py --workers 4
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
from tape.outcomes import TradeConfig, simulate_anchored  # noqa: E402
from tape.registry import Registry  # noqa: E402

DELAYS = [("s", 1), ("s", 0), ("slot", 0), ("slot", 1), ("slot", 2), ("slot", 4)]
EXITS_E = {"base": (0.60, -0.30, 30), "f_best": (0.30, -0.30, 10)}
EXITS_W = {"mirror": (1e9, -1e9, 30), "fixed": (0.60, -0.30, 30)}
MAX_DEC_MS = 60 * 60_000
FETCH_AFTER_MS = MAX_DEC_MS + 40 * 60_000


def label(d):
    return f"{d[1]}{'s' if d[0] == 's' else 'slot'}"


def run_configs(ts, q_mkt, slots, dec, k, fb, fs, exits, bt, exit_idx=None):
    out = {}
    for name, (tp, sl, h) in exits.items():
        for d in DELAYS:
            cfg = TradeConfig(size_sol=2.0, take_profit=tp, stop_loss=sl, horizon_ms=h * 60_000,
                              latency_s=d[1] if d[0] == "s" else 1)
            kw = {"slots": slots, "latency_slots": d[1]} if d[0] == "slot" else {}
            o = simulate_anchored(ts, q_mkt, dec, k, fb, fs, cfg, None, bt,
                                  exit_idx=(exit_idx if name == "mirror" else None), **kw)
            out[f"{name}|{label(d)}"] = o.net_ret
    return out


_W = {}


def _init(store, mem, tmp):
    _W["store"] = store
    _W["con"] = ps.open_connection(memory_limit=mem, threads=1, temp_dir=tmp)


def work_bucket(task):
    b, metaE, metaW = task
    import pandas as pd
    import build_research_set_v2 as b2
    con = _W["con"]
    files = ps.bucket_files(_W["store"], b)
    flist = "[" + ", ".join(ps._q(f.as_posix()) for f in files) + "]"
    want = pd.concat([metaE[["mint", "create_ts"]], metaW[["mint", "create_ts"]]]).drop_duplicates("mint")
    con.register("want_df", want)
    df = con.execute(
        f"WITH j AS (SELECT s.* FROM read_parquet({flist}, union_by_name=true) s "
        f"JOIN want_df w ON s.mint = w.mint WHERE s.ts_ms <= w.create_ts + {FETCH_AFTER_MS}) "
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM j) WHERE rn = 1 "
        f"ORDER BY {ps._FETCH_ORDER}").df()
    con.unregister("want_df")
    E = metaE.set_index("mint")
    Wg = {m: g for m, g in metaW.groupby("mint")}
    rows, slot_gap, mism = [], [], 0
    if len(df):
        m_arr = df["mint"].to_numpy()
        cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
        for s_, e_ in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(df)]])):
            mint = m_arr[s_]
            g, q_mkt, params, _ = b2.prepare_token(df.iloc[s_:e_])
            ts = g["ts_ms"].to_numpy(dtype="int64")
            slots = g["slot"].to_numpy(dtype="int64")
            if mint in E.index:
                info = E.loc[mint]
                closes = b2.bar_close_indices(g["quote_amount"].to_numpy(dtype=float),
                                              float(g["quote_reserve_after"].to_numpy()[0]))
                if len(closes) < 10 or int(ts[closes[9]]) != int(info["decision_ts"]):
                    mism += 1
                else:
                    d = closes[9]
                    bt = None if info["bonding_ts"] != info["bonding_ts"] else int(info["bonding_ts"])
                    r = run_configs(ts, q_mkt, slots, d, params["k"], float(info["fee_b"]), float(info["fee_s"]),
                                    EXITS_E, bt)
                    e1 = d
                    while e1 + 1 < len(ts) and ts[e1 + 1] <= ts[d] + 1000:
                        e1 += 1
                    slot_gap.append(int(slots[e1] - slots[d]))
                    rows.append({"group": "E", "mint": mint, "day": int(ts[d]) // tr.DAY_MS,
                                 "in_E000": bool(info["in_E000"]), "in_FLOOR": bool(info["in_FLOOR"]),
                                 "registered": float(info["o_base_net"]), **r})
            if mint in Wg:
                buy = g["side"].to_numpy() == "buy"
                qa = g["quote_amount"].to_numpy(dtype=float)
                ba = g["base_amount"].to_numpy(dtype=float)
                wal = g["wallet"].to_numpy(dtype=object)
                for _, w in Wg[mint].iterrows():
                    idx = np.flatnonzero((wal == w["leader"]) & buy & (qa >= wl.MIN_TRIGGER_SOL)
                                         & (ts == int(w["trig_ts"])))
                    if len(idx) == 0 or params["fitted"]:
                        mism += 1
                        continue
                    t = int(idx[0])
                    bt = None if w["bonding_ts"] != w["bonding_ts"] else int(w["bonding_ts"])
                    r = run_configs(ts, q_mkt, slots, t, params["k"], float(w["fee_b"]), float(w["fee_s"]),
                                    EXITS_W, bt, exit_idx=wl.mirror_exit_index(buy, ba, wal, t))
                    rows.append({"group": f"W_{w['cls']}", "mint": mint, "day": int(ts[t]) // tr.DAY_MS,
                                 "registered_mirror": float(w["net_mirror"]),
                                 "registered_fixed": float(w["net_fixed"]), **r})
    return b, rows, slot_gap, mism


def summarize(res, log):
    import pandas as pd
    out = {}
    groups = [("E000", res[(res["group"] == "E") & res["in_E000"].fillna(False).astype(bool)], EXITS_E),
              ("FLOOR", res[(res["group"] == "E") & res["in_FLOOR"].fillna(False).astype(bool)], EXITS_E),
              ("W_top1", res[res["group"] == "W_top1"], EXITS_W),
              ("W_top5", res[res["group"] == "W_top5"], EXITS_W)]
    for gname, sub, exits in groups:
        if len(sub) == 0:
            continue
        day = sub["day"].to_numpy()
        ones = np.ones(len(sub), bool)
        for ex in exits:
            ref = sub[f"{ex}|1s"].to_numpy(float)
            for d in DELAYS:
                col = f"{ex}|{label(d)}"
                y = sub[col].to_numpy(float)
                ok = np.isfinite(y) & np.isfinite(ref)
                ci = tr.day_block_bootstrap(y[ok], ones[ok], day[ok], 1000, seed=51)["sel_mean_ci"]
                dd = y[ok] - ref[ok]
                dci = tr.day_block_bootstrap(dd, ones[ok], day[ok], 1000, seed=52)["sel_mean_ci"]
                key = f"{gname}|{col}"
                out[key] = {"n": int(ok.sum()), "mean": float(np.mean(y[ok])), "ci": ci,
                            "delta_vs_1s": float(np.mean(dd)), "delta_ci": dci}
                r = out[key]
                log(f"  {gname:6s} {ex:6s} {label(d):6s} n={r['n']:,} mean {r['mean']:+.4f} "
                    f"CI{[round(v, 4) for v in ci]}  vs 1 s {r['delta_vs_1s']:+.4f} CI{[round(v, 4) for v in dci]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--sets", default=r"E:\tape_research\sets_v3\discovery")
    ap.add_argument("--h-replay", default=r"E:\tape_research\trials\family_H_v3\replay.parquet")
    ap.add_argument("--w-replay", default=r"E:\tape_research\trials\family_W\replay.parquet")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--out", default=r"E:\tape_research\trials\diag_latency")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--temp-dir", default=r"E:\tape_store_v2\_duckdb_tmp")
    a = ap.parse_args()
    import pandas as pd
    import pyarrow.parquet as pq

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lf = open(out / "log.txt", "a", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    try:
        t0 = time.time()
        log(f"\n=== diag_latency {datetime.now(timezone.utc).isoformat()} delays={[label(d) for d in DELAYS]} ===")
        h = pd.read_parquet(a.h_replay, columns=["mint", "in_E000", "in_FLOOR"])
        parts = sorted(Path(a.sets).glob("part-b*.parquet"))
        cols = ["mint", "K", "create_ts", "decision_ts", "fee_buy_ratio", "fee_sell_ratio", "fee_ratio", "o_base_net"]
        s = pd.concat([pq.read_table(p, columns=cols).to_pandas() for p in parts], ignore_index=True)
        s = s[s["K"] == 10].merge(h, on="mint")
        s["fee_b"] = s["fee_buy_ratio"].where(s["fee_buy_ratio"].notna(),
                                              s["fee_ratio"].where(s["fee_ratio"].notna(), 0.0125))
        s["fee_s"] = s["fee_sell_ratio"].where(s["fee_sell_ratio"].notna(), s["fee_b"])
        ev = ps.load_events(a.store)
        cr = (ev[(ev["event_type"] == "create") & ev["ts_ms"].notna()].sort_values("ts_ms")
              .drop_duplicates("mint").set_index("mint")["ts_ms"])
        bc = ev[(ev["event_type"] == "bonding_complete") & ev["ts_ms"].notna()].groupby("mint")["ts_ms"].min()
        del ev
        s["bonding_ts"] = s["mint"].map(bc)
        log(f"  E000/FLOOR tokens: {len(s):,} (E000 {int(s['in_E000'].sum()):,}, FLOOR {int(s['in_FLOOR'].sum()):,})")

        w = pd.read_parquet(a.w_replay)
        w = w[(w["lat"] == 1) & (w["size"] == 2.0) & w["cls"].isin(["top1", "top5"])
              & ~w["status"].isin(["CENS", "NOENTRY"])]
        wm = w[w["exit"] == "mirror"][["mint", "cls", "trig_ts", "leader", "fee_b", "fee_s", "net"]].rename(
            columns={"net": "net_mirror"})
        wf = w[w["exit"] == "fixed"][["mint", "cls", "net"]].rename(columns={"net": "net_fixed"})
        w = wm.merge(wf, on=["mint", "cls"])
        w["create_ts"] = w["mint"].map(cr).astype("int64")
        w["bonding_ts"] = w["mint"].map(bc)
        log(f"  W triggers: {len(w):,} ({w['cls'].value_counts().to_dict()})")

        s["bucket"] = [ps.bucket_of(m) for m in s["mint"]]
        w["bucket"] = [ps.bucket_of(m) for m in w["mint"]]
        sg, wg = dict(list(s.groupby("bucket"))), dict(list(w.groupby("bucket")))
        tasks = [(b, sg.get(b, s.iloc[:0]), wg.get(b, w.iloc[:0])) for b in range(ps.N_BUCKETS)]
        rows, gaps, mism = [], [], 0
        t1 = time.time()
        with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                                 initargs=(a.store, "2GB", a.temp_dir)) as ex:
            for i, (b, r_, g_, m_) in enumerate(ex.map(work_bucket, tasks), start=1):
                rows.extend(r_)
                gaps.extend(g_)
                mism += m_
                if i % 4 == 0 or i == len(tasks):
                    el = time.time() - t1
                    log(f"  [replay] {i}/{len(tasks)} buckets rows={len(rows):,} elapsed={el:.0f}s "
                        f"ETA~{el / i * (len(tasks) - i):.0f}s")
        res = pd.DataFrame(rows)
        res.to_parquet(out / "replay.parquet", index=False)
        log(f"  rows {len(res):,}; mismatches skipped {mism}")
        gq = np.quantile(gaps, [.1, .25, .5, .75, .9]).tolist() if gaps else []
        log(f"  slots between the decision swap and the 1 s entry state: quantiles(.1,.25,.5,.75,.9) = {gq}")
        e = res[res["group"] == "E"]
        dE = float(np.nanmax(np.abs(e["base|1s"] - e["registered"]))) if len(e) else float("nan")
        ww = res[res["group"].str.startswith("W")]
        dW = float(np.nanmax(np.abs(np.r_[ww["mirror|1s"] - ww["registered_mirror"],
                                          ww["fixed|1s"] - ww["registered_fixed"]]))) if len(ww) else float("nan")
        log(f"  consistency at 1 s vs registered outcomes: E max|diff| {dE:.2e}, W max|diff| {dW:.2e}")
        if not (dE < 1e-6 and dW < 1e-6):
            log("  WARNING: 1 s does not reproduce the registered outcomes -- read deltas with care")
        log("\n  mean net by delay (2 SOL); 'vs 1 s' = paired change on the same tokens:")
        summ = summarize(res, log)
        reg = Registry(a.registry)
        tid = reg.register("T", "latency sensitivity (seconds and slots) on E000/FLOOR/W",
                           "is the 1 s reaction delay what kills the edge?",
                           {"diagnostic": "D140", "delays": [label(d) for d in DELAYS]}, zone="discovery",
                           status="diagnostic", metrics={"slot_gap_quantiles": gq, "consistency": [dE, dW],
                                                         "summary": summ})
        (out / "results.json").write_text(json.dumps({"diagnostic": tid, "slot_gap_quantiles": gq,
                                                      "consistency": [dE, dW], "summary": summ},
                                                     indent=1, default=float), encoding="utf-8")
        log(f"  recorded as {tid}; done in {time.time() - t0:.0f}s -> {out}")
        return 0
    finally:
        lf.close()


if __name__ == "__main__":
    raise SystemExit(main())
