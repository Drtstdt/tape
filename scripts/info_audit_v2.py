#!/usr/bin/env python3

"""
Stage 1 -- the information audit. Run this BEFORE fitting anything.

One question, answered without training a model: did we discover information
that was genuinely available before the future price move, and does that
information survive evaluation on tokens that played no role in selecting it?

For each feature, measure out-of-sample AUC against the MODEL-FREE excursion
label (did price reach +X% before -Y%).

"Out-of-sample" means three separate things that all have to hold at once:

1. TEMPORAL:
   evaluation tokens' tapes started strictly after every token used to pick
   or tune anything (screen + validation).

2. SELECTION:
   the feature/family reported at the end was named BEFORE the final test
   set was touched.

3. MULTIPLE-TESTING:
   dozens of features are tried, so some will clear any fixed AUC bar by
   chance alone. The bar is therefore built from THIS run's own feature count
   and token structure.

This script is a measurement, not a model.

IMPORTANT DATA SAFETY:
- data/swaps/**/*.parquet is READ ONLY.
- No source Parquet is modified, rewritten or recompressed.
- DuckDB spill is redirected to E:.
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import json
import os
import re
import sys
import time

from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.bars import BarBuilder, band_bar_threshold
from tape.cv import auc_cluster_bootstrap
from tape.features import TokenState
from tape.labels import BarrierConfig, label_series, thin_non_overlapping
from tape.schema import CanonicalSwap
from tape.store import Store
from tape.store_v2 import StoreV2
from tape.swaps_cache import (
    check_cache_fresh,
    commit_preflight,
    parse_gb,
    fetch_swaps,
    open_cache_connection,
)


# ---------------------------------------------------------------------------
# Corpus filters
# ---------------------------------------------------------------------------

MIN_SWAPS_PER_TOKEN = 50
MIN_BARS_PER_TOKEN = 20


# ---------------------------------------------------------------------------
# Hardware / DuckDB settings
# ---------------------------------------------------------------------------

# i5-9600KF = 6 physical cores / 6 threads.
# Four workers leaves some CPU/RAM headroom for Windows and Python.
DEFAULT_WORKERS = 5

# Each worker has its own DuckDB connection.
WORKER_THREADS = 1
WORKER_MEMORY_LIMIT = "2GB"   # D116: was 3GB; 2GB = the setting the sequential path ran with
# Mints fetched per DuckDB query inside a worker (D116). One query = one scan
# of the corpus, so this amortises the scan over CHUNK_MINTS tokens.
CHUNK_MINTS = 25

# D118: mint-clustered derived copy of data/swaps (scripts/build_swaps_cache.py).
# Used automatically when present; --no-swaps-cache forces the old path.
SWAPS_CACHE_DEFAULT = r"E:\tape_cache\swaps_by_mint"
WORKER_MAX_TEMP_SIZE = "10GB"

# Spill lives on E:, not beside the paid source data.
WORKER_TEMP_ROOT = r"E:\duckdb_tmp_workers"
UNIVERSE_TEMP_DIR = r"E:\duckdb_tmp_universe"

# Main-process universe scan.
UNIVERSE_MEMORY_LIMIT = "4GB"
UNIVERSE_MAX_TEMP_SIZE = "4GB"


# ---------------------------------------------------------------------------
# D118: compact feature storage
# ---------------------------------------------------------------------------
#
# The first 88k run died with OOM at 25.8% while the main process kept every
# row as a dict of ~53 Python floats (a few KB per row, ~1M+ rows at the end).
# Workers now return a float64 matrix (53 floats = 424 B/row, None -> NaN,
# the same conversion _f() always did) and the main process stacks matrices.

class FeatureMatrix:
    def __init__(self, names, data):
        self.names = list(names)
        self.data = data
        self._idx = {n: i for i, n in enumerate(self.names)}

    def __len__(self) -> int:
        return int(self.data.shape[0])

    def col(self, name: str) -> np.ndarray:
        return self.data[:, self._idx[name]]

    def subset(self, mask: np.ndarray) -> "FeatureMatrix":
        return FeatureMatrix(self.names, self.data[mask])

    @staticmethod
    def from_dicts(rows, names=None) -> "FeatureMatrix":
        names = list(names) if names is not None else sorted({k for r in rows for k in r})
        return FeatureMatrix(names, dicts_to_matrix(rows, names))

    @staticmethod
    def stack(parts, names) -> "FeatureMatrix":
        parts = [p for p in parts if p.shape[0]]
        data = np.vstack(parts) if parts else np.empty((0, len(names)), dtype=float)
        return FeatureMatrix(names, data)


def dicts_to_matrix(rows, names) -> np.ndarray:
    """rows: list of feature dicts -> (n, len(names)) float64, None/missing ->
    NaN. A key not in `names` raises (never silently dropped)."""
    if not rows:
        return np.empty((0, len(names)), dtype=float)
    known = set(names)
    for r in rows:
        extra = set(r) - known
        if extra:
            raise KeyError(f"feature(s) not in the fixed name list: {sorted(extra)}")
    return np.array([[_f(r.get(n)) for n in names] for r in rows], dtype=float)


def feature_names_fixed() -> List[str]:
    return sorted(TokenState("probe").features().keys())


def _mem_note() -> str:
    """Main-process RSS + system available RAM when psutil is installed
    (pip install psutil); empty otherwise."""
    try:
        import psutil
        return (f" [main rss={psutil.Process().memory_info().rss / 1e9:.1f}GB "
                f"sys_avail={psutil.virtual_memory().available / 1e9:.1f}GB]")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# AUC
# ---------------------------------------------------------------------------

def auc(scores: np.ndarray, y: np.ndarray) -> float:
    """
    Rank AUC, ties averaged.

    No sklearn dependency.

    Constant features correctly return 0.5 rather than 1.0.
    """

    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y, dtype=float)

    mask = ~np.isnan(scores)

    s = scores[mask]
    yy = y[mask]

    n_pos = int(yy.sum())
    n_neg = int(len(yy) - yy.sum())

    if n_pos == 0 or n_neg == 0:
        return float("nan")

    order = np.argsort(s, kind="mergesort")

    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)

    _, inv, counts = np.unique(
        s,
        return_inverse=True,
        return_counts=True,
    )

    sums = np.zeros(len(counts), dtype=float)

    np.add.at(
        sums,
        inv,
        ranks,
    )

    ranks = (sums / counts)[inv]

    return float(
        (
            ranks[yy == 1].sum()
            - n_pos * (n_pos + 1) / 2
        )
        / (n_pos * n_neg)
    )


def _f(v) -> float:
    return float("nan") if v is None else float(v)


def direction_of(a: float) -> str:
    if np.isnan(a):
        return "n/a"

    if a > 0.5:
        return "positive"

    if a < 0.5:
        return "inverse"

    return "none"


# ---------------------------------------------------------------------------
# Feature families
# ---------------------------------------------------------------------------

_WINDOW_SUFFIX = re.compile(r"\_(3|10|30)(?=\_|$)")


def family_of(name: str) -> str:
    fam = _WINDOW_SUFFIX.sub("", name)
    return fam if fam else name


# ---------------------------------------------------------------------------
# Legacy chronological_universe
#
# Kept for compatibility/documentation, but main() DOES NOT use this.
# main() uses StoreV2.chronological_universe(), which avoids the original
# giant whole-corpus GROUP BY execution path.
# ---------------------------------------------------------------------------

def chronological_universe(
    store: Store,
    since: Optional[str],
    until: Optional[str],
) -> List[Tuple[str, int, int]]:
    """
    Legacy implementation.

    Do not use this for the V2 audit path.
    """

    where = []

    if since:
        where.append(f"dt >= '{since}'")

    if until:
        where.append(f"dt <= '{until}'")

    clause = (
        "WHERE " + " AND ".join(where)
        if where
        else ""
    )

    print(
        "  [chronological_universe: LEGACY] "
        "running whole-corpus GROUP BY; "
        "V2 main path uses StoreV2 instead.",
        flush=True,
    )

    t0 = time.time()

    df = store.sql(
        f"""
        SELECT
            mint,
            min(ts_ms) AS first_ts_ms,
            count(*) AS n
        FROM swaps
        {clause}
        GROUP BY mint
        ORDER BY first_ts_ms, mint
        """
    )

    print(
        f"  [chronological_universe: LEGACY] "
        f"done in {time.time() - t0:.1f}s -- "
        f"{len(df)} mint(s)",
        flush=True,
    )

    return list(
        zip(
            df["mint"].tolist(),
            df["first_ts_ms"].tolist(),
            df["n"].tolist(),
        )
    )


# ---------------------------------------------------------------------------
# Real-creation-time cache
# ---------------------------------------------------------------------------

def load_real_creation_cache(
    path: Path,
) -> Dict[str, dict]:
    """
    Load real_creation_times.json.

    This is read-only.
    """

    size_mb = path.stat().st_size / 1_000_000

    print(
        f"  [load_real_creation_cache] "
        f"reading {path} ({size_mb:.0f} MB)...",
        flush=True,
    )

    t0 = time.time()

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as f:
        cache = json.load(f)

    print(
        f"  [load_real_creation_cache] "
        f"done in {time.time() - t0:.1f}s -- "
        f"{len(cache)} mint(s) in cache",
        flush=True,
    )

    return cache


def filter_by_real_creation(
    eligible: List[Tuple[str, int]],
    cache: Dict[str, dict],
    max_observed_age_hours: float,
) -> Tuple[List[Tuple[str, int]], int, int]:
    """
    Keep tokens that were genuinely young at first observation.
    """

    max_gap_ms = max_observed_age_hours * 3_600_000

    kept: List[Tuple[str, int]] = []

    excluded_too_old = 0
    excluded_no_data = 0

    for mint, ts in eligible:

        rec = cache.get(mint)

        if (
            not rec
            or rec.get("status") != "ok"
            or rec.get("real_created_ts_ms") is None
        ):
            excluded_no_data += 1
            continue

        if (
            ts - rec["real_created_ts_ms"]
        ) > max_gap_ms:
            excluded_too_old += 1
            continue

        kept.append((mint, ts))

    return (
        kept,
        excluded_too_old,
        excluded_no_data,
    )


# ---------------------------------------------------------------------------
# Build one token
# ---------------------------------------------------------------------------

def build_one(
    store: Store,
    mint: str,
    cfg: BarrierConfig,
    bar_fraction: float,
    stride: int,
    non_overlap: bool,
    timing: Optional[Dict[str, float]] = None,
    swaps: Optional[List[CanonicalSwap]] = None,
) -> Tuple[
    str,
    List[Dict[str, Optional[float]]],
    List[float],
]:
    """
    Build one token.

    swaps (D116): optional PRE-FETCHED, time-ordered swaps for this mint
    (from _swaps_for_mints). None = query store.iter_swaps(mint) as before.

    timing:
        query_s   = DuckDB query + CanonicalSwap construction
        compute_s = bars + features + labels
    """

    t0 = time.time()

    if swaps is None:
        swaps = list(
            store.iter_swaps(mint)
        )

    if timing is not None:
        timing["query_s"] = (
            timing.get("query_s", 0.0)
            + (time.time() - t0)
        )

    if len(swaps) < MIN_SWAPS_PER_TOKEN:
        return (
            "insufficient_swaps",
            [],
            [],
        )

    t1 = time.time()

    try:
        depth = (
            next(
                (
                    s.quote_reserve_after
                    for s in swaps
                    if s.quote_reserve_after
                ),
                None,
            )
            or 1.0
        )

        bb = BarBuilder(
            "dollar",
            threshold=band_bar_threshold(
                depth,
                bar_fraction,
            ),
        )

        st = TokenState(
            mint,
            created_ts_ms=swaps[0].ts_ms,
        )

        feats = []
        h = []
        l = []
        c = []
        t = []

        for s in swaps:

            bar = bb.push(s)

            if bar is None:
                continue

            # Important ordering:
            #
            # update(bar)
            #     ↓
            # features()
            #
            # Therefore features[i] only contain information available
            # when bar i closed.

            st.update(bar)

            feats.append(
                dict(st.features())
            )

            h.append(bar.high)
            l.append(bar.low)
            c.append(bar.close)
            t.append(bar.close_ts_ms)

        if len(c) < MIN_BARS_PER_TOKEN:
            return (
                "insufficient_bars",
                [],
                [],
            )

        labels = label_series(
            h,
            l,
            c,
            t,
            cfg,
            mint=mint,
            stride=stride,
        )

        labels = [
            lab
            for lab in labels
            if not lab.truncated
        ]

        if non_overlap:
            keep = set(
                thin_non_overlapping(labels)
            )

            labels = [
                lab
                for i, lab in enumerate(labels)
                if i in keep
            ]

        if not labels:
            return (
                "no_usable_labels",
                [],
                [],
            )

        rows = [
            feats[lab.bar_index]
            for lab in labels
        ]

        ys = [
            float(lab.y)
            for lab in labels
        ]

        return (
            "ok",
            rows,
            ys,
        )

    finally:

        if timing is not None:
            timing["compute_s"] = (
                timing.get("compute_s", 0.0)
                + (time.time() - t1)
            )


# ---------------------------------------------------------------------------
# Sequential build
# ---------------------------------------------------------------------------

def build(
    store: Store,
    mints: Sequence[str],
    cfg: BarrierConfig,
    bar_fraction: float,
    stride: int,
    non_overlap: bool,
    progress_every_s: float = 5.0,
) -> Tuple[
    List[Dict],
    np.ndarray,
    np.ndarray,
    Counter,
    List[str],
]:
    """
    Sequential build.

    Kept unchanged logically from the original implementation.
    """

    rows = []
    ys = []
    groups = []

    reasons: Counter = Counter()
    used_mints: List[str] = []

    total = len(mints)

    t0 = time.time()
    last_print = t0

    timing: Dict[str, float] = {
        "query_s": 0.0,
        "compute_s": 0.0,
    }

    for i, mint in enumerate(
        mints,
        start=1,
    ):

        status, r, y = build_one(
            store,
            mint,
            cfg,
            bar_fraction,
            stride,
            non_overlap,
            timing=timing,
        )

        reasons[status] += 1

        if status == "ok":

            used_mints.append(mint)

            rows.extend(r)
            ys.extend(y)

            groups.extend(
                [mint] * len(r)
            )

        now = time.time()

        if (
            now - last_print >= progress_every_s
            or i == total
        ):

            elapsed = now - t0

            rate = (
                i / elapsed
                if elapsed > 0
                else 0.0
            )

            eta_s = (
                (total - i) / rate
                if rate > 0
                else float("nan")
            )

            q_pct = (
                100.0
                * timing["query_s"]
                / elapsed
                if elapsed > 0
                else float("nan")
            )

            print(
                f"  [build] "
                f"{i}/{total} mints "
                f"({i / total:.1%}) "
                f"elapsed={elapsed:.0f}s "
                f"ETA~{eta_s:.0f}s "
                f"ok={reasons['ok']} "
                f"insufficient_swaps="
                f"{reasons['insufficient_swaps']} "
                f"insufficient_bars="
                f"{reasons['insufficient_bars']} "
                f"no_usable_labels="
                f"{reasons['no_usable_labels']} "
                f"[query={timing['query_s']:.0f}s "
                f"({q_pct:.0f}%) "
                f"compute="
                f"{timing['compute_s']:.0f}s]",
                flush=True,
            )

            last_print = now

    return (
        rows,
        np.array(ys, dtype=float),
        np.array(groups),
        reasons,
        used_mints,
    )


# ---------------------------------------------------------------------------
# Multiprocessing workers
# ---------------------------------------------------------------------------

_worker_store: Optional[Store] = None
_worker_cfg: Optional[BarrierConfig] = None
_worker_bar_fraction: float = 0.01
_worker_stride: int = 5
_worker_non_overlap: bool = False
_worker_cache_con = None
_worker_cache_path: Optional[str] = None
_worker_names: Optional[List[str]] = None


def _init_worker(
    data_dir: str,
    cfg: BarrierConfig,
    bar_fraction: float,
    stride: int,
    non_overlap: bool,
    memory_limit: str = WORKER_MEMORY_LIMIT,
    cache_path: Optional[str] = None,
) -> None:
    """
    Initialize one DuckDB connection per OS worker.

    Hardware target:
        32 GB RAM
        i5-9600KF

    Configuration:
        5 workers
        1 DuckDB thread / worker
        3 GB DuckDB memory / worker
        10 GB spill / worker

    Source Parquets remain read-only.
    """

    global _worker_store
    global _worker_cfg
    global _worker_bar_fraction
    global _worker_stride
    global _worker_non_overlap
    global _worker_cache_con
    global _worker_cache_path
    global _worker_names

    _worker_names = feature_names_fixed()

    if cache_path:
        # D118: mint-clustered cache -- no Store, no 710-file view
        # registration, no corpus scan per mint.
        _worker_cache_con = open_cache_connection(
            memory_limit,
            WORKER_THREADS,
            os.path.join(WORKER_TEMP_ROOT, f"cache_worker_{os.getpid()}"),
        )
        _worker_cache_path = cache_path
        _worker_cfg = cfg
        _worker_bar_fraction = bar_fraction
        _worker_stride = stride
        _worker_non_overlap = non_overlap
        return

    # ---------------------------------------------------------------
    # READ-ONLY source store
    # ---------------------------------------------------------------

    _worker_store = Store(
        data_dir,
        read_only=True,
    )

    # ---------------------------------------------------------------
    # Important: each process has its OWN DuckDB connection.
    #
    # Do not allow each DuckDB to start 6 threads.
    # Four processes × one thread is intentional.
    # ---------------------------------------------------------------

    _worker_store.con.execute(
        f"SET threads = {WORKER_THREADS}"
    )

    _worker_store.con.execute(
        f"SET memory_limit = '{memory_limit}'"
    )

    # D116: workers were printing their own DuckDB progress bars into the
    # shared console.
    _worker_store.con.execute(
        "SET enable_progress_bar = false"
    )

    # Helps memory usage for large external scans.
    _worker_store.con.execute(
        "SET preserve_insertion_order = false"
    )

    # ---------------------------------------------------------------
    # Scratch directory on E:
    #
    # One directory per PID prevents workers from fighting over the
    # same scratch path.
    # ---------------------------------------------------------------

    worker_temp_dir = os.path.join(
        WORKER_TEMP_ROOT,
        f"worker_{os.getpid()}",
    )

    os.makedirs(
        worker_temp_dir,
        exist_ok=True,
    )

    _worker_store.con.execute(
        "SET temp_directory = ?",
        [worker_temp_dir],
    )

    # Maximum spill for this one worker.
    _worker_store.con.execute(
        f"SET max_temp_directory_size = "
        f"'{WORKER_MAX_TEMP_SIZE}'"
    )

    _worker_cfg = cfg
    _worker_bar_fraction = bar_fraction
    _worker_stride = stride
    _worker_non_overlap = non_overlap


def _worker_build_one(
    mint: str,
) -> Tuple[
    str,
    List[Dict[str, Optional[float]]],
    List[float],
    float,
    float,
]:
    """
    Worker task.

    Returns:
        status
        rows
        ys
        query_s
        compute_s
    """

    timing: Dict[str, float] = {}

    status, rows, ys = build_one(
        _worker_store,
        mint,
        _worker_cfg,
        _worker_bar_fraction,
        _worker_stride,
        _worker_non_overlap,
        timing=timing,
    )

    return (
        status,
        rows,
        ys,
        timing.get("query_s", 0.0),
        timing.get("compute_s", 0.0),
    )



def _swaps_for_mints(
    store: Store,
    mints: Sequence[str],
) -> Dict[str, List[CanonicalSwap]]:
    """
    D116: ONE query for several mints.

    Same view (`swaps`, i.e. the deduplicated one), same ORDER BY within a
    mint as Store.iter_swaps(): the result for each mint is identical to
    list(store.iter_swaps(mint)). Only the number of corpus scans changes
    (1 per chunk instead of 1 per mint).
    """

    out: Dict[str, List[CanonicalSwap]] = {m: [] for m in mints}

    if not mints:
        return out

    in_list = ", ".join(
        "'" + m.replace("'", "''") + "'"
        for m in mints
    )

    df = store.sql(
        f"SELECT * FROM swaps WHERE mint IN ({in_list}) "
        f"ORDER BY mint, ts_ms, slot, sig"
    )

    cols = {
        f.name
        for f in dataclasses.fields(CanonicalSwap)
    }

    for mint, g in df.groupby("mint", sort=False):
        out[mint] = [
            CanonicalSwap(
                **{
                    k: v
                    for k, v in row.items()
                    if k in cols
                }
            )
            for row in g.to_dict("records")
        ]

    del df

    return out


def _worker_build_chunk(
    mints: List[str],
) -> List[
    Tuple[
        str,
        List[Dict[str, Optional[float]]],
        List[float],
        float,
        float,
    ]
]:
    """
    D116 worker task: one DuckDB query for the whole chunk, then the same
    build_one() per mint. Returns one result tuple per mint, IN INPUT ORDER
    (load-bearing for the chronological split).

    A failure is NOT swallowed or turned into a per-mint status: silently
    dropping the tokens that fail (typically the biggest tapes) would be a
    selection effect. It is re-raised with the chunk's identity + RSS so the
    cause is evidence, not a guess.
    """

    try:
        t0 = time.time()

        if _worker_cache_con is not None:
            fetched = fetch_swaps(
                _worker_cache_con,
                _worker_cache_path,
                mints,
            )
        else:
            fetched = _swaps_for_mints(
                _worker_store,
                mints,
            )

        q_share = (time.time() - t0) / max(len(mints), 1)

        results = []

        for m in mints:

            timing: Dict[str, float] = {}

            status, rows, ys = build_one(
                _worker_store,
                m,
                _worker_cfg,
                _worker_bar_fraction,
                _worker_stride,
                _worker_non_overlap,
                timing=timing,
                swaps=fetched.pop(m),
            )

            results.append(
                (
                    status,
                    dicts_to_matrix(rows, _worker_names),
                    ys,
                    q_share,
                    timing.get("compute_s", 0.0),
                )
            )

        return results

    except Exception as e:

        rss = "n/a"

        try:
            import psutil

            rss = (
                f"{psutil.Process().memory_info().rss / 1e9:.2f} GB"
            )
        except Exception:
            pass

        raise RuntimeError(
            f"worker pid={os.getpid()} failed on chunk of "
            f"{len(mints)} mint(s) starting {mints[0]!r}: "
            f"{type(e).__name__}: {e} (worker rss={rss})"
        ) from e


# ---------------------------------------------------------------------------
# Parallel build
# ---------------------------------------------------------------------------

def build_parallel(
    data_dir: str,
    mints: Sequence[str],
    cfg: BarrierConfig,
    bar_fraction: float,
    stride: int,
    non_overlap: bool,
    workers: int,
    progress_every_s: float = 5.0,
    chunk_mints: int = CHUNK_MINTS,
    memory_limit: str = WORKER_MEMORY_LIMIT,
    cache_path: Optional[str] = None,
) -> Tuple[
    List[Dict],
    np.ndarray,
    np.ndarray,
    Counter,
    List[str],
]:
    """
    Parallel build.

    Input order is preserved intentionally because used_mints must remain
    chronological. This is important for the screen/validation/final_test
    split.
    """

    import concurrent.futures

    parts = []
    ys = []
    groups = []

    reasons: Counter = Counter()
    used_mints: List[str] = []

    total = len(mints)

    if total == 0:
        return (
            FeatureMatrix.stack([], feature_names_fixed()),
            np.array([], dtype=float),
            np.array([], dtype=str),
            reasons,
            [],
        )

    t0 = time.time()
    last_print = t0

    timing = {
        "query_s": 0.0,
        "compute_s": 0.0,
    }

    # D116: tasks are CHUNKS of mints (one corpus scan per chunk, not per
    # mint). chunks are consecutive slices of `mints` and ex.map() returns
    # results in input order, so flattening keeps used_mints chronological.
    chunk_mints = max(1, int(chunk_mints))

    chunks = [
        list(mints[k:k + chunk_mints])
        for k in range(0, total, chunk_mints)
    ]

    print(
        f"  [parallel] {len(chunks)} chunk(s) of up to "
        f"{chunk_mints} mint(s); worker memory_limit={memory_limit}",
        flush=True,
    )

    print(
        f"  [parallel] starting "
        f"{workers} worker(s)",
        flush=True,
    )

    print(
        f"  [parallel] worker DuckDB "
        f"threads={WORKER_THREADS} "
        f"memory={WORKER_MEMORY_LIMIT} "
        f"temp={WORKER_MAX_TEMP_SIZE}/worker",
        flush=True,
    )

    print(
        f"  [parallel] worker temp root="
        f"{WORKER_TEMP_ROOT}",
        flush=True,
    )

    with concurrent.futures.ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_worker,
        initargs=(
            data_dir,
            cfg,
            bar_fraction,
            stride,
            non_overlap,
            memory_limit,
            cache_path,
        ),
    ) as ex:

        results = (
            r
            for chunk_result in ex.map(
                _worker_build_chunk,
                chunks,
                chunksize=1,
            )
            for r in chunk_result
        )

        for i, (
            mint,
            result,
        ) in enumerate(
            zip(mints, results),
            start=1,
        ):

            (
                status,
                r,
                y,
                q_s,
                c_s,
            ) = result

            reasons[status] += 1

            timing["query_s"] += q_s
            timing["compute_s"] += c_s

            if status == "ok":

                used_mints.append(mint)

                parts.append(r)
                ys.extend(y)

                groups.extend(
                    [mint] * len(r)
                )

            now = time.time()

            if (
                now - last_print >= progress_every_s
                or i == total
            ):

                elapsed = now - t0

                rate = (
                    i / elapsed
                    if elapsed > 0
                    else 0.0
                )

                eta_s = (
                    (total - i) / rate
                    if rate > 0
                    else float("nan")
                )

                print(
                    f"  [build:parallel x{workers}] "
                    f"{i}/{total} mints "
                    f"({i / total:.1%}) "
                    f"elapsed={elapsed:.0f}s "
                    f"ETA~{eta_s:.0f}s "
                    f"ok={reasons['ok']} "
                    f"insufficient_swaps="
                    f"{reasons['insufficient_swaps']} "
                    f"insufficient_bars="
                    f"{reasons['insufficient_bars']} "
                    f"no_usable_labels="
                    f"{reasons['no_usable_labels']} "
                    f"[summed across workers -- "
                    f"cpu-time query="
                    f"{timing['query_s']:.0f}s "
                    f"compute="
                    f"{timing['compute_s']:.0f}s]"
                    f"{_mem_note()}",
                    flush=True,
                )

                last_print = now

    return (
        FeatureMatrix.stack(parts, feature_names_fixed()),
        np.array(ys, dtype=float),
        np.array(groups),
        reasons,
        used_mints,
    )


# ---------------------------------------------------------------------------
# Chronological token-level split
# ---------------------------------------------------------------------------

def split_tokens(
    mints_sorted: Sequence[str],
    screen_frac: float,
    val_frac: float,
    test_frac: float,
) -> Tuple[
    List[str],
    List[str],
    List[str],
]:
    total = (
        screen_frac
        + val_frac
        + test_frac
    )

    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            "--screen-frac + --val-frac + "
            "--test-frac must sum to 1.0, "
            f"got {total}"
        )

    n = len(mints_sorted)

    n_screen = int(
        round(n * screen_frac)
    )

    n_val = int(
        round(n * val_frac)
    )

    screen = list(
        mints_sorted[:n_screen]
    )

    val = list(
        mints_sorted[
            n_screen:n_screen + n_val
        ]
    )

    test = list(
        mints_sorted[
            n_screen + n_val:
        ]
    )

    return (
        screen,
        val,
        test,
    )


# ---------------------------------------------------------------------------
# Multiple testing permutation null
# ---------------------------------------------------------------------------

def permutation_null_max_edge(
    rows: FeatureMatrix,
    y: np.ndarray,
    groups: np.ndarray,
    names: Sequence[str],
    n_perm: int,
    seed: int,
) -> np.ndarray:
    """
    Token-level permutation null for:

        max |AUC - 0.5|

    across all tested features.

    Labels are reassigned between whole tokens rather than shuffled by row.
    """

    rng = np.random.default_rng(seed)

    uniq = np.array(
        sorted(
            set(groups.tolist())
        )
    )

    idx_by_token = {
        g: np.flatnonzero(
            groups == g
        )
        for g in uniq
    }

    y_by_token = {
        g: y[idx_by_token[g]]
        for g in uniq
    }

    cols = {
        name: rows.col(name)
        for name in names
    }

    progress_every_s = 5.0

    t0 = time.time()
    last_print = t0

    null_max = np.empty(
        n_perm,
        dtype=float,
    )

    for p in range(n_perm):

        donors = rng.permutation(
            uniq
        )

        y_perm = np.empty(
            len(y),
            dtype=float,
        )

        for recipient, donor in zip(
            uniq,
            donors,
        ):

            idx = idx_by_token[
                recipient
            ]

            dy = y_by_token[
                donor
            ]

            if len(dy):

                reps = int(
                    np.ceil(
                        len(idx)
                        / len(dy)
                    )
                )

                y_perm[idx] = np.tile(
                    dy,
                    max(reps, 1),
                )[:len(idx)]

            else:
                y_perm[idx] = np.nan

        best = 0.0

        for name in names:

            a = auc(
                cols[name],
                y_perm,
            )

            if not np.isnan(a):
                best = max(
                    best,
                    abs(a - 0.5),
                )

        null_max[p] = best

        now = time.time()

        if (
            now - last_print >= progress_every_s
            or p + 1 == n_perm
        ):

            elapsed = now - t0

            rate = (
                (p + 1) / elapsed
                if elapsed > 0
                else 0.0
            )

            eta_s = (
                (n_perm - (p + 1))
                / rate
                if rate > 0
                else float("nan")
            )

            print(
                f"  [permutation null] "
                f"{p + 1}/{n_perm} replicates "
                f"({(p + 1) / n_perm:.1%}) "
                f"elapsed={elapsed:.0f}s "
                f"ETA~{eta_s:.0f}s",
                flush=True,
            )

            last_print = now

    return null_max


# ---------------------------------------------------------------------------
# Feature table
# ---------------------------------------------------------------------------

def feature_table(
    rows: FeatureMatrix,
    y: np.ndarray,
    groups: np.ndarray,
    mask: np.ndarray,
    names: Sequence[str],
) -> List[
    Tuple[str, float, float, str, int]
]:
    """
    (name, auc, edge, direction, n)
    """

    out = []

    for name in names:

        col = rows.col(name)

        a = auc(
            col[mask],
            y[mask],
        )

        if np.isnan(a):
            continue

        n = int(
            (
                ~np.isnan(
                    col[mask]
                )
            ).sum()
        )

        out.append(
            (
                name,
                a,
                abs(a - 0.5),
                direction_of(a),
                n,
            )
        )

    out.sort(
        key=lambda r: r[2],
        reverse=True,
    )

    return out


# ---------------------------------------------------------------------------
# Family table
# ---------------------------------------------------------------------------

def family_table(
    results: List[
        Tuple[str, float, float, str, int]
    ],
) -> List[
    Tuple[str, str, float, float, str, int]
]:
    """
    Best-in-family from individual feature results.
    """

    by_family: Dict[
        str,
        List[
            Tuple[str, float, float, str, int]
        ],
    ] = defaultdict(list)

    for (
        name,
        a,
        edge,
        d,
        n,
    ) in results:

        by_family[
            family_of(name)
        ].append(
            (
                name,
                a,
                edge,
                d,
                n,
            )
        )

    out = []

    for fam, members in by_family.items():

        best = max(
            members,
            key=lambda r: r[2],
        )

        out.append(
            (
                fam,
                best[0],
                best[1],
                best[2],
                best[3],
                len(members),
            )
        )

    out.sort(
        key=lambda r: r[3],
        reverse=True,
    )

    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:

    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0]
    )

    ap.add_argument(
        "--data",
        default="data",
    )

    ap.add_argument(
        "--since",
        default=None,
    )

    ap.add_argument(
        "--until",
        default=None,
    )

    ap.add_argument(
        "--limit",
        type=int,
        default=2000,
        help=(
            "cap on the ELIGIBLE, chronologically-sorted universe "
            "(earliest N tokens kept)"
        ),
    )

    ap.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            "OS worker processes for build(). "
            f"Default={DEFAULT_WORKERS}. "
            "For i5-9600KF + 32 GB RAM this is deliberately below "
            "the 6 physical cores."
        ),
    )

    ap.add_argument(
        "--chunk-mints",
        type=int,
        default=CHUNK_MINTS,
        help=(
            "mints fetched per DuckDB query in each worker (D116); "
            "each query scans the corpus once, so bigger = fewer scans "
            "but more RAM per query"
        ),
    )

    ap.add_argument(
        "--swaps-cache",
        default=None,
        help=(
            "mint-clustered derived swaps parquet (D118, "
            "scripts/build_swaps_cache.py). Default: "
            f"{SWAPS_CACHE_DEFAULT} if it exists."
        ),
    )

    ap.add_argument(
        "--no-swaps-cache",
        action="store_true",
        help="ignore the cache; query data/swaps directly (slow)",
    )

    ap.add_argument(
        "--allow-stale-cache",
        action="store_true",
        help="use a cache even if data/swaps changed since it was built",
    )

    ap.add_argument(
        "--worker-memory",
        default=WORKER_MEMORY_LIMIT,
        help=(
            "DuckDB memory_limit per worker, e.g. 2GB"
        ),
    )

    ap.add_argument(
        "--stride",
        type=int,
        default=5,
    )

    ap.add_argument(
        "--upper",
        type=float,
        default=1.6,
    )

    ap.add_argument(
        "--lower",
        type=float,
        default=-0.30,
    )

    ap.add_argument(
        "--horizon-min",
        type=float,
        default=30.0,
    )

    ap.add_argument(
        "--bar-fraction",
        type=float,
        default=0.01,
    )

    ap.add_argument(
        "--screen-frac",
        type=float,
        default=0.5,
    )

    ap.add_argument(
        "--val-frac",
        type=float,
        default=0.2,
    )

    ap.add_argument(
        "--test-frac",
        type=float,
        default=0.3,
    )

    ap.add_argument(
        "--n-boot",
        type=int,
        default=2000,
        help=(
            "AUC cluster-bootstrap replicates"
        ),
    )

    ap.add_argument(
        "--n-perm",
        type=int,
        default=200,
        help=(
            "permutation-null replicates"
        ),
    )

    ap.add_argument(
        "--non-overlap",
        action="store_true",
        help=(
            "thin each token's labels to a temporally-disjoint subset"
        ),
    )

    ap.add_argument(
        "--seed",
        type=int,
        default=0,
    )

    ap.add_argument(
        "--top",
        type=int,
        default=20,
    )

    ap.add_argument(
        "--real-creation-file",
        default=None,
        help=(
            "path to real_creation_times.json"
        ),
    )

    ap.add_argument(
        "--max-observed-age-hours",
        type=float,
        default=6.0,
        help=(
            "exclude token if it was already older than this"
        ),
    )

    ap.add_argument(
        "--no-age-filter",
        action="store_true",
        help=(
            "disable real-creation-time filter"
        ),
    )

    args = ap.parse_args()

    # -------------------------------------------------------------------
    # Worker validation
    # -------------------------------------------------------------------

    if args.workers < 1:
        raise ValueError(
            "--workers must be >= 1"
        )

    # D116: Store.con reads TAPE_DUCKDB_TMP when it opens a connection, so
    # workers (spawned, inheriting this env) spill to E: from the first query
    # instead of creating .duckdb_tmp\\pid<N> on D: and moving later.
    os.environ.setdefault("TAPE_DUCKDB_TMP", WORKER_TEMP_ROOT)

    # -------------------------------------------------------------------
    # Startup message
    # -------------------------------------------------------------------

    print(
        f"[info-audit] "
        f"data={args.data!r} "
        f"since={args.since!r} "
        f"until={args.until!r} "
        f"limit={args.limit} "
        f"workers={args.workers} "
        f"-- starting",
        flush=True,
    )

    print(
        "[info-audit] SOURCE DATA IS READ-ONLY",
        flush=True,
    )

    commit_preflight(
        args.workers * parse_gb(args.worker_memory) + 4.0,
        f"{args.workers} workers + main process",
    )

    print(
        "[info-audit] "
        f"worker RAM={WORKER_MEMORY_LIMIT}/worker "
        f"threads={WORKER_THREADS}/worker "
        f"worker temp={WORKER_TEMP_ROOT} "
        f"max spill={WORKER_MAX_TEMP_SIZE}/worker",
        flush=True,
    )

    print(
        "[info-audit] "
        f"universe RAM={UNIVERSE_MEMORY_LIMIT} "
        f"universe temp={UNIVERSE_TEMP_DIR} "
        f"max spill={UNIVERSE_MAX_TEMP_SIZE}",
        flush=True,
    )

    # -------------------------------------------------------------------
    # Main Store
    #
    # StoreV2 is used ONLY by the main process for the chronological
    # universe scan.
    #
    # Workers intentionally use normal read-only Store instances.
    # -------------------------------------------------------------------

    store = StoreV2(
        args.data,
        read_only=True,
        temp_dir=UNIVERSE_TEMP_DIR,
        memory_limit=UNIVERSE_MEMORY_LIMIT,
        max_temp_size=UNIVERSE_MAX_TEMP_SIZE,
    )

    # -------------------------------------------------------------------
    # Universe
    #
    # IMPORTANT:
    #
    # This intentionally uses StoreV2.chronological_universe()
    # instead of the old whole-corpus GROUP BY in this file.
    # -------------------------------------------------------------------

    universe = store.chronological_universe(
        args.since,
        args.until,
    )

    requested = len(universe)

    eligible_by_swaps = [
        (m, ts)
        for m, ts, n in universe
        if n >= MIN_SWAPS_PER_TOKEN
    ]

    excluded_by_swaps = (
        requested
        - len(eligible_by_swaps)
    )

    # -------------------------------------------------------------------
    # Real creation time
    # -------------------------------------------------------------------

    real_creation_path = (
        Path(args.real_creation_file)
        if args.real_creation_file
        else Path(args.data)
        / "real_creation_times.json"
    )

    cache = None

    if (
        not args.no_age_filter
        and real_creation_path.exists()
    ):

        try:

            cache = load_real_creation_cache(
                real_creation_path
            )

        except Exception as e:

            print(
                f"  (could not read "
                f"{real_creation_path}: {e} "
                f"-- age filter disabled)",
                flush=True,
            )

    excluded_too_old = 0
    excluded_no_real_creation = 0

    if cache is not None:

        (
            eligible,
            excluded_too_old,
            excluded_no_real_creation,
        ) = filter_by_real_creation(
            eligible_by_swaps,
            cache,
            args.max_observed_age_hours,
        )

    else:

        eligible = eligible_by_swaps

    # D116: the real-creation cache (millions of dict entries) and the full
    # universe list are only needed up to here. Free them before the worker
    # pool starts so the main process is not holding GBs it no longer uses.
    n_eligible_by_swaps = len(eligible_by_swaps)
    cache_loaded = cache is not None

    del cache
    del universe
    del eligible_by_swaps
    gc.collect()

    # -------------------------------------------------------------------
    # Limit
    # -------------------------------------------------------------------

    limited = eligible[:args.limit]

    excluded_by_limit = (
        len(eligible)
        - len(limited)
    )

    limited_mints = [
        m
        for m, _ in limited
    ]

    # -------------------------------------------------------------------
    # Universe report
    # -------------------------------------------------------------------

    print(
        "=" * 78
    )

    print(
        "UNIVERSE"
    )

    print(
        "=" * 78
    )

    print(
        f"  requested "
        f"(any swap, in [{args.since}, {args.until}]): "
        f"{requested}"
    )

    print(
        f"  excluded "
        f"(< {MIN_SWAPS_PER_TOKEN} swaps): "
        f"{excluded_by_swaps}"
    )

    print(
        f"  eligible-by-swaps "
        f"(>= {MIN_SWAPS_PER_TOKEN} swaps): "
        f"{n_eligible_by_swaps}"
    )

    if cache_loaded:

        print(
            f"  excluded "
            f"(already >{args.max_observed_age_hours:.1f}h old, "
            f"real creation time): "
            f"{excluded_too_old}"
        )

        print(
            f"  excluded "
            f"(no real-creation-time data): "
            f"{excluded_no_real_creation}"
        )

        print(
            f"  eligible-after-age-filter: "
            f"{len(eligible)}"
        )

    else:

        print(
            "  NOTE: age filter DISABLED"
        )

        print(
            f"  cache path: "
            f"{real_creation_path}"
        )

        print(
            "  Universe may mix genuinely new launches "
            "with older survivor tokens."
        )

    print(
        f"  excluded "
        f"(beyond --limit {args.limit}, earliest kept): "
        f"{excluded_by_limit}"
    )

    print(
        f"  considered for build: "
        f"{len(limited_mints)}"
    )

    if len(limited_mints) < 30:

        print(
            f"\nstatus=collecting_data "
            f"eligible_mints={len(limited_mints)} "
            f"(need >= 30)"
        )

        return 2

    # -------------------------------------------------------------------
    # Label configuration
    # -------------------------------------------------------------------

    cfg = BarrierConfig(
        args.upper,
        args.lower,
        int(
            args.horizon_min
            * 60_000
        ),
    )

    # -------------------------------------------------------------------
    # Build
    # -------------------------------------------------------------------

    cache_path = None

    if not args.no_swaps_cache:

        cp = args.swaps_cache or SWAPS_CACHE_DEFAULT

        if Path(cp).exists():

            if not args.allow_stale_cache:
                check_cache_fresh(cp, args.data)

            cache_path = cp

            print(
                f"  swaps cache: {cp} (fingerprint matches data/swaps)",
                flush=True,
            )

        elif args.swaps_cache:

            raise FileNotFoundError(args.swaps_cache)

        else:

            print(
                f"  NOTE: no swaps cache at {cp} -- using the slow "
                f"per-chunk corpus scans. Build it once with: "
                f"python scripts\\build_swaps_cache.py --data {args.data}",
                flush=True,
            )

    if args.workers > 1:

        print(
            f"  running build() across "
            f"{args.workers} worker process(es)",
            flush=True,
        )

        rows, y, groups, reasons, used_mints = build_parallel(
            args.data,
            limited_mints,
            cfg,
            args.bar_fraction,
            args.stride,
            args.non_overlap,
            args.workers,
            chunk_mints=args.chunk_mints,
            memory_limit=args.worker_memory,
            cache_path=cache_path,
        )

    else:

        print(
            "  running build() sequentially",
            flush=True,
        )

        rows, y, groups, reasons, used_mints = build(
            store,
            limited_mints,
            cfg,
            args.bar_fraction,
            args.stride,
            args.non_overlap,
        )

        rows = FeatureMatrix.from_dicts(
            rows,
            feature_names_fixed(),
        )

    print(
        f"  excluded "
        f"(< {MIN_BARS_PER_TOKEN} bars): "
        f"{reasons['insufficient_bars']}"
    )

    print(
        f"  excluded "
        f"(no usable, non-truncated label): "
        f"{reasons['no_usable_labels']}"
    )

    print(
        f"  used: "
        f"{len(used_mints)}"
    )

    print(
        f"  rows (bar-labels, pre-split): "
        f"{len(y)}"
    )

    if len(y) < 500:

        print(
            f"\nstatus=collecting_data "
            f"usable_rows={len(y)}"
        )

        return 2

    # -------------------------------------------------------------------
    # Chronological token split
    # -------------------------------------------------------------------

    screen_mints, val_mints, test_mints = split_tokens(
        used_mints,
        args.screen_frac,
        args.val_frac,
        args.test_frac,
    )

    screen_set = set(screen_mints)
    val_set = set(val_mints)
    test_set = set(test_mints)

    is_screen = np.array(
        [
            g in screen_set
            for g in groups
        ]
    )

    is_val = np.array(
        [
            g in val_set
            for g in groups
        ]
    )

    is_test = np.array(
        [
            g in test_set
            for g in groups
        ]
    )

    ts_by_mint = {
        m: ts
        for m, ts in eligible
    }

    def _bounds(mints):

        if not mints:
            return None, None

        tss = [
            ts_by_mint[m]
            for m in mints
        ]

        return (
            min(tss),
            max(tss),
        )

    print(
        "\n" + "=" * 78
    )

    print(
        "TIME "
        "(chronological split by TOKEN -- never by row, never randomly)"
    )

    print(
        "=" * 78
    )

    for label, mints, mask in (
        (
            "screen",
            screen_mints,
            is_screen,
        ),
        (
            "validation",
            val_mints,
            is_val,
        ),
        (
            "final_test",
            test_mints,
            is_test,
        ),
    ):

        lo, hi = _bounds(mints)

        print(
            f"  {label:11s} "
            f"tokens={len(mints):5d} "
            f"rows={int(mask.sum()):6d} "
            f"first_ts=[{lo}, {hi}]"
        )

    if args.val_frac == 0:

        print(
            "  NOTE: --val-frac=0 -- "
            "2-stage screen/final_test design."
        )

    if not test_mints or not screen_mints:

        print(
            "\nstatus=collecting_data "
            "not enough tokens to fill every split"
        )

        return 2

    # -------------------------------------------------------------------
    # Feature names
    # -------------------------------------------------------------------

    names = list(rows.names)

    # -------------------------------------------------------------------
    # Labels report
    # -------------------------------------------------------------------

    print(
        "\n" + "=" * 78
    )

    print(
        "LABELS"
    )

    print(
        "=" * 78
    )

    print(
        f"  horizon={args.horizon_min}min "
        f"upper_multiple={args.upper} "
        f"lower_pct={args.lower} "
        f"stride={args.stride} "
        f"non_overlap={args.non_overlap}"
    )

    validation_base_rate = (
        y[is_val].mean()
        if is_val.any()
        else float("nan")
    )

    print(
        f"  screen base_rate={y[is_screen].mean():.4f} "
        f"validation base_rate={validation_base_rate:.4f} "
        f"final_test base_rate={y[is_test].mean():.4f}"
    )

    # -------------------------------------------------------------------
    # Screen feature table
    # -------------------------------------------------------------------

    print(
        "\n" + "=" * 78
    )

    print(
        f"FEATURES (n={len(names)}, "
        f"evaluated on SCREEN only)"
    )

    print(
        "=" * 78
    )

    screen_results = feature_table(
        rows,
        y,
        groups,
        is_screen,
        names,
    )

    print(
        f"{'feature':38s} "
        f"{'AUC':>8s} "
        f"{'edge':>8s} "
        f"{'direction':>10s} "
        f"{'n':>8s}"
    )

    print(
        "-" * 78
    )

    for (
        name,
        a,
        edge,
        d,
        n,
    ) in screen_results[:args.top]:

        print(
            f"{name:38s} "
            f"{a:8.4f} "
            f"{edge:8.4f} "
            f"{d:>10s} "
            f"{n:8d}"
        )

    # -------------------------------------------------------------------
    # Families
    # -------------------------------------------------------------------

    print(
        "\nFEATURE FAMILIES "
        "(best-in-family, screen set)"
    )

    fam_results = family_table(
        screen_results
    )

    print(
        f"{'family':30s} "
        f"{'best member':38s} "
        f"{'AUC':>8s} "
        f"{'edge':>8s} "
        f"{'dir':>10s} "
        f"{'#in fam':>8s}"
    )

    print(
        "-" * 100
    )

    for (
        fam,
        best_name,
        a,
        edge,
        d,
        k,
    ) in fam_results[:args.top]:

        print(
            f"{fam:30s} "
            f"{best_name:38s} "
            f"{a:8.4f} "
            f"{edge:8.4f} "
            f"{d:>10s} "
            f"{k:8d}"
        )

    if not screen_results:

        print(
            "\nVERDICT: no_signal -- "
            "no feature produced a finite AUC."
        )

        return 1

    best_name, best_auc, best_edge, best_dir, best_n = (
        screen_results[0]
    )

    # -------------------------------------------------------------------
    # Multiple-testing null
    # -------------------------------------------------------------------

    null = permutation_null_max_edge(
        rows.subset(is_screen),
        y[is_screen],
        groups[is_screen],
        names,
        args.n_perm,
        args.seed,
    )

    p_value = float(
        (
            1
            + (null >= best_edge).sum()
        )
        / (
            len(null) + 1
        )
    )

    print(
        "\n" + "=" * 78
    )

    print(
        "MULTIPLE-TESTING NULL"
    )

    print(
        "=" * 78
    )

    print(
        f"  {len(names)} features tested on screen; "
        f"permutation replicates={args.n_perm}"
    )

    print(
        f"  null max|AUC-0.5|: "
        f"mean={null.mean():.4f} "
        f"p95={np.percentile(null, 95):.4f} "
        f"p99={np.percentile(null, 99):.4f}"
    )

    print(
        f"  observed best (screen): "
        f"{best_name} "
        f"edge={best_edge:.4f}"
    )

    print(
        f"  permutation p-value: "
        f"{p_value:.4f} "
        f"({'below' if p_value < 0.05 else 'NOT below'} 0.05)"
    )

    # -------------------------------------------------------------------
    # Validation
    # -------------------------------------------------------------------

    val_confirms = None

    if val_mints:

        col = rows.col(best_name)

        a_val = auc(
            col[is_val],
            y[is_val],
        )

        val_confirms = (
            not np.isnan(a_val)
            and direction_of(a_val) == best_dir
            and abs(a_val - 0.5) > 0.0
        )

        print(
            "\n" + "=" * 78
        )

        print(
            "VALIDATION "
            "(same pre-specified feature; never re-selects)"
        )

        print(
            "=" * 78
        )

        print(
            f"  {best_name} "
            f"AUC(validation)={a_val:.4f} "
            f"direction={direction_of(a_val)} "
            f"(screen direction was {best_dir})"
        )

    # -------------------------------------------------------------------
    # Final test
    # -------------------------------------------------------------------

    print(
        "\n" + "=" * 78
    )

    print(
        "BEST PRE-SPECIFIED FEATURE"
    )

    print(
        "=" * 78
    )

    print(
        f"  {best_name} "
        f"(family: {family_of(best_name)})"
    )

    print(
        "  chosen on SCREEN only, "
        "before final_test was read"
    )

    col = rows.col(best_name)

    a_final = auc(
        col[is_test],
        y[is_test],
    )

    n_test_obs = int(
        is_test.sum()
    )

    # -------------------------------------------------------------------
    # Bootstrap
    # -------------------------------------------------------------------

    print(
        f"\n  [bootstrap CI] "
        f"running {args.n_boot} "
        f"token-cluster bootstrap replicates "
        f"on {n_test_obs} final_test row(s)...",
        flush=True,
    )

    t_boot = time.time()

    lo, hi = auc_cluster_bootstrap(
        col[is_test],
        y[is_test],
        groups[is_test],
        auc,
        n_boot=args.n_boot,
        seed=args.seed,
    )

    print(
        f"  [bootstrap CI] "
        f"done in {time.time() - t_boot:.1f}s",
        flush=True,
    )

    n_test_tokens = len(
        test_mints
    )

    # -------------------------------------------------------------------
    # Final evaluation report
    # -------------------------------------------------------------------

    print(
        "\n" + "=" * 78
    )

    print(
        "FINAL HELD-OUT EVALUATION "
        "(final_test, touched once, never used to select)"
    )

    print(
        "=" * 78
    )

    print(
        f"  MAX EDGE (final_test): "
        f"AUC={a_final:.4f} "
        f"edge={abs(a_final - 0.5):.4f} "
        f"direction={direction_of(a_final)}"
    )

    print(
        f"  95% CI "
        f"(token-cluster bootstrap, "
        f"{args.n_boot} reps): "
        f"[{lo:.4f}, {hi:.4f}]"
    )

    print(
        f"  test tokens={n_test_tokens} "
        f"test observations={n_test_obs}"
    )

    ci_excludes_half = (
        not np.isnan(lo)
        and not np.isnan(hi)
        and (
            lo > 0.5
            or hi < 0.5
        )
    )

    print(
        f"  CI excludes 0.5: "
        f"{ci_excludes_half}"
    )

    # -------------------------------------------------------------------
    # Verdict
    # -------------------------------------------------------------------

    print(
        "\n" + "=" * 78
    )

    signal = (
        p_value < 0.05
        and ci_excludes_half
    )

    if signal:

        print(
            "VERDICT: signal_present -- GATE PASSED."
        )

        print(
            "  At least one pre-specified feature "
            "shows a statistically detectable"
        )

        print(
            "  out-of-sample association with the "
            "defined future barrier outcome."
        )

        print(
            "  Its screen-set edge clears the run's "
            "multiple-testing null,"
        )

        print(
            "  AND its final_test AUC's 95% CI "
            "excludes 0.5."
        )

        print(
            "  This is NOT a claim that trading this "
            "feature is profitable -- costs,"
        )

        print(
            "  slippage, sizing and execution are "
            "untested here."
        )

    else:

        print(
            "VERDICT: no_edge_found -- GATE FAILED."
        )

        print(
            f"  permutation p-value={p_value:.4f} "
            f"(want <0.05), "
            f"final_test CI excludes 0.5: "
            f"{ci_excludes_half}."
        )

        print(
            "  Try a different HORIZON or a different "
            "UNIVERSE."
        )

        print(
            "  Do NOT reach for a bigger model: if "
            "nothing predicts maximum"
        )

        print(
            "  favourable excursion under a clean "
            "split, nothing will predict profit either."
        )

    return 0 if signal else 1


# ---------------------------------------------------------------------------
# Windows multiprocessing entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(main())