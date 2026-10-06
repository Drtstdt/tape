"""D120 (docs/DECISIONS.md) -- pumpfundata store v2 on E:.

data/swaps holds only 2026-02-08..03-03 (D119 addendum); F:\\pumpfundata holds
~8 months of raw hourly files. This store is built straight from the raw
files, never touching data/swaps or F: (both read-only):

    <root>/manifest.jsonl                one JSON line per processed raw file
                                         (status, counts, timestamp stats) --
                                         the COVERAGE record: which UTC hours
                                         were collected comes from here, never
                                         from a formula
    <root>/staging/month=YYYY-MM/f=YYYY-MM-DD_HH/grp=G.parquet   (stage 1,
                                         8 bucket groups per raw file, deleted
                                         after the month is compacted)
    <root>/staging/month=YYYY-MM/f=.../events.parquet
    <root>/months/month=YYYY-MM/bucket=NN.parquet  sorted (mint, ts_ms, slot,
                                         sig), small row groups -> a mint fetch
                                         prunes row groups (same idea as D118)
    <root>/months/month=YYYY-MM/events.parquet     create / bonding_complete
    <root>/months/month=YYYY-MM/mint_index.parquet mint, bucket, first/last ts, n
    <root>/compacted.json                month -> fingerprint of its raw files

Conversion is a VECTORISED copy of tape/sources/pumpfundata.py::_row_to_swap
(same filters, same unit scaling, same field mapping), tested row-for-row
against it (tests/test_pf_store.py). Extra columns kept because they are
needed later and cost little: fee_sol (vendor fee_lamports -> lets the cost
model use the MEASURED fee), real reserves, is_mayhem_mode. Rows are stored
RAW (no dedup); readers apply the same dedup window as the Store `swaps` view.

pandas/pyarrow/duckdb are imported lazily so the pure helpers are importable
in a sandbox without them.
"""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .sources.pumpfundata import (
    PUMPFUN_PROGRAM, TOKEN_DECIMALS, LAMPORTS_PER_SOL,
    EXPECTED_CREATE_TOTAL_SUPPLY_RAW, REQUIRED_COLUMNS,
)

WSOL = "So11111111111111111111111111111111111111112"
SOURCE = "pumpfundata"
N_BUCKETS = 64
N_GROUPS = 8                       # stage-1 output: bucket // 8
HOUR_MS = 3_600_000
# A wrong epoch-unit guess is off by a factor of 1000 (decades); vendor
# boundary noise seen so far is seconds to ~2 minutes (D98-D109). Anything
# beyond one hour is treated as a parsing problem and the file is EXCLUDED
# (recorded in the manifest, never silently dropped).
SUSPECT_OFFSET_S = 3600

SWAP_COLUMNS = ["mint", "venue", "pool", "ts_ms", "slot", "sig", "side",
                "base_amount", "quote_amount", "quote_mint", "price", "wallet",
                "base_reserve_after", "quote_reserve_after", "source",
                "fee_sol", "real_base_reserve_after", "real_quote_reserve_after",
                "is_mayhem_mode", "bucket", "src"]
EVENT_COLUMNS = ["event_type", "mint", "ts_ms", "slot", "sig", "creator",
                 "token_total_supply", "is_mayhem_mode", "can_be_frozen", "src"]
DONE_STATUSES = ("ok", "boundary_noise")          # staged and usable
SKIP_STATUSES = ("ts_suspect", "schema_mismatch", "read_error")   # recorded, excluded

# D133: the key includes the real reserve after the swap. Two DISTINCT swaps of
# one tx with identical token amounts (same sig/mint/side/base_amount) differ in
# real_quote_reserve_after and must both survive; a true duplicate (the same row
# in two hourly files, D99) has the same reserve and still collapses. The order
# makes the surviving copy deterministic (before D133 it was arbitrary); the
# earliest timestamp wins, so a query with a ts cut-off keeps the same copy as
# one without whenever any copy lies inside the cut.
_DEDUP_WINDOW = ("row_number() OVER (PARTITION BY sig, mint, side, round(base_amount, 12), "
                 "round(real_quote_reserve_after, 9) ORDER BY ts_ms, slot, source, src)")
_FETCH_ORDER = "mint, ts_ms, slot, sig, side, base_amount, real_quote_reserve_after"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def bucket_of(mint: str) -> int:
    """Same function as tape/swaps_cache.bucket_of (crc32 % 64)."""
    return zlib.crc32(str(mint).encode("utf-8")) % N_BUCKETS


def parse_raw_relpath(rel: str) -> Optional[Tuple[str, int]]:
    """'date=2026-02-12/hour=07.parquet' (any separator) -> ('2026-02-12', 7)."""
    m_d = re.search(r"date=(\d{4}-\d{2}-\d{2})", rel)
    m_h = re.search(r"hour=(\d{2})\.parquet$", rel.replace("\\", "/"))
    if not m_d or not m_h:
        return None
    return m_d.group(1), int(m_h.group(1))


def file_tag(date: str, hour: int) -> str:
    return f"{date}_{hour:02d}"


def month_of(date: str) -> str:
    return date[:7]


def hour_window_ms(date: str, hour: int) -> Tuple[int, int]:
    from datetime import datetime, timezone
    y, mo, d = (int(x) for x in date.split("-"))
    lo = int(datetime(y, mo, d, hour, tzinfo=timezone.utc).timestamp() * 1000)
    return lo, lo + HOUR_MS


def ts_to_ms(series):
    """Vectorised `_parse_timestamp_ms`. Returns a float64 numpy array (NaN =
    unparsable -> row dropped, exactly where the scalar version returns None).

    datetime64 (naive = UTC, like pandas Timestamp.timestamp()) -> exact
    integer milliseconds (floor). The scalar version computes
    int(ts.timestamp() * 1000) through a float; for whole-millisecond values
    (the vendor's are whole SECONDS, D109's `ts_ms=1786521478000`) both are
    identical -- asserted in the tests.
    numeric -> same magnitude rule and truncation as the scalar version."""
    import pandas as pd
    s = series
    if s.dtype == object:
        first = next((x for x in s if x is not None), None)
        if first is not None and hasattr(first, "timestamp") and not isinstance(first, (int, float)):
            s = pd.to_datetime(s, utc=True, errors="coerce")
    if pd.api.types.is_datetime64_any_dtype(s):
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_convert("UTC").dt.tz_localize(None)
        ns = s.astype("datetime64[ns]").astype("int64").to_numpy()
        out = np.floor_divide(ns, 1_000_000).astype("float64")
        out[s.isna().to_numpy()] = np.nan
        return out
    v = pd.to_numeric(s, errors="coerce").to_numpy(dtype="float64")
    out = np.full(len(v), np.nan)
    pos = v > 0
    sec = pos & (v < 1e11)
    ms = pos & (v >= 1e11) & (v < 1e14)
    us = pos & (v >= 1e14) & (v < 1e17)
    ns_ = pos & (v >= 1e17)
    out[sec] = np.trunc(v[sec] * 1000)
    out[ms] = np.trunc(v[ms])
    out[us] = np.trunc(v[us] / 1000)
    out[ns_] = np.trunc(v[ns_] / 1_000_000)
    return out


def _truthy_str(s):
    """Python truthiness of each value as `row.get(x)` would see it: None and
    '' are falsy; NaN (float) is truthy -- reproduced on purpose for parity."""
    import pandas as pd
    return s.map(lambda x: bool(x) if not (isinstance(x, float) and np.isnan(x)) else True) \
        if s.dtype == object else s.notna() & (s.astype(str) != "")


def _col(df, name):
    import pandas as pd
    return df[name] if name in df.columns else pd.Series([None] * len(df), index=df.index, dtype=object)


def convert_raw_frame(df, src: str, hour_lo_ms: Optional[int] = None):
    """Raw pumpfundata frame -> (swaps_df, events_df, stats). Raises nothing
    for data problems; schema problems are the caller's (REQUIRED_COLUMNS)."""
    import pandas as pd
    stats = {"n_rows": int(len(df))}
    et = df["event_type"]
    stats["n_swap_rows"] = int((et == "swap").sum())
    stats["n_create"] = int((et == "create").sum())
    stats["n_bonding_complete"] = int((et == "bonding_complete").sum())
    stats["n_other_event"] = int(len(df) - stats["n_swap_rows"] - stats["n_create"]
                                 - stats["n_bonding_complete"])

    # ---- swaps: the exact filter chain of _row_to_swap -----------------
    sw = df[et == "swap"]
    ts = ts_to_ms(sw["timestamp"])
    action = sw["action"]
    keep = (_truthy_str(sw["token_mint"]).to_numpy() & _truthy_str(sw["signature"]).to_numpy()
            & action.isin(["buy", "sell"]).to_numpy() & ~np.isnan(ts)
            & sw["slot_number"].notna().to_numpy()
            & _col(sw, "token_amount").map(lambda x: x is not None).to_numpy()
            & _col(sw, "lamports_amount").map(lambda x: x is not None).to_numpy())
    sw = sw[keep]
    ts = ts[keep]
    action = sw["action"]
    stats["n_swaps_dropped"] = int((~keep).sum())

    base = np.abs(pd.to_numeric(sw["token_amount"], errors="coerce").to_numpy("float64")) / (10 ** TOKEN_DECIMALS)
    quote = np.abs(pd.to_numeric(sw["lamports_amount"], errors="coerce").to_numpy("float64")) / LAMPORTS_PER_SOL
    with np.errstate(divide="ignore", invalid="ignore"):
        # `quote / base if base else 0.0`: base==0 -> 0.0; NaN base is truthy -> NaN
        price = np.where(base == 0, 0.0, quote / np.where(base == 0, 1.0, base))

    def num(name, scale):
        c = _col(sw, name)
        return pd.to_numeric(c, errors="coerce").to_numpy("float64") / scale

    creator = _col(sw, "token_creator")
    pool = creator.map(lambda x: str(x) if (x is not None and not (isinstance(x, str) and x == "")
                                            and not (isinstance(x, float) and np.isnan(x))) else
                       ("nan" if isinstance(x, float) and np.isnan(x) else "unknown"))
    wallet = _col(sw, "user_wallet").map(lambda x: None if (x is None or x == "") else x)
    mints = sw["token_mint"].astype(str)
    out = pd.DataFrame({
        "mint": mints.to_numpy(),
        "venue": PUMPFUN_PROGRAM,
        "pool": pool.to_numpy(),
        "ts_ms": ts.astype("int64"),
        "slot": pd.to_numeric(sw["slot_number"]).astype("int64").to_numpy(),
        "sig": sw["signature"].astype(str).to_numpy(),
        "side": np.where(action.to_numpy() == "buy", "buy", "sell"),
        "base_amount": base,
        "quote_amount": quote,
        "quote_mint": WSOL,
        "price": price,
        "wallet": wallet.to_numpy(),
        "base_reserve_after": num("virtual_token_reserve", 10 ** TOKEN_DECIMALS),
        "quote_reserve_after": num("virtual_lamports_reserve", LAMPORTS_PER_SOL),
        "source": SOURCE,
        "fee_sol": num("fee_lamports", LAMPORTS_PER_SOL),
        "real_base_reserve_after": num("real_token_reserve", 10 ** TOKEN_DECIMALS),
        "real_quote_reserve_after": num("real_lamports_reserve", LAMPORTS_PER_SOL),
        "is_mayhem_mode": _col(sw, "is_mayhem_mode").map(
            lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else bool(x)).to_numpy(),
    })
    uniq = pd.unique(out["mint"])
    bmap = {m: bucket_of(m) for m in uniq}
    out["bucket"] = out["mint"].map(bmap).astype("int16")
    out["src"] = src
    stats["n_swaps"] = int(len(out))
    stats["n_mints"] = int(len(uniq))
    stats["n_nan_amount"] = int(np.isnan(base).sum() + np.isnan(quote).sum())
    stats["n_wallet_null"] = int(out["wallet"].isna().sum())
    fee_ratio = out["fee_sol"] / out["quote_amount"].where(out["quote_amount"] > 0)
    fr = fee_ratio.dropna()
    stats["fee_ratio_p50"] = float(fr.median()) if len(fr) else None
    stats["fee_ratio_p05"] = float(fr.quantile(0.05)) if len(fr) else None
    stats["fee_ratio_p95"] = float(fr.quantile(0.95)) if len(fr) else None
    stats["n_fee_null"] = int(out["fee_sol"].isna().sum())

    # ---- timestamp vs the file's own hour (D98-D109, recorded not raised)
    stats["ts_min"] = int(out["ts_ms"].min()) if len(out) else None
    stats["ts_max"] = int(out["ts_ms"].max()) if len(out) else None
    if hour_lo_ms is not None and len(out):
        t = out["ts_ms"].to_numpy()
        hi = hour_lo_ms + HOUR_MS
        off = np.maximum(np.maximum(hour_lo_ms - t, t - (hi - 1)), 0) / 1000.0
        bad = off[off > 0]
        stats["n_outside_hour"] = int(len(bad))
        stats["outside_p50_s"] = float(np.median(bad)) if len(bad) else 0.0
        stats["outside_p90_s"] = float(np.quantile(bad, 0.9)) if len(bad) else 0.0
        stats["outside_max_s"] = float(bad.max()) if len(bad) else 0.0
    else:
        stats["n_outside_hour"] = 0
        stats["outside_max_s"] = 0.0

    # ---- events -----------------------------------------------------------
    ev = df[et.isin(["create", "bonding_complete"])]
    ev_ts = ts_to_ms(ev["timestamp"])
    events = pd.DataFrame({
        "event_type": ev["event_type"].astype(str).to_numpy(),
        "mint": ev["token_mint"].astype(str).to_numpy(),
        "ts_ms": ev_ts,                              # float: NaN if unparsable
        "slot": pd.to_numeric(_col(ev, "slot_number"), errors="coerce").to_numpy("float64"),
        "sig": _col(ev, "signature").astype(str).to_numpy(),
        "creator": _col(ev, "token_creator").to_numpy(),
        "token_total_supply": pd.to_numeric(_col(ev, "token_total_supply"), errors="coerce").to_numpy("float64"),
        "is_mayhem_mode": _col(ev, "is_mayhem_mode").to_numpy(),
        "can_be_frozen": _col(ev, "can_be_frozen").to_numpy(),
    })
    events["src"] = src
    cr = events[events["event_type"] == "create"]["token_total_supply"].dropna()
    stats["n_bad_supply"] = int((cr != float(EXPECTED_CREATE_TOTAL_SUPPLY_RAW)).sum())
    return out, events, stats


def classify_file(stats: dict) -> str:
    if stats.get("outside_max_s", 0.0) > SUSPECT_OFFSET_S:
        return "ts_suspect"
    if stats.get("n_outside_hour", 0) > 0:
        return "boundary_noise"
    return "ok"


# ---------------------------------------------------------------------------
# Manifest / resume logic
# ---------------------------------------------------------------------------

def read_manifest(path: Path) -> Dict[str, dict]:
    """Last record per raw file wins."""
    out: Dict[str, dict] = {}
    if not Path(path).exists():
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue          # a torn last line after a crash: ignore it
            out[r["file"]] = r
    return out


def month_fingerprint(files: Dict[str, Tuple[int, int]]) -> str:
    """files: relpath -> (size, mtime_ns), for every raw file of a month."""
    import hashlib
    blob = json.dumps(sorted((k, list(v)) for k, v in files.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def plan_work(raw_files: Dict[str, Tuple[int, int]], manifest: Dict[str, dict],
              compacted: Dict[str, dict], staged_exists) -> Tuple[List[str], List[str]]:
    """Decide what to (re)stage and which months to (re)compact.

    raw_files: relpath -> (size, mtime_ns) for every raw file found.
    staged_exists(relpath) -> bool: stage-1 output present on disk.
    A file needs staging if it has no current manifest record, or if its
    month needs (re)compaction and its staged output is gone (staging is
    deleted after compaction). A month needs compaction if it was never
    compacted or its raw-file fingerprint changed (e.g. new files appeared)."""
    by_month: Dict[str, Dict[str, Tuple[int, int]]] = {}
    for rel, meta in raw_files.items():
        p = parse_raw_relpath(rel)
        if p is None:
            continue
        by_month.setdefault(month_of(p[0]), {})[rel] = meta
    months_todo = sorted(m for m, fs in by_month.items()
                         if compacted.get(m, {}).get("fingerprint") != month_fingerprint(fs))
    todo_set = set(months_todo)
    stage = []
    for rel, (size, mt) in sorted(raw_files.items()):
        p = parse_raw_relpath(rel)
        if p is None:
            continue
        rec = manifest.get(rel)
        current = rec is not None and rec.get("size") == size and rec.get("mtime_ns") == mt
        if not current:
            stage.append(rel)
        elif (month_of(p[0]) in todo_set and rec.get("status") in DONE_STATUSES
              and not staged_exists(rel)):
            stage.append(rel)
    return stage, months_todo


# ---------------------------------------------------------------------------
# Readers (lazy duckdb)
# ---------------------------------------------------------------------------

def _q(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _installed(paths) -> List[Path]:
    """Only installed months: `month=YYYY-MM`, never a `month=YYYY-MM.building`
    directory that a concurrent build_pf_store run is still writing."""
    return sorted(p for p in paths if re.fullmatch(r"month=\d{4}-\d{2}", p.parent.name))


def bucket_files(root, bucket: int) -> List[Path]:
    return _installed(Path(root).glob(f"months/month=*/bucket={bucket:02d}.parquet"))


def open_connection(memory_limit: str = "2GB", threads: int = 1, temp_dir: Optional[str] = None):
    from .swaps_cache import open_cache_connection
    return open_cache_connection(memory_limit=memory_limit, threads=threads, temp_dir=temp_dir)


def fetch_frame(con, root, mints: Sequence[str], columns: str = "*"):
    """Deduped rows for the given mints (all months), ordered (mint, ts_ms,
    slot, sig). One query per bucket touched."""
    import pandas as pd
    by_b: Dict[int, List[str]] = {}
    for m in mints:
        by_b.setdefault(bucket_of(m), []).append(m)
    parts = []
    for b, ms in sorted(by_b.items()):
        files = bucket_files(root, b)
        if not files:
            continue
        flist = "[" + ", ".join(_q(f.as_posix()) for f in files) + "]"
        inl = ", ".join(_q(m) for m in ms)
        parts.append(con.execute(
            f"SELECT {columns} FROM (SELECT * EXCLUDE (rn) FROM (SELECT *, {_DEDUP_WINDOW} AS rn "
            f"FROM read_parquet({flist}, union_by_name=true) WHERE mint IN ({inl})) WHERE rn = 1) "
            f"ORDER BY {_FETCH_ORDER}").df())
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def fetch_swaps(con, root, mints: Sequence[str]):
    """{mint: [CanonicalSwap, ...]} -- same contract as swaps_cache.fetch_swaps."""
    from .swaps_cache import rows_to_swaps
    df = fetch_frame(con, root, mints)
    out = {m: [] for m in mints}
    if len(df) == 0:
        return out
    m_arr = df["mint"].to_numpy()
    cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
    for s, e in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(df)]])):
        out[m_arr[s]] = rows_to_swaps(df.iloc[s:e])
    return out


def load_mint_index(root):
    """mint -> bucket, first_ts, last_ts, n_raw over ALL compacted months."""
    import pandas as pd
    files = _installed(Path(root).glob("months/month=*/mint_index.parquet"))
    if not files:
        return pd.DataFrame(columns=["mint", "bucket", "first_ts", "last_ts", "n_raw"])
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return (df.groupby(["mint", "bucket"], as_index=False)
              .agg(first_ts=("first_ts", "min"), last_ts=("last_ts", "max"), n_raw=("n_raw", "sum")))


def load_events(root):
    import pandas as pd
    files = _installed(Path(root).glob("months/month=*/events.parquet"))
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else pd.DataFrame()


def load_coverage(root):
    """One row per raw file from the manifest (date, hour, status, counts)."""
    import pandas as pd
    man = read_manifest(Path(root) / "manifest.jsonl")
    rows = []
    for rel, r in man.items():
        p = parse_raw_relpath(rel)
        if p:
            rows.append({"date": p[0], "hour": p[1], **{k: v for k, v in r.items() if k != "file"}})
    return pd.DataFrame(rows)
