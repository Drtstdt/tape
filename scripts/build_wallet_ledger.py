#!/usr/bin/env python3
"""D136: wallet ledger for family W (tape/wallets.py::ledger_frame).

One row per (wallet, token) with the wallet's flows in the token's first 24 h
and the leftover position marked at the token's last price in that window.
Built ONLY from tokens of the given zones (default discovery + validation);
the sealed zones are never loaded. Read-only on the store; one parquet part per
bucket; a rerun skips finished parts (resumable).

    python scripts\\build_wallet_ledger.py --workers 4
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape import pf_store as ps  # noqa: E402
from tape import wallets as wl  # noqa: E402
from tape import zones as zn  # noqa: E402

_W = {}


def _init(store, out_dir, mem, tmp):
    _W.update(store=store, out_dir=out_dir)
    _W["con"] = ps.open_connection(memory_limit=mem, threads=1, temp_dir=tmp)


def work_bucket(task):
    b, meta = task                         # meta: mint, create_ts
    out_path = Path(_W["out_dir"]) / f"part-b{b:02d}.parquet"
    if out_path.exists():
        return b, "skipped", {}
    t0 = time.time()
    con = _W["con"]
    files = ps.bucket_files(_W["store"], b)
    flist = "[" + ", ".join(ps._q(f.as_posix()) for f in files) + "]"
    con.register("want_df", meta[["mint", "create_ts"]])
    df = con.execute(
        f"WITH j AS (SELECT s.mint, s.wallet, s.side, s.ts_ms, s.slot, s.sig, s.quote_amount, s.fee_sol, "
        f"s.base_amount, s.price, s.real_quote_reserve_after, s.source, s.src "
        f"FROM read_parquet({flist}, union_by_name=true) s JOIN want_df w ON s.mint = w.mint "
        f"WHERE s.ts_ms >= w.create_ts AND s.ts_ms <= w.create_ts + {wl.SETTLE_MS}) "
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM j) WHERE rn = 1 "
        f"ORDER BY {ps._FETCH_ORDER}").df()
    con.unregister("want_df")
    t_q = time.time() - t0
    led = wl.ledger_frame(df, dict(zip(meta["mint"], meta["create_ts"].astype("int64"))))
    tmp = out_path.with_suffix(".tmp")
    led.to_parquet(tmp, index=False)
    os.replace(tmp, out_path)
    return b, "done", {"swaps": int(len(df)), "rows": int(len(led)), "tokens": int(led["mint"].nunique()),
                       "query_s": t_q, "total_s": time.time() - t0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--zones", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--zone-list", default="discovery,validation")
    ap.add_argument("--out", default=r"E:\tape_research\wallets_v1\ledger")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--worker-memory", default="2GB")
    ap.add_argument("--temp-dir", default=r"E:\tape_store_v2\_duckdb_tmp")
    a = ap.parse_args()
    import pandas as pd

    zones = [z.strip() for z in a.zone_list.split(",") if z.strip()]
    if any(z.startswith("sealed") for z in zones):
        print("REFUSED: the sealed zones are opened only via Registry.open_sealed (D123)")
        return 3
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_f = open(out_dir / "build_log.txt", "a", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        log_f.write(m + "\n")
        log_f.flush()

    try:
        t0 = time.time()
        log(f"\n=== build_wallet_ledger {datetime.now(timezone.utc).isoformat()} zones={zones} ===")
        mints = set()
        for z in zones:
            m = zn.load_zone(a.zones, z)               # verifies the frozen sha256
            log(f"  zone {z}: {len(m):,} tokens (sha256 verified)")
            mints |= set(m)
        ev = ps.load_events(a.store)
        cr = (ev[(ev["event_type"] == "create") & ev["ts_ms"].notna()].sort_values("ts_ms")
              .drop_duplicates("mint")[["mint", "ts_ms"]].rename(columns={"ts_ms": "create_ts"}))
        cr = cr[cr["mint"].isin(mints)].copy()
        cr["create_ts"] = cr["create_ts"].astype("int64")
        cr["bucket"] = [ps.bucket_of(m) for m in cr["mint"]]
        del ev
        tasks = [(int(b), g.drop(columns=["bucket"]).reset_index(drop=True)) for b, g in cr.groupby("bucket")]
        log(f"  tokens with a create event: {len(cr):,}; tasks {len(tasks)} buckets; parts already done: "
            f"{sum(1 for b, _ in tasks if (out_dir / f'part-b{b:02d}.parquet').exists())}")
        t1 = time.time()
        tot = {"swaps": 0, "rows": 0, "tokens": 0}
        with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                                 initargs=(a.store, str(out_dir), a.worker_memory, a.temp_dir)) as ex:
            for i, (b, st, s) in enumerate(ex.map(work_bucket, tasks), start=1):
                for k in tot:
                    tot[k] += s.get(k, 0)
                el = time.time() - t1
                log(f"  [bucket {b:02d} {st}] {i}/{len(tasks)}  swaps={s.get('swaps', 0):,} rows={s.get('rows', 0):,} "
                    f"({s.get('total_s', 0):.0f}s)  elapsed={el:.0f}s ETA~{el / i * (len(tasks) - i):.0f}s")
        parts = sorted(out_dir.glob("part-b*.parquet"))
        led = pd.concat([pd.read_parquet(p, columns=["wallet", "mint", "pnl", "sol_in"]) for p in parts],
                        ignore_index=True)
        per_w = led.groupby("wallet").agg(n=("mint", "size"), pnl=("pnl", "sum"))
        log(f"\n  ledger rows {len(led):,}; wallets {len(per_w):,}; tokens {led['mint'].nunique():,}")
        log(f"  wallets with >= {wl.MIN_N} tokens: {(per_w['n'] >= wl.MIN_N).sum():,}; "
            f"total pnl (all wallets, SOL) {led['pnl'].sum():+,.1f}; sol_in {led['sol_in'].sum():,.1f}")
        log(f"  per-wallet pnl quantiles (n>={wl.MIN_N}): "
            f"{per_w.loc[per_w['n'] >= wl.MIN_N, 'pnl'].quantile([.01, .1, .5, .9, .99]).round(3).to_dict()}")
        log("  NOTE: whole-period numbers above are descriptive only; W uses weekly point-in-time snapshots.")
        log(f"  done in {time.time() - t0:.0f}s -> {out_dir}")
        return 0
    finally:
        log_f.close()


if __name__ == "__main__":
    raise SystemExit(main())
