#!/usr/bin/env python3
"""D142: per-decision price paths for the brute-force search (family X).

For every research-set row (K in 5/10/20, universe as families A/C/E:
non-mayhem, standard curve, outcome window collected) store the token's path
from the decision swap on, up to decision + 31 min, in execution order:
rel_ts (ms), slot_rel, q (market virtual SOL reserve), plus the graduation
index. tape/fastexit.exit_grid turns a path into net returns for any exit grid.

    python scripts\\build_paths.py --zone discovery --workers 4
    python scripts\\build_paths.py --zone validation --workers 4   (needs sets_v3\\validation)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tape import pf_store as ps  # noqa: E402

KS = (5, 10, 20)
PATH_MS = 31 * 60_000
MAX_DEC_MS = 60 * 60_000
FETCH_AFTER_MS = MAX_DEC_MS + 40 * 60_000
MONDAY0 = 4 * 86_400_000
WEEK_MS = 7 * 86_400_000
DAY_MS = 86_400_000

_W = {}


def _init(store, out_dir, mem, tmp):
    _W.update(store=store, out_dir=out_dir)
    _W["con"] = ps.open_connection(memory_limit=mem, threads=1, temp_dir=tmp)


def work_bucket(task):
    b, meta = task
    import pandas as pd
    import build_research_set_v2 as b2
    out_meta = Path(_W["out_dir"]) / f"meta-b{b:02d}.parquet"
    out_arr = Path(_W["out_dir"]) / f"path-b{b:02d}.npz"
    if out_meta.exists() and out_arr.exists():
        return b, "skipped", 0, 0
    con = _W["con"]
    files = ps.bucket_files(_W["store"], b)
    flist = "[" + ", ".join(ps._q(f.as_posix()) for f in files) + "]"
    want = meta[["mint", "create_ts"]].drop_duplicates("mint")
    con.register("want_df", want)
    df = con.execute(
        f"WITH j AS (SELECT s.* FROM read_parquet({flist}, union_by_name=true) s "
        f"JOIN want_df w ON s.mint = w.mint WHERE s.ts_ms <= w.create_ts + {FETCH_AFTER_MS}) "
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM j) WHERE rn = 1 "
        f"ORDER BY {ps._FETCH_ORDER}").df()
    con.unregister("want_df")
    by_mint = {m: g for m, g in meta.groupby("mint")}
    rows, ts_l, sl_l, q_l = [], [], [], []
    off, mism = 0, 0
    if len(df):
        m_arr = df["mint"].to_numpy()
        cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
        for s_, e_ in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(df)]])):
            mint = m_arr[s_]
            g, q_mkt, params, _ = b2.prepare_token(df.iloc[s_:e_])
            ts = g["ts_ms"].to_numpy(dtype="int64")
            slots = g["slot"].to_numpy(dtype="int64")
            closes = b2.bar_close_indices(g["quote_amount"].to_numpy(dtype=float),
                                          float(g["quote_reserve_after"].to_numpy()[0]))
            for _, r in by_mint[mint].iterrows():
                K = int(r["K"])
                if len(closes) < K or int(ts[closes[K - 1]]) != int(r["decision_ts"]):
                    mism += 1
                    continue
                d = closes[K - 1]
                end = int(np.searchsorted(ts, ts[d] + PATH_MS, side="right"))
                bt = r["bonding_ts"]
                grad_abs = len(ts) if bt != bt else int(np.searchsorted(ts, int(bt), side="left"))
                ln = end - d
                grad = int(np.clip(grad_abs - d, 0, ln))           # first path index at/after graduation
                ts_l.append((ts[d:end] - ts[d]).astype(np.int32))
                sl_l.append((slots[d:end] - slots[d]).astype(np.int32))
                q_l.append(q_mkt[d:end].astype(np.float32))
                rows.append({"mint": mint, "K": K, "create_ts": int(r["create_ts"]), "decision_ts": int(ts[d]),
                             "day": int(ts[d]) // DAY_MS, "week": (int(r["create_ts"]) - MONDAY0) // WEEK_MS,
                             "k": float(params["k"]), "fee_b": float(r["fee_b"]), "fee_s": float(r["fee_s"]),
                             "grad": int(grad), "offset": off, "length": ln})
                off += ln
    pm = pd.DataFrame(rows)
    tmp_a = out_arr.with_suffix(".tmp.npz")
    np.savez(tmp_a, rel_ts=np.concatenate(ts_l) if ts_l else np.zeros(0, np.int32),
             slot_rel=np.concatenate(sl_l) if sl_l else np.zeros(0, np.int32),
             q=np.concatenate(q_l) if q_l else np.zeros(0, np.float32))
    os.replace(tmp_a, out_arr)
    tmp_m = out_meta.with_suffix(".tmp")
    pm.to_parquet(tmp_m, index=False)
    os.replace(tmp_m, out_meta)
    return b, "done", len(rows), mism


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--sets", default=r"E:\tape_research\sets_v3")
    ap.add_argument("--zone", default="discovery", choices=["discovery", "validation"])
    ap.add_argument("--out", default=r"E:\tape_research\paths_v1")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--temp-dir", default=r"E:\tape_store_v2\_duckdb_tmp")
    a = ap.parse_args()
    import pandas as pd
    import pyarrow.parquet as pq

    out_dir = Path(a.out) / a.zone
    out_dir.mkdir(parents=True, exist_ok=True)
    lf = open(out_dir / "build_log.txt", "a", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    try:
        t0 = time.time()
        log(f"\n=== build_paths {datetime.now(timezone.utc).isoformat()} zone={a.zone} ===")
        parts = sorted((Path(a.sets) / a.zone).glob("part-b*.parquet"))
        if len(parts) < ps.N_BUCKETS:
            log(f"STOP: research set {a.sets}\\{a.zone} incomplete ({len(parts)} parts) -- "
                f"run build_research_set_v2.py --zone {a.zone} first")
            return 4
        cols = ["mint", "K", "create_ts", "decision_ts", "is_mayhem", "chk_curve_fitted", "o_base_status",
                "fee_buy_ratio", "fee_sell_ratio", "fee_ratio"]
        s = pd.concat([pq.read_table(p, columns=cols).to_pandas() for p in parts], ignore_index=True)
        s = s[s["K"].isin(KS) & (s["is_mayhem"] == 0) & (~s["chk_curve_fitted"].astype(bool))
              & (~s["o_base_status"].isin(["CENS", "NOENTRY"]))].copy()
        s["fee_b"] = s["fee_buy_ratio"].where(s["fee_buy_ratio"].notna(),
                                              s["fee_ratio"].where(s["fee_ratio"].notna(), 0.0125))
        s["fee_s"] = s["fee_sell_ratio"].where(s["fee_sell_ratio"].notna(), s["fee_b"])
        ev = ps.load_events(a.store)
        bc = ev[(ev["event_type"] == "bonding_complete") & ev["ts_ms"].notna()].groupby("mint")["ts_ms"].min()
        del ev
        s["bonding_ts"] = s["mint"].map(bc)
        log(f"  decisions: {len(s):,} ({s['K'].value_counts().sort_index().to_dict()})")
        s["bucket"] = [ps.bucket_of(m) for m in s["mint"]]
        tasks = [(int(b), g.drop(columns=["bucket"]).reset_index(drop=True)) for b, g in s.groupby("bucket")]
        t1 = time.time()
        tot, mism = 0, 0
        with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                                 initargs=(a.store, str(out_dir), "2GB", a.temp_dir)) as ex:
            for i, (b, st, n, m) in enumerate(ex.map(work_bucket, tasks), start=1):
                tot += n
                mism += m
                el = time.time() - t1
                log(f"  [bucket {b:02d} {st}] {i}/{len(tasks)} paths={tot:,} elapsed={el:.0f}s "
                    f"ETA~{el / i * (len(tasks) - i):.0f}s")
        log(f"  paths written: {tot:,}; decision mismatches: {mism}; done in {time.time() - t0:.0f}s -> {out_dir}")
        return 0
    finally:
        lf.close()


if __name__ == "__main__":
    raise SystemExit(main())
