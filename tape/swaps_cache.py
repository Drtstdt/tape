"""D118 (docs/DECISIONS.md) -- a DERIVED, mint-clustered copy of data/swaps,
stored as N_BUCKETS parquet files `bucket=NN.parquet` (bucket = crc32(mint) %
N_BUCKETS), each sorted by (mint, ts_ms, slot, sig). Built bucket by bucket so
no step ever sorts more than ~1/64 of the corpus (the first single-sort
attempt died with `Allocation failure` on the user's machine).

Why: real evidence from a 25.8%-complete info_audit_v2 run (5 workers, chunks
of 25 mints): summed worker cpu-time query=15842s vs compute=188s -- 99% of the
time was DuckDB fetching swaps. data/swaps is partitioned by venue/dt and NOT
sorted by mint, so every `WHERE mint = ...` has to read the `mint` column of
the whole corpus (710+ files); chunking only amortises that. A copy sorted by
(mint, ts_ms, slot, sig) with small row groups lets DuckDB prune row groups by
min/max, so one mint costs milliseconds.

Safety:
  * data/swaps is only READ. The cache is written elsewhere (E:), to a .tmp
    file, verified, then renamed into place.
  * the cache holds RAW rows (no dedup). fetch_swaps() applies the SAME dedup
    window as Store's `swaps` view, restricted to one mint -- exact, because
    `mint` is in the window's PARTITION BY.
  * a sidecar .meta.json records a fingerprint of data/swaps (file count,
    bytes, newest mtime). Readers refuse a cache whose fingerprint no longer
    matches, so a grown corpus can never be silently half-covered.

duckdb is imported lazily so the pure helpers are importable/testable without
it.
"""

from __future__ import annotations

import dataclasses
import json
import random
import shutil
import time
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .schema import CanonicalSwap

_DEDUP_WINDOW = ("row_number() OVER (PARTITION BY sig, mint, side, "
                 "round(base_amount, 12) ORDER BY source)")


def _q(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def source_fingerprint(data_dir) -> Dict[str, int]:
    n = total = mx = 0
    for p in (Path(data_dir) / "swaps").rglob("*.parquet"):
        st = p.stat()
        n += 1
        total += st.st_size
        mx = max(mx, st.st_mtime_ns)
    return {"files": n, "bytes": total, "max_mtime_ns": mx}


N_BUCKETS = 64


def bucket_of(mint: str) -> int:
    """Deterministic (crc32, not DuckDB hash(): stable across versions)."""
    return zlib.crc32(str(mint).encode("utf-8")) % N_BUCKETS


def bucket_file(cache_dir, bucket: int) -> Path:
    return Path(cache_dir) / f"bucket={bucket:02d}.parquet"


def meta_path(cache_dir) -> Path:
    return Path(cache_dir) / "meta.json"


def check_cache_fresh(cache_dir, data_dir) -> None:
    """Raise if the cache is missing or no longer matches data/swaps."""
    cache_dir = Path(cache_dir)
    mp = meta_path(cache_dir)
    if not cache_dir.is_dir() or not mp.exists():
        raise FileNotFoundError(f"swaps cache not found/incomplete: {cache_dir} "
                                f"(build it with scripts/build_swaps_cache.py)")
    meta = json.loads(mp.read_text(encoding="utf-8"))
    if meta.get("n_buckets") != N_BUCKETS:
        raise RuntimeError(f"swaps cache has n_buckets={meta.get('n_buckets')}, "
                           f"code expects {N_BUCKETS}: rebuild it")
    missing = [b for b in range(N_BUCKETS) if not bucket_file(cache_dir, b).exists()]
    if missing:
        raise FileNotFoundError(f"swaps cache is missing bucket file(s): {missing[:5]}...")
    built = meta.get("source_fingerprint")
    now = source_fingerprint(data_dir)
    if built != now:
        raise RuntimeError(
            f"swaps cache is STALE: built from {built}, data/swaps is now {now}. "
            f"Rebuild with scripts/build_swaps_cache.py (or pass --allow-stale-cache "
            f"to deliberately use a snapshot; it will not contain newer swaps).")


def commit_headroom():
    """(available_commit_gb, commit_limit_gb) on Windows, else None.

    D118 evidence: three `Allocation failure` crashes while 15.6GB of
    PHYSICAL RAM was free -- Windows' COMMIT limit (RAM + pagefile, here
    35.6GB with only 5.3GB left, pagefile 2GB) was the binding constraint."""
    import sys
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return None
        return ms.ullAvailPageFile / 1e9, ms.ullTotalPageFile / 1e9
    except Exception:  # noqa: BLE001
        return None


def parse_gb(text: str) -> float:
    t = str(text).strip().upper().replace(" ", "")
    for suf, mul in (("GIB", 1.0), ("GB", 1.0), ("MIB", 1 / 1024), ("MB", 1 / 1000), ("G", 1.0)):
        if t.endswith(suf):
            return float(t[: -len(suf)]) * mul
    return float(t)


def commit_preflight(need_gb: float, what: str, log=print) -> None:
    """Print commit headroom and warn LOUDLY if the planned memory use can
    exceed it. Warn only -- never aborts."""
    h = commit_headroom()
    if h is None:
        return
    avail, limit = h
    log(f"[memory] Windows commit: {avail:.1f} GB available of {limit:.1f} GB limit "
        f"(RAM + pagefile); {what} may need ~{need_gb:.1f} GB")
    if avail < need_gb:
        log(f"[memory] WARNING: planned use ({need_gb:.1f} GB) > available commit ({avail:.1f} GB). "
            f"Expect 'Out of Memory Error: Allocation failure' even with free RAM. "
            f"Fix: enlarge the pagefile (System Properties > Advanced > Performance > "
            f"Advanced > Virtual memory), close memory-heavy programs, or lower --workers / "
            f"--worker-memory / --memory-limit.")


def rows_to_swaps(df) -> List[CanonicalSwap]:
    cols = {f.name for f in dataclasses.fields(CanonicalSwap)}
    return [CanonicalSwap(**{k: v for k, v in row.items() if k in cols})
            for row in df.to_dict("records")]


def open_cache_connection(memory_limit: str = "2GB", threads: int = 1,
                          temp_dir: Optional[str] = None):
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute(f"SET threads = {int(threads)}")
    con.execute(f"SET memory_limit = '{memory_limit}'")
    con.execute("SET enable_progress_bar = false")
    if temp_dir:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = {_q(Path(temp_dir).as_posix())}")
    for setting in ("parquet_metadata_cache", "enable_object_cache"):
        try:
            con.execute(f"SET {setting} = true")
        except Exception:  # noqa: BLE001 -- name differs across DuckDB versions
            pass
    return con


def fetch_swaps(con, cache_dir, mints: Sequence[str]) -> Dict[str, List[CanonicalSwap]]:
    """Same result as list(Store.iter_swaps(mint)) for each mint (verified by
    build_swaps_cache's own check on a random sample)."""
    out: Dict[str, List[CanonicalSwap]] = {}
    for m in mints:
        path = _q(bucket_file(cache_dir, bucket_of(m)).as_posix())
        df = con.execute(
            f"SELECT * EXCLUDE (rn) FROM ("
            f"SELECT *, {_DEDUP_WINDOW} AS rn FROM read_parquet({path}) "
            f"WHERE mint = {_q(m)}) WHERE rn = 1 ORDER BY ts_ms, slot, sig").df()
        out[m] = rows_to_swaps(df)
    return out


def build_swaps_cache(data_dir, out_dir, *, temp_dir, memory_limit="4GB", threads=2,
                      row_group_size=25000, verify_sample=300, seed=0,
                      accept_diffs=False, log=print) -> Dict:
    import duckdb
    import pandas as pd

    data_dir, out_dir = Path(data_dir), Path(out_dir)
    swaps_dir = data_dir / "swaps"
    if not swaps_dir.exists():
        raise FileNotFoundError(swaps_dir)
    build_dir = out_dir.with_name(out_dir.name + ".building")
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    commit_preflight(parse_gb(memory_limit) + 3.0, "the cache build", log)

    fp = source_fingerprint(data_dir)
    log(f"[cache] source: {fp['files']} parquet file(s), {fp['bytes'] / 1e9:.2f} GB (read-only)")
    log(f"[cache] output: {out_dir} ({N_BUCKETS} bucket files, built in {build_dir.name})")

    con = duckdb.connect(":memory:")
    con.execute(f"SET threads = {int(threads)}")
    con.execute(f"SET memory_limit = '{memory_limit}'")
    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = {_q(Path(temp_dir).as_posix())}")
    con.execute("SET max_temp_directory_size = '60GB'")
    con.execute("SET preserve_insertion_order = false")

    src = (f"read_parquet({_q((swaps_dir / '**' / '*.parquet').as_posix())}, "
           f"hive_partitioning=1, union_by_name=1)")

    t0 = time.time()
    n_src, n_null = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE mint IS NULL) FROM {src}").fetchone()
    log(f"[cache] source rows: {n_src:,} (null-mint rows: {n_null:,}, never fetchable by mint) "
        f"counted in {time.time() - t0:.0f}s")

    t0 = time.time()
    mints = [r[0] for r in con.execute(
        f"SELECT DISTINCT mint FROM {src} WHERE mint IS NOT NULL").fetchall()]
    log(f"[cache] distinct mints: {len(mints):,} ({time.time() - t0:.0f}s)")
    mb = pd.DataFrame({"mint": mints, "bucket": [bucket_of(m) for m in mints]})
    con.register("mb_df", mb)
    con.execute("CREATE TEMP TABLE mb AS SELECT * FROM mb_df")

    t_all = time.time()
    n_written = 0
    for b in range(N_BUCKETS):
        t1 = time.time()
        target = bucket_file(build_dir, b)
        con.execute(
            f"COPY (SELECT s.* FROM {src} s JOIN mb ON s.mint = mb.mint WHERE mb.bucket = {b} "
            f"ORDER BY s.mint, s.ts_ms, s.slot, s.sig) TO {_q(target.as_posix())} "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {int(row_group_size)})")
        n_written += 1
        el = time.time() - t_all
        eta = el / n_written * (N_BUCKETS - n_written)
        log(f"[cache] bucket {b + 1}/{N_BUCKETS} done in {time.time() - t1:.1f}s  "
            f"elapsed={el:.0f}s ETA~{eta:.0f}s")

    cache_sql = f"read_parquet({_q((build_dir / 'bucket=*.parquet').as_posix())})"

    # -- verification (never skipped) ------------------------------------
    n_cache = con.execute(f"SELECT count(*) FROM {cache_sql}").fetchone()[0]
    ok = n_cache == n_src - n_null
    log(f"[cache] verify 1/3 row count: source(non-null mint)={n_src - n_null:,} "
        f"cache={n_cache:,} -> {'OK' if ok else 'MISMATCH'}")

    sample = random.Random(seed).sample(mints, min(verify_sample, len(mints)))
    in_list = ", ".join(_q(m) for m in sample)

    def dedup(s_):
        return (f"SELECT * EXCLUDE (rn) FROM (SELECT *, {_DEDUP_WINDOW} AS rn FROM {s_} "
                f"WHERE mint IN ({in_list})) WHERE rn = 1")

    t0 = time.time()
    a_minus_b = con.execute(
        f"SELECT count(*) FROM (({dedup(src)}) EXCEPT ALL ({dedup(cache_sql)}))").fetchone()[0]
    b_minus_a = con.execute(
        f"SELECT count(*) FROM (({dedup(cache_sql)}) EXCEPT ALL ({dedup(src)}))").fetchone()[0]
    ok_sample = a_minus_b == 0 and b_minus_a == 0
    log(f"[cache] verify 2/3 deduped rows of {len(sample)} random mints, source vs cache: "
        f"only-in-source={a_minus_b} only-in-cache={b_minus_a} ({time.time() - t0:.0f}s) "
        f"-> {'OK' if ok_sample else 'DIFFERENT'}")

    # exactly what workers do: fetch_swaps(), compared with the source-side dedup per mint
    fcon = open_cache_connection()
    t0 = time.time()
    got = fetch_swaps(fcon, build_dir, sample[:20])
    per_mint = (time.time() - t0) / max(len(sample[:20]), 1)
    log(f"[cache] verify 3/3 worker-path fetch_swaps(): {per_mint * 1000:.0f} ms/mint "
        f"({sum(len(v) for v in got.values())} swaps for 20 mints) "
        f"-> {'OK' if per_mint < 2.0 else 'SLOW'}")

    if not ok or (not ok_sample and not accept_diffs):
        raise RuntimeError(
            "cache verification FAILED -- NOT installed "
            f"(left in {build_dir}). Source data is untouched.")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    build_dir.replace(out_dir)
    meta = {"source_fingerprint": fp, "rows": n_cache, "mints": len(mints),
            "n_buckets": N_BUCKETS, "row_group_size": row_group_size,
            "built_at": time.time(), "verify_sample": len(sample),
            "verify_diffs": [a_minus_b, b_minus_a], "null_mint_rows": n_null}
    meta_path(out_dir).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    log(f"[cache] DONE -> {out_dir} ({time.time() - t_all:.0f}s)")
    return meta
