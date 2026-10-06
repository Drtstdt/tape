#!/usr/bin/env python3
"""D120: build the pumpfundata store v2 on E: from the raw hourly files on F:.

Read-only on F:\\pumpfundata and on data/. Writes only under --out.
Resumable: re-running skips everything already done (manifest.jsonl +
compacted.json). Safe to Ctrl+C at any point.

    # smoke test on one month first (minutes):
    python scripts\\build_pf_store.py --months 2026-02
    # everything:
    python scripts\\build_pf_store.py

Stage 1 (parallel, one task per raw file): raw parquet -> canonical swaps
(vectorised copy of tape/sources/pumpfundata.py::_row_to_swap) split into 8
bucket groups + create/bonding_complete events + one manifest line.
Stage 2 (per month, sequential): staged groups -> 64 bucket files sorted by
(mint, ts_ms, slot, sig), events, mint index; row counts verified against
the manifest before the month is installed; staging of that month deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape import pf_store as ps  # noqa: E402
from tape.sources.pumpfundata import REQUIRED_COLUMNS  # noqa: E402


def _swap_schema():
    import pyarrow as pa
    s, f, i = pa.string(), pa.float64(), pa.int64()
    return pa.schema([("mint", s), ("venue", s), ("pool", s), ("ts_ms", i), ("slot", i),
                      ("sig", s), ("side", s), ("base_amount", f), ("quote_amount", f),
                      ("quote_mint", s), ("price", f), ("wallet", s),
                      ("base_reserve_after", f), ("quote_reserve_after", f), ("source", s),
                      ("fee_sol", f), ("real_base_reserve_after", f),
                      ("real_quote_reserve_after", f), ("is_mayhem_mode", pa.bool_()),
                      ("bucket", pa.int16()), ("src", s)])


def _event_schema():
    import pyarrow as pa
    s, f = pa.string(), pa.float64()
    return pa.schema([("event_type", s), ("mint", s), ("ts_ms", f), ("slot", f), ("sig", s),
                      ("creator", s), ("token_total_supply", f), ("is_mayhem_mode", pa.bool_()),
                      ("can_be_frozen", pa.bool_()), ("src", s)])


def _to_str_or_none(series):
    return series.map(lambda x: None if x is None or (isinstance(x, float) and x != x) else str(x))


def _to_bool_or_none(series):
    return series.map(lambda x: None if x is None or (isinstance(x, float) and x != x) else bool(x))


def staged_dir(out_root: Path, rel: str) -> Path:
    d, h = ps.parse_raw_relpath(rel)
    return out_root / "staging" / f"month={ps.month_of(d)}" / f"f={ps.file_tag(d, h)}"


# ---------------------------------------------------------------------------
# Stage 1 worker (module level: Windows spawn)
# ---------------------------------------------------------------------------

def stage_one(task):
    raw_root, rel, size, mtime_ns, out_root = task
    raw_root, out_root = Path(raw_root), Path(out_root)
    t0 = time.time()
    rec = {"file": rel, "size": size, "mtime_ns": mtime_ns,
           "staged_utc": datetime.now(timezone.utc).isoformat()}
    try:
        import pandas as pd
        import pyarrow as pa
        import pyarrow.parquet as pq
        date, hour = ps.parse_raw_relpath(rel)
        df = pd.read_parquet(raw_root / rel)
        missing = sorted(REQUIRED_COLUMNS - set(df.columns))
        rec["columns"] = sorted(map(str, df.columns))
        if missing:
            rec.update(status="schema_mismatch", missing=missing, elapsed_s=time.time() - t0)
            return rec
        rec["ts_dtype"] = str(df["timestamp"].dtype)
        lo, _ = ps.hour_window_ms(date, hour)
        swaps, events, stats = ps.convert_raw_frame(df, ps.file_tag(date, hour), lo)
        del df
        rec.update(stats)
        status = ps.classify_file(stats)
        rec["status"] = status
        if status == "ts_suspect":
            rec["elapsed_s"] = time.time() - t0
            return rec

        final = staged_dir(out_root, rel)
        tmp = final.with_name(final.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        for c in ("mint", "pool", "sig", "wallet"):
            swaps[c] = _to_str_or_none(swaps[c])
        swaps["is_mayhem_mode"] = _to_bool_or_none(swaps["is_mayhem_mode"])
        sch = _swap_schema()
        grp = (swaps["bucket"] // (ps.N_BUCKETS // ps.N_GROUPS)).to_numpy()
        for g in range(ps.N_GROUPS):
            part = swaps[grp == g]
            pq.write_table(pa.Table.from_pandas(part[ps.SWAP_COLUMNS], schema=sch, preserve_index=False),
                           tmp / f"grp={g}.parquet", compression="zstd")
        for c in ("mint", "sig", "creator"):
            events[c] = _to_str_or_none(events[c])
        for c in ("is_mayhem_mode", "can_be_frozen"):
            events[c] = _to_bool_or_none(events[c])
        pq.write_table(pa.Table.from_pandas(events[ps.EVENT_COLUMNS], schema=_event_schema(),
                                            preserve_index=False),
                       tmp / "events.parquet", compression="zstd")
        if final.exists():
            shutil.rmtree(final)
        os.replace(tmp, final)
        rec["elapsed_s"] = time.time() - t0
        return rec
    except Exception as e:  # noqa: BLE001 -- recorded in the manifest, never swallowed silently
        rec.update(status="read_error", error=f"{type(e).__name__}: {e}",
                   trace=traceback.format_exc()[-1500:], elapsed_s=time.time() - t0)
        return rec


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------

def compact_month(con, out_root: Path, month: str, rels, manifest, log, keep_staging: bool):
    t_m = time.time()
    done = [r for r in rels if manifest[r]["status"] in ps.DONE_STATUSES]
    expected = sum(int(manifest[r]["n_swaps"]) for r in done)
    build = out_root / "months" / f"month={month}.building"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    dirs = [staged_dir(out_root, r) for r in done]
    missing = [d for d in dirs if not d.exists()]
    if missing:
        raise RuntimeError(f"{month}: {len(missing)} staged dir(s) missing, e.g. {missing[0]}")
    per_g = ps.N_BUCKETS // ps.N_GROUPS
    written = 0
    idx_parts = []
    for g in range(ps.N_GROUPS):
        t_g = time.time()
        files = "[" + ", ".join(ps._q((d / f"grp={g}.parquet").as_posix()) for d in dirs) + "]"
        con.execute(f"CREATE OR REPLACE TEMP TABLE t AS SELECT * FROM read_parquet({files}, union_by_name=true)")
        ng = con.execute("SELECT count(*) FROM t").fetchone()[0]
        outs = []
        for b in range(g * per_g, (g + 1) * per_g):
            target = build / f"bucket={b:02d}.parquet"
            con.execute(f"COPY (SELECT * FROM t WHERE bucket = {b} ORDER BY mint, ts_ms, slot, sig) "
                        f"TO {ps._q(target.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 25000)")
            outs.append(target)
        idx_parts.append(con.execute(
            "SELECT mint, bucket, min(ts_ms) AS first_ts, max(ts_ms) AS last_ts, count(*) AS n_raw "
            "FROM t GROUP BY mint, bucket").df())
        con.execute("DROP TABLE t")
        olist = "[" + ", ".join(ps._q(o.as_posix()) for o in outs) + "]"
        nw = con.execute(f"SELECT count(*) FROM read_parquet({olist})").fetchone()[0]
        if nw != ng:
            raise RuntimeError(f"{month} group {g}: staged {ng} rows, wrote {nw}")
        written += nw
        el = time.time() - t_m
        log(f"  [compact {month}] group {g + 1}/{ps.N_GROUPS}: {ng:,} rows in {time.time() - t_g:.0f}s  "
            f"elapsed={el:.0f}s ETA~{el / (g + 1) * (ps.N_GROUPS - g - 1):.0f}s")
    if written != expected:
        raise RuntimeError(f"{month}: manifest says {expected} swaps, compacted {written} -- NOT installed")
    ev = "[" + ", ".join(ps._q((d / "events.parquet").as_posix()) for d in dirs) + "]"
    con.execute(f"COPY (SELECT * FROM read_parquet({ev}, union_by_name=true) ORDER BY ts_ms, mint) "
                f"TO {ps._q((build / 'events.parquet').as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)")
    import pandas as pd
    pd.concat(idx_parts, ignore_index=True).to_parquet(build / "mint_index.parquet", index=False)
    final = out_root / "months" / f"month={month}"
    if final.exists():
        shutil.rmtree(final)
    os.replace(build, final)
    if not keep_staging:
        shutil.rmtree(out_root / "staging" / f"month={month}", ignore_errors=True)
    log(f"  [compact {month}] installed: {written:,} rows, {len(done)} file(s) "
        f"in {time.time() - t_m:.0f}s; staging {'kept' if keep_staging else 'deleted'}")
    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _dir_gb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e9 if p.exists() else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--raw-dir", default=r"F:\pumpfundata")
    ap.add_argument("--exchange", default="pump_fun")
    ap.add_argument("--out", default=r"E:\tape_store_v2")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--months", default=None, help="comma list, e.g. 2026-02,2026-03 (default: all)")
    ap.add_argument("--stage1-only", action="store_true")
    ap.add_argument("--retry-skipped", action="store_true",
                    help="re-stage files recorded as ts_suspect/schema_mismatch/read_error")
    ap.add_argument("--keep-staging", action="store_true")
    ap.add_argument("--min-free-gb", type=float, default=15.0)
    ap.add_argument("--memory-limit", default="8GB", help="DuckDB, stage 2")
    ap.add_argument("--threads", type=int, default=4, help="DuckDB, stage 2")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log_f = open(out / "build_log.txt", "a", encoding="utf-8")

    def log(msg=""):
        print(msg, flush=True)
        log_f.write(msg + "\n")
        log_f.flush()

    def free_gb():
        return shutil.disk_usage(out).free / 1e9

    log(f"\n=== build_pf_store {datetime.now(timezone.utc).isoformat()} pid={os.getpid()} ===")
    log(f"  raw={a.raw_dir}\\{a.exchange} (read-only)  out={out}  workers={a.workers}  free={free_gb():.1f} GB")
    from tape.swaps_cache import commit_preflight, parse_gb
    commit_preflight(parse_gb(a.memory_limit) + a.workers * 1.5 + 2.0, "build_pf_store", log)

    base = Path(a.raw_dir) / a.exchange
    t0 = time.time()
    raw = {}
    for p in sorted(base.glob("date=*/hour=*.parquet")):
        st = p.stat()
        raw[p.relative_to(base).as_posix()] = (st.st_size, st.st_mtime_ns)
    months_filter = set(a.months.split(",")) if a.months else None
    if months_filter:
        raw = {k: v for k, v in raw.items() if ps.month_of(ps.parse_raw_relpath(k)[0]) in months_filter}
    by_month = {}
    for rel, (sz, _) in raw.items():
        m = ps.month_of(ps.parse_raw_relpath(rel)[0])
        by_month.setdefault(m, [0, 0])
        by_month[m][0] += 1
        by_month[m][1] += sz
    log(f"  raw files: {len(raw):,} ({sum(v[0] for v in raw.values()) / 1e9:.1f} GB) scanned in {time.time() - t0:.0f}s")
    for m, (n, sz) in sorted(by_month.items()):
        log(f"    {m}: {n:4d} files  {sz / 1e9:6.2f} GB")

    man_path = out / "manifest.jsonl"
    comp_path = out / "compacted.json"
    manifest = ps.read_manifest(man_path)
    compacted = json.loads(comp_path.read_text(encoding="utf-8")) if comp_path.exists() else {}
    if a.retry_skipped:
        retry = [k for k, v in manifest.items() if v.get("status") in ps.SKIP_STATUSES and k in raw]
        for k in retry:          # their month must be rebuilt once they are staged
            compacted.pop(ps.month_of(ps.parse_raw_relpath(k)[0]), None)
            del manifest[k]
        log(f"  --retry-skipped: {len(retry)} file(s) will be re-staged")
    stage, months_todo = ps.plan_work(raw, manifest, compacted,
                                      lambda rel: staged_dir(out, rel).exists())
    log(f"  to stage: {len(stage):,} file(s); months to compact: {months_todo or 'none'}")

    # ---------------- Stage 1 ------------------------------------------------
    if stage:
        if free_gb() < a.min_free_gb:
            log(f"  STOP: only {free_gb():.1f} GB free on {out} (< --min-free-gb {a.min_free_gb})")
            return 3
        tasks = [(str(base), rel, raw[rel][0], raw[rel][1], str(out)) for rel in stage]
        tot_bytes = sum(raw[r][0] for r in stage)
        t1 = time.time()
        last = 0.0
        done_bytes = 0
        counts = {}
        man_f = open(man_path, "a", encoding="utf-8")
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for i, rec in enumerate(ex.map(stage_one, tasks, chunksize=2), start=1):
                man_f.write(json.dumps(rec, default=str) + "\n")
                man_f.flush()
                manifest[rec["file"]] = rec
                counts[rec["status"]] = counts.get(rec["status"], 0) + 1
                done_bytes += rec["size"]
                if rec["status"] in ps.SKIP_STATUSES:
                    log(f"  !! {rec['file']}: {rec['status']} "
                        f"{rec.get('error') or rec.get('missing') or ''} "
                        f"outside_max_s={rec.get('outside_max_s')}")
                el = time.time() - t1
                if el - last >= 5 or i == len(tasks):
                    last = el
                    rate = done_bytes / el if el > 0 else 0
                    eta = (tot_bytes - done_bytes) / rate if rate > 0 else float("nan")
                    log(f"  [stage1] {i}/{len(tasks)} files  {done_bytes / 1e9:.1f}/{tot_bytes / 1e9:.1f} GB  "
                        f"{rate / 1e6:.0f} MB/s  elapsed={el:.0f}s ETA~{eta:.0f}s  {counts}  free={free_gb():.0f}GB")
                    if free_gb() < a.min_free_gb:
                        log(f"  STOP: free space below {a.min_free_gb} GB -- rerun after freeing space (resumes)")
                        ex.shutdown(cancel_futures=True)
                        return 3
        man_f.close()

    # ---------------- Stage 2 ------------------------------------------------
    if a.stage1_only:
        log("  --stage1-only: skipping compaction")
    else:
        from tape.swaps_cache import open_cache_connection
        con = open_cache_connection(memory_limit=a.memory_limit, threads=a.threads,
                                    temp_dir=str(out / "_duckdb_tmp"))
        # NOTE: preserve_insertion_order is left at its default (true) on purpose --
        # the bucket files must come out in ORDER BY order (row-group pruning).
        for m in months_todo:
            rels = [r for r in raw if ps.month_of(ps.parse_raw_relpath(r)[0]) == m]
            not_current = [r for r in rels if r not in manifest
                           or manifest[r].get("size") != raw[r][0] or manifest[r].get("mtime_ns") != raw[r][1]]
            if not_current:
                log(f"  [compact {m}] skipped: {len(not_current)} file(s) not staged yet")
                continue
            if not any(manifest[r]["status"] in ps.DONE_STATUSES for r in rels):
                log(f"  [compact {m}] skipped: no usable file (all skipped/errored)")
                continue
            if free_gb() < a.min_free_gb:
                log(f"  STOP before compacting {m}: only {free_gb():.1f} GB free")
                return 3
            log(f"  [compact {m}] {len(rels)} file(s)")
            compact_month(con, out, m, rels, manifest, log, a.keep_staging)
            compacted[m] = {"fingerprint": ps.month_fingerprint({r: raw[r] for r in rels}),
                            "n_files": len(rels), "built_utc": datetime.now(timezone.utc).isoformat(),
                            "rows": sum(int(manifest[r].get("n_swaps", 0)) for r in rels
                                        if manifest[r]["status"] in ps.DONE_STATUSES)}
            tmp = comp_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(compacted, indent=1), encoding="utf-8")
            os.replace(tmp, comp_path)
        con.close()

    # ---------------- Summary -------------------------------------------------
    import pandas as pd
    cov = ps.load_coverage(out)
    if len(cov):
        cov.to_parquet(out / "coverage.parquet", index=False)
        cov["month"] = cov["date"].str[:7]
        if "n_swaps" not in cov.columns:
            cov["n_swaps"] = 0
        cov["n_swaps"] = pd.to_numeric(cov["n_swaps"], errors="coerce").fillna(0)
        g = cov.groupby("month").agg(files=("hour", "size"),
                                     ok=("status", lambda s: int((s == "ok").sum())),
                                     noise=("status", lambda s: int((s == "boundary_noise").sum())),
                                     skipped=("status", lambda s: int(s.isin(ps.SKIP_STATUSES).sum())),
                                     swaps=("n_swaps", "sum"), days=("date", "nunique"))
        log("\n  per month: files ok noise skipped swaps days")
        for m, r in g.iterrows():
            log(f"    {m}: {int(r.files):4d} {int(r.ok):4d} {int(r.noise):4d} {int(r.skipped):3d} "
                f"{int(r.swaps):>12,} {int(r.days):3d}  {'COMPACTED' if m in compacted else ''}")
    log(f"  store size: months={_dir_gb(out / 'months'):.1f} GB staging={_dir_gb(out / 'staging'):.1f} GB  "
        f"free={free_gb():.1f} GB")
    log(f"  done {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
