#!/usr/bin/env python3
"""Stage 1 -- the information audit. Run this BEFORE fitting anything.

One question, answered without training a model: did we discover information
that was genuinely available before the future price move, and does that
information survive evaluation on tokens that played no role in selecting it?

For each feature, measure out-of-sample AUC against the MODEL-FREE excursion
label (did price reach +X% before -Y%). "Out-of-sample" here means three
separate things that all have to hold at once, or the number is fiction:

  1. TEMPORAL:   the evaluation tokens' tapes started strictly after every
                 token used to pick or tune anything (screen + validation).
  2. SELECTION:  the feature/family reported at the end was named BEFORE the
                 final test set was touched -- never "whichever scored best
                 on the held-out data", which is not held out at all.
  3. MULTIPLE-TESTING: dozens of features are tried, so some will clear any
                 fixed AUC bar by chance alone. The bar itself must be set by
                 a null built from THIS run's own feature count and token
                 structure, not a fixed constant like 0.55.

This script is a measurement, not a model. It fits nothing: no gradient
boosting, no neural net, no hyperparameter search, no feature engineered from
a future observation. If it fails, the fix is a different universe or a
different horizon -- NOT a bigger model.

    python scripts/information_audit.py --data data --since 2026-09-01

See docs/DECISIONS.md (D53) for the full audit of the previous version of
this script and exactly which of the above three properties it violated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from tape.bars import BarBuilder, band_bar_threshold
from tape.cv import auc_cluster_bootstrap
from tape.features import TokenState
from tape.labels import BarrierConfig, label_series, thin_non_overlapping
from tape.store import Store

MIN_SWAPS_PER_TOKEN = 50   # corpus filter: below this a tape is mostly noise
MIN_BARS_PER_TOKEN = 20    # need enough bars for features to have warmed up


# ---------------------------------------------------------------------------
# AUC
# ---------------------------------------------------------------------------

def auc(scores: np.ndarray, y: np.ndarray) -> float:
    """Rank AUC, ties averaged. No sklearn dependency so this runs anywhere.

    Tie-averaging is not cosmetic: without it a constant feature (every score
    tied) scores AUC=1.0 instead of the correct 0.5, and "AUC 1.0" on the
    verdict line for a feature that is a constant is exactly the kind of bug
    that looks like a miracle discovery. `tests/test_information_audit.py`
    checks both this and the sign-flip identity AUC(-s, y) == 1 - AUC(s, y).
    """
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y, dtype=float)
    m = ~np.isnan(scores)
    s, yy = scores[m], y[m]
    n_pos, n_neg = int(yy.sum()), int(len(yy) - yy.sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    _, inv, counts = np.unique(s, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    ranks = (sums / counts)[inv]
    return float((ranks[yy == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


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
# Feature families -- derived from the repo's OWN naming structure, not
# invented. `tape/features.py::TokenState.features()` builds netflow_{k},
# netflow_{k}_over_liq, buysell_ratio_{k}, ret_{k}, vol_{k} and
# unique_buyers_{k}/largest_buyer_share_{k}/liq_slope_{k} from a single
# `for k in (3, 10, 30):` loop (and one more `_10` outside that loop). The
# only real "family" structure present in the code is this window-length
# suffix: collapsing it is not a category invented for this audit, it is the
# repo's own repetition made explicit. Every other feature name has no such
# suffix and is its own singleton family.
# ---------------------------------------------------------------------------

_WINDOW_SUFFIX = re.compile(r"_(3|10|30)(?=_|$)")


def family_of(name: str) -> str:
    fam = _WINDOW_SUFFIX.sub("", name)
    return fam if fam else name


# ---------------------------------------------------------------------------
# Universe: explicit, chronological, reported -- never `store.mints()`'s
# alphabetical order truncated by a `--limit` slice.
# ---------------------------------------------------------------------------

def chronological_universe(store: Store, since: Optional[str], until: Optional[str]
                           ) -> List[Tuple[str, int, int]]:
    """(mint, first_ts_ms, n_swaps) for every mint with >=1 swap in range,
    sorted by REAL first-seen time. `Store.mints()` orders `ORDER BY mint`
    (alphabetical on the address string) -- confirmed by reading tape/store.py
    directly, not assumed -- so it cannot be used as-is for a chronological
    split. `Store.sql()` is the store's own documented raw-SQL escape hatch,
    used here instead of adding a duplicate method to Store for one query.
    """
    where = []
    if since:
        where.append(f"dt >= '{since}'")
    if until:
        where.append(f"dt <= '{until}'")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # D110 (docs/DECISIONS.md): this is the single biggest *silent* stretch in
    # the whole script -- it's both the first real query against `store`
    # (which lazily opens the DuckDB connection and registers the `swaps`
    # view over the ENTIRE data/swaps/**/*.parquet glob, D80/D104) and a full
    # GROUP BY scan of that same corpus, with no way to report partial
    # progress mid-query (DuckDB gives no row-level callback for a single
    # aggregate query). A real run hit several real minutes of dead silence
    # here once data/swaps/ grew large/fragmented enough -- confirmed live,
    # not assumed -- so this prints before/after rather than leaving the user
    # unable to tell "still working" from "hung".
    print(f"  [chronological_universe] opening Store + scanning data/swaps/ "
          f"(first query against the Store -- registers the `swaps` view over "
          f"the whole corpus, then GROUP BY's it; can take minutes on a large "
          f"or fragmented data/swaps/, with NO partial progress possible "
          f"mid-query)...", flush=True)
    t0 = time.time()
    df = store.sql(f"""
        SELECT mint, min(ts_ms) AS first_ts_ms, count(*) AS n
        FROM swaps {clause}
        GROUP BY mint ORDER BY first_ts_ms, mint
    """)
    print(f"  [chronological_universe] done in {time.time() - t0:.1f}s -- "
          f"{len(df)} mint(s) found", flush=True)
    return list(zip(df["mint"].tolist(), df["first_ts_ms"].tolist(), df["n"].tolist()))


# ---------------------------------------------------------------------------
# Real-creation-time cache (D55, docs/DECISIONS.md): `first_ts_ms` above is
# when OUR OWN discovery first saw a mint trade -- not when the token was
# created. Bitquery's `discover()` surfaces any mint with recent activity,
# not specifically new launches, so a real, live-confirmed fraction of the
# universe are tokens that are months-to-years old and merely traded again
# recently. Mixing those into a Stage 1 gate meant to test EARLY-LIFE signal
# in NEW launches is a confound, not more data -- this cache (built by
# `scripts/fetch_real_creation_times.py`, optional, never required) lets
# main() filter them out instead of silently treating "was recently
# rediscovered" as "was recently created".
# ---------------------------------------------------------------------------

def load_real_creation_cache(path: Path) -> Dict[str, dict]:
    """D112 (docs/DECISIONS.md): another real silent-for-a-while point found
    while chasing the same "is it stuck?" complaint D110 was about --
    `data/real_creation_times.json` is now ~400MB after the pumpfundata
    creation-time backfill (D109), and a single blocking `json.load()` over a
    file that size is a real, noticeable, zero-feedback pause of its own,
    distinct from the Store/DuckDB-side pauses D110 already covers. Shared
    by information_audit.py, paper_trade_replay.py, scenario_backtest.py, and
    token_dna_report.py (all import this one function rather than each
    re-implementing it), so fixing it here covers all four call sites at
    once."""
    size_mb = path.stat().st_size / 1_000_000
    print(f"  [load_real_creation_cache] reading {path} ({size_mb:.0f} MB)...",
          flush=True)
    t0 = time.time()
    with open(path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    print(f"  [load_real_creation_cache] done in {time.time() - t0:.1f}s -- "
          f"{len(cache)} mint(s) in cache", flush=True)
    return cache


def filter_by_real_creation(eligible: List[Tuple[str, int]], cache: Dict[str, dict],
                            max_observed_age_hours: float
                            ) -> Tuple[List[Tuple[str, int]], int, int]:
    """Keep (mint, first_ts_ms) pairs only when the cache says the mint was
    genuinely young (<= `max_observed_age_hours` old, in REAL creation time)
    at the instant `first_ts_ms` (our own first-seen time) happened.

    Returns (kept, excluded_too_old, excluded_no_data). A missing cache entry,
    a non-"ok" status, or a null `real_created_ts_ms` all count as "no data" --
    never silently treated as "young enough to keep".
    """
    max_gap_ms = max_observed_age_hours * 3_600_000
    kept: List[Tuple[str, int]] = []
    excluded_too_old = 0
    excluded_no_data = 0
    for m, ts in eligible:
        rec = cache.get(m)
        if not rec or rec.get("status") != "ok" or rec.get("real_created_ts_ms") is None:
            excluded_no_data += 1
            continue
        if (ts - rec["real_created_ts_ms"]) > max_gap_ms:
            excluded_too_old += 1
            continue
        kept.append((m, ts))
    return kept, excluded_too_old, excluded_no_data


# ---------------------------------------------------------------------------
# Build: one token's rows, with an explicit reason when a token contributes
# nothing, so the universe accounting in main() is never a silent drop.
# ---------------------------------------------------------------------------

def build_one(store: Store, mint: str, cfg: BarrierConfig, bar_fraction: float,
             stride: int, non_overlap: bool, timing: Optional[Dict[str, float]] = None
             ) -> Tuple[str, List[Dict[str, Optional[float]]], List[float]]:
    """`timing`, when passed, is updated IN PLACE with `query_s` (time spent
    in `store.iter_swaps()` -- the DuckDB query + per-row CanonicalSwap
    construction) and `compute_s` (bar/feature/label construction) -- added
    (D113, docs/DECISIONS.md) specifically to answer "where does the
    ~0.3s/mint actually go" with real evidence before reaching for
    multiprocessing or a GPU port (neither of which help the wrong
    bottleneck). Optional and defaulted so no existing call site breaks."""
    _t0 = time.time()
    swaps = list(store.iter_swaps(mint))
    if timing is not None:
        timing["query_s"] = timing.get("query_s", 0.0) + (time.time() - _t0)
    if len(swaps) < MIN_SWAPS_PER_TOKEN:
        return "insufficient_swaps", [], []
    _t1 = time.time()
    try:
        depth = next((s.quote_reserve_after for s in swaps if s.quote_reserve_after), None) or 1.0
        bb = BarBuilder("dollar", threshold=band_bar_threshold(depth, bar_fraction))
        st = TokenState(mint, created_ts_ms=swaps[0].ts_ms)
        feats, h, l, c, t = [], [], [], [], []
        for s in swaps:
            bar = bb.push(s)
            if bar is None:
                continue
            # Feature order matters here: `st.update(bar)` folds bar i in, THEN
            # `st.features()` is read -- so feats[i] is "everything knowable the
            # instant bar i closed", never bar i+1 or later (tests/test_no_lookahead.py
            # already proves `update()` cannot see forward; this just proves the
            # call order here doesn't undo that guarantee).
            st.update(bar)
            feats.append(dict(st.features()))
            h.append(bar.high); l.append(bar.low); c.append(bar.close)
            t.append(bar.close_ts_ms)
        if len(c) < MIN_BARS_PER_TOKEN:
            return "insufficient_bars", [], []

        labels = label_series(h, l, c, t, cfg, mint=mint, stride=stride)
        labels = [lab for lab in labels if not lab.truncated]
        if non_overlap:
            keep = set(thin_non_overlapping(labels))
            labels = [lab for i, lab in enumerate(labels) if i in keep]
        if not labels:
            return "no_usable_labels", [], []

        rows = [feats[lab.bar_index] for lab in labels]
        ys = [float(lab.y) for lab in labels]
        return "ok", rows, ys
    finally:
        if timing is not None:
            timing["compute_s"] = timing.get("compute_s", 0.0) + (time.time() - _t1)


def build(store: Store, mints: Sequence[str], cfg: BarrierConfig, bar_fraction: float,
         stride: int, non_overlap: bool, progress_every_s: float = 5.0
         ) -> Tuple[List[Dict], np.ndarray, np.ndarray, Counter, List[str]]:
    """D110 (docs/DECISIONS.md): this loop does one `store.iter_swaps(mint)`
    query PER mint (up to `--limit`, default 2000) plus bar/feature/label
    construction on each -- real per-mint cost varies hugely (some pump.fun
    tapes carry a handful of swaps, others tens of thousands), so a fixed
    mint-count tick would either spam on the cheap ones or go silent for a
    long stretch on an expensive one. Throttled by WALL-CLOCK time instead
    (`progress_every_s`, default every 5s) -- confirmed live that a run here
    can otherwise sit with zero output for minutes, indistinguishable from a
    hang. ETA is a simple linear extrapolation from the average per-mint
    time seen so far; it's a rough guide, not a guarantee -- per-mint cost is
    not uniform (see above)."""
    rows, ys, groups = [], [], []
    reasons: Counter = Counter()
    used_mints: List[str] = []
    total = len(mints)
    t0 = time.time()
    last_print = t0
    # D113 (docs/DECISIONS.md): cumulative query-vs-compute split, so a slow
    # run tells you WHERE the time is going (DuckDB query/row-construction vs
    # bar/feature/label building) instead of just THAT it's slow -- the two
    # have very different fixes (query-bound suggests Store/DuckDB-side work
    # or per-process parallelism across mints; compute-bound suggests
    # optimizing build_one's Python loop itself). Neither is GPU-shaped
    # work: no matrix math here, just DuckDB I/O and small sequential
    # per-swap state updates.
    timing: Dict[str, float] = {"query_s": 0.0, "compute_s": 0.0}
    for i, mint in enumerate(mints, start=1):
        status, r, y = build_one(store, mint, cfg, bar_fraction, stride, non_overlap, timing=timing)
        reasons[status] += 1
        if status == "ok":
            used_mints.append(mint)
            rows.extend(r)
            ys.extend(y)
            groups.extend([mint] * len(r))
        now = time.time()
        if now - last_print >= progress_every_s or i == total:
            elapsed = now - t0
            rate = i / elapsed if elapsed > 0 else 0.0
            eta_s = (total - i) / rate if rate > 0 else float("nan")
            q_pct = 100.0 * timing["query_s"] / elapsed if elapsed > 0 else float("nan")
            print(f"  [build] {i}/{total} mints ({i / total:.1%})  "
                  f"elapsed={elapsed:.0f}s  ETA~{eta_s:.0f}s  "
                  f"ok={reasons['ok']} insufficient_swaps={reasons['insufficient_swaps']} "
                  f"insufficient_bars={reasons['insufficient_bars']} "
                  f"no_usable_labels={reasons['no_usable_labels']}  "
                  f"[query={timing['query_s']:.0f}s ({q_pct:.0f}%) "
                  f"compute={timing['compute_s']:.0f}s]", flush=True)
            last_print = now
    return rows, np.array(ys, dtype=float), np.array(groups), reasons, used_mints


# ---------------------------------------------------------------------------
# D114 (docs/DECISIONS.md): parallel build() across OS processes. `build_one`
# is fully independent per mint (no shared mutable state), which is what
# makes this safe -- unlike scripts/paper_trade_replay.py's replay loop,
# whose OnlinePolicy MUST update sequentially in chronological order (D84)
# and so is NOT a candidate for this same treatment.
#
# `_init_worker`/`_worker_build_one` must be plain MODULE-LEVEL functions
# (not closures/nested defs) -- multiprocessing on Windows uses the `spawn`
# start method, which re-imports this module in each child process and
# pickles the worker function BY NAME, not by value; a function defined
# inside build_parallel() wouldn't be importable that way and would fail to
# pickle at all. This module's existing `if __name__ == "__main__":` guard
# (already present, needed for any multiprocessing use on Windows) is what
# keeps that re-import from re-running main() in every child.
# ---------------------------------------------------------------------------

_worker_store: Optional[Store] = None
_worker_cfg: Optional[BarrierConfig] = None
_worker_bar_fraction: float = 0.01
_worker_stride: int = 5
_worker_non_overlap: bool = False


def _init_worker(data_dir: str, cfg: BarrierConfig, bar_fraction: float,
                 stride: int, non_overlap: bool) -> None:
    """Runs once per worker process (ProcessPoolExecutor's `initializer`) --
    opens ONE read-only Store/DuckDB connection per worker, reused for every
    mint that worker processes, rather than reconnecting per mint."""
    global _worker_store, _worker_cfg, _worker_bar_fraction, _worker_stride, _worker_non_overlap
    _worker_store = Store(data_dir, read_only=True)
    _worker_cfg = cfg
    _worker_bar_fraction = bar_fraction
    _worker_stride = stride
    _worker_non_overlap = non_overlap


def _worker_build_one(mint: str) -> Tuple[str, List[Dict[str, Optional[float]]], List[float], float, float]:
    """The per-task function submitted to the pool. Returns build_one()'s
    normal 3-tuple PLUS (query_s, compute_s) -- a shared `timing` dict (the
    pattern build()/build_one() use sequentially) can't cross process
    boundaries, so each task reports its own timing and build_parallel()
    sums them after the fact."""
    timing: Dict[str, float] = {}
    status, rows, ys = build_one(_worker_store, mint, _worker_cfg, _worker_bar_fraction,
                                 _worker_stride, _worker_non_overlap, timing=timing)
    return status, rows, ys, timing.get("query_s", 0.0), timing.get("compute_s", 0.0)


def build_parallel(data_dir: str, mints: Sequence[str], cfg: BarrierConfig, bar_fraction: float,
                   stride: int, non_overlap: bool, workers: int, progress_every_s: float = 5.0
                   ) -> Tuple[List[Dict], np.ndarray, np.ndarray, Counter, List[str]]:
    """Same return contract as build(). Results come back via
    `Executor.map()`, which yields in INPUT order regardless of which worker
    finishes which task when or in what order -- load-bearing, not cosmetic:
    `used_mints` must stay chronologically sorted (it's a filtered copy of
    `limited_mints`, itself chronological from chronological_universe()),
    because main()'s screen/validation/final_test split assumes that order
    IS the chronological split. Reordering by completion time would silently
    turn the split into noise -- exactly the kind of leak this project's
    whole 3-way-split discipline exists to prevent (D18)."""
    import concurrent.futures

    rows, ys, groups = [], [], []
    reasons: Counter = Counter()
    used_mints: List[str] = []
    total = len(mints)
    t0 = time.time()
    last_print = t0
    timing = {"query_s": 0.0, "compute_s": 0.0}

    # Small chunksize: individual mints vary hugely in cost (a handful of
    # swaps vs tens of thousands), so batching too many mints into one
    # IPC round-trip risks one worker getting stuck with a slow run of
    # expensive mints while others sit idle. Not tuned beyond "small and
    # reasonable" -- revisit with real evidence if workers finish unevenly.
    chunksize = 4

    with concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, initializer=_init_worker,
            initargs=(data_dir, cfg, bar_fraction, stride, non_overlap)) as ex:
        results = ex.map(_worker_build_one, mints, chunksize=chunksize)
        for i, (mint, (status, r, y, q_s, c_s)) in enumerate(zip(mints, results), start=1):
            reasons[status] += 1
            timing["query_s"] += q_s
            timing["compute_s"] += c_s
            if status == "ok":
                used_mints.append(mint)
                rows.extend(r)
                ys.extend(y)
                groups.extend([mint] * len(r))
            now = time.time()
            if now - last_print >= progress_every_s or i == total:
                elapsed = now - t0
                rate = i / elapsed if elapsed > 0 else 0.0
                eta_s = (total - i) / rate if rate > 0 else float("nan")
                print(f"  [build:parallel x{workers}] {i}/{total} mints ({i / total:.1%})  "
                      f"elapsed={elapsed:.0f}s  ETA~{eta_s:.0f}s  "
                      f"ok={reasons['ok']} insufficient_swaps={reasons['insufficient_swaps']} "
                      f"insufficient_bars={reasons['insufficient_bars']} "
                      f"no_usable_labels={reasons['no_usable_labels']}  "
                      f"[summed across workers -- cpu-time query={timing['query_s']:.0f}s "
                      f"compute={timing['compute_s']:.0f}s]", flush=True)
                last_print = now
    return rows, np.array(ys, dtype=float), np.array(groups), reasons, used_mints


# ---------------------------------------------------------------------------
# Chronological, token-level, 3-way split. The final test set is used ONCE,
# at the very end, and never to pick a feature -- that is the whole point of
# having three stages instead of two.
# ---------------------------------------------------------------------------

def split_tokens(mints_sorted: Sequence[str], screen_frac: float, val_frac: float,
                 test_frac: float) -> Tuple[List[str], List[str], List[str]]:
    total = screen_frac + val_frac + test_frac
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"--screen-frac + --val-frac + --test-frac must sum to 1.0, got {total}")
    n = len(mints_sorted)
    n_screen = int(round(n * screen_frac))
    n_val = int(round(n * val_frac))
    screen = list(mints_sorted[:n_screen])
    val = list(mints_sorted[n_screen:n_screen + n_val])
    test = list(mints_sorted[n_screen + n_val:])
    return screen, val, test


# ---------------------------------------------------------------------------
# Multiple testing: a token-level permutation null for max|AUC-0.5| over ALL
# tested features, built ONLY on the screening set.
# ---------------------------------------------------------------------------

def permutation_null_max_edge(rows: List[Dict], y: np.ndarray, groups: np.ndarray,
                              names: Sequence[str], n_perm: int, seed: int) -> np.ndarray:
    """Under the null "labels carry no information about which token produced
    these features", how big would the best of `len(names)` feature edges get
    by chance, given THIS run's own token sizes and count?

    Each replicate reassigns whole tokens' label sequences to other tokens
    (drawn without replacement, i.e. a permutation of token identity) rather
    than shuffling rows: shuffling rows would destroy the very within-token
    serial correlation that makes overlapping labels non-independent in the
    first place, understating how often a spurious max can occur. A donor
    token's label sequence is tiled/truncated to the recipient token's row
    count so every row still gets a label; feature values never move.
    """
    rng = np.random.default_rng(seed)
    uniq = np.array(sorted(set(groups.tolist())))
    idx_by_token = {g: np.flatnonzero(groups == g) for g in uniq}
    y_by_token = {g: y[idx_by_token[g]] for g in uniq}
    cols = {name: np.array([_f(r.get(name)) for r in rows]) for name in names}

    # D110 (docs/DECISIONS.md): cost here is n_perm * len(names) AUC passes
    # over the screen set -- on a wide, now-much-larger corpus (pumpfundata
    # merge) this is no longer the instant loop it was on the old 61-65-token
    # corpus. Same wall-clock-throttled progress pattern as build() above,
    # so a long permutation run doesn't look indistinguishable from a hang
    # either.
    progress_every_s = 5.0
    t0 = time.time()
    last_print = t0
    null_max = np.empty(n_perm)
    for p in range(n_perm):
        donors = rng.permutation(uniq)
        y_perm = np.empty(len(y), dtype=float)
        for recipient, donor in zip(uniq, donors):
            idx = idx_by_token[recipient]
            dy = y_by_token[donor]
            reps = int(np.ceil(len(idx) / len(dy))) if len(dy) else 1
            y_perm[idx] = np.tile(dy, max(reps, 1))[: len(idx)] if len(dy) else np.nan
        best = 0.0
        for name in names:
            a = auc(cols[name], y_perm)
            if not np.isnan(a):
                best = max(best, abs(a - 0.5))
        null_max[p] = best
        now = time.time()
        if now - last_print >= progress_every_s or p + 1 == n_perm:
            elapsed = now - t0
            rate = (p + 1) / elapsed if elapsed > 0 else 0.0
            eta_s = (n_perm - (p + 1)) / rate if rate > 0 else float("nan")
            print(f"  [permutation null] {p + 1}/{n_perm} replicates "
                  f"({(p + 1) / n_perm:.1%})  elapsed={elapsed:.0f}s  "
                  f"ETA~{eta_s:.0f}s", flush=True)
            last_print = now
    return null_max


def feature_table(rows: List[Dict], y: np.ndarray, groups: np.ndarray, mask: np.ndarray,
                  names: Sequence[str]) -> List[Tuple[str, float, float, str, int]]:
    """(name, auc, edge, direction, n) on the rows selected by `mask`."""
    out = []
    for name in names:
        col = np.array([_f(r.get(name)) for r in rows])
        a = auc(col[mask], y[mask])
        if np.isnan(a):
            continue
        n = int((~np.isnan(col[mask])).sum())
        out.append((name, a, abs(a - 0.5), direction_of(a), n))
    out.sort(key=lambda r: r[2], reverse=True)
    return out


def family_table(results: List[Tuple[str, float, float, str, int]]
                 ) -> List[Tuple[str, str, float, float, str, int]]:
    """Best-in-family per family, from already-computed individual results.

    Best-in-family (not an averaged family AUC) because averaging AUCs across
    features that disagree in direction is not a meaningful statistic, and a
    fabricated 1-D aggregate would require fitting something -- exactly what
    Stage 1 must not do. This is still a MAX over members and so is still
    subject to multiple-testing inflation; that is corrected for at the
    all-feature level by the permutation null below, not re-derived per
    family.
    """
    by_family: Dict[str, List[Tuple[str, float, float, str, int]]] = defaultdict(list)
    for name, a, edge, d, n in results:
        by_family[family_of(name)].append((name, a, edge, d, n))
    out = []
    for fam, members in by_family.items():
        best = max(members, key=lambda r: r[2])
        out.append((fam, best[0], best[1], best[2], best[3], len(members)))
    out.sort(key=lambda r: r[3], reverse=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--limit", type=int, default=2000,
                     help="cap on the ELIGIBLE, chronologically-sorted universe "
                          "(earliest N tokens kept) -- reported explicitly, never silent")
    ap.add_argument("--workers", type=int, default=1,
                     help="D114 (docs/DECISIONS.md): run build() across this many OS "
                          "processes instead of one, each with its own read-only "
                          "Store/DuckDB connection -- build_one() is independent per "
                          "mint, so this parallelizes cleanly (default 1 = sequential, "
                          "unchanged behavior). Leave at least 1 core free for the OS; "
                          "on a 6-core machine, try 5. NOT applied to "
                          "scripts/paper_trade_replay.py -- its online policy updates "
                          "sequentially by design (D84) and cannot be parallelized the "
                          "same way without a bigger redesign.")
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--upper", type=float, default=1.6)
    ap.add_argument("--lower", type=float, default=-0.30)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--bar-fraction", type=float, default=0.01)
    ap.add_argument("--screen-frac", type=float, default=0.5)
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--n-boot", type=int, default=2000, help="AUC cluster-bootstrap replicates")
    ap.add_argument("--n-perm", type=int, default=200, help="permutation-null replicates")
    ap.add_argument("--non-overlap", action="store_true",
                     help="thin each token's labels to a temporally-disjoint subset before "
                          "evaluation, instead of relying only on token-level clustering")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--real-creation-file", default=None,
                     help="path to the cache scripts/fetch_real_creation_times.py writes "
                          "(default: <--data>/real_creation_times.json if it exists)")
    ap.add_argument("--max-observed-age-hours", type=float, default=6.0,
                     help="exclude a token if it was already older than this, in real "
                          "creation time, at the instant we first observed it trading -- "
                          "guards against D55's new-launch/old-survivor mixing confound")
    ap.add_argument("--no-age-filter", action="store_true",
                     help="disable the real-creation-time filter even if the cache exists "
                          "(for comparing runs with/without it)")
    args = ap.parse_args()

    # D110 (docs/DECISIONS.md): print immediately, before anything that could
    # block -- a real run went 5+ minutes with zero output (the first Store
    # query, inside chronological_universe(), both opens the DuckDB
    # connection/registers views over the whole data/swaps/ corpus AND runs a
    # full GROUP BY scan, with no way to report partial progress mid-query),
    # and from the terminal that is indistinguishable from a hang. This line
    # at least confirms the process is alive and what it's about to do before
    # the first potentially-long silent stretch.
    print(f"[info-audit] data={args.data!r}  since={args.since!r}  until={args.until!r}  "
          f"limit={args.limit}  -- starting (next step scans the whole Store once; "
          f"see docs/DECISIONS.md D110 if this looks stuck for minutes)", flush=True)

    # D114 (docs/DECISIONS.md): read_only=True -- this script never writes to
    # the Store (confirmed: no write_swaps/write_bars call anywhere in this
    # file). Doing this unconditionally (not just when --workers > 1) is what
    # lets --workers > 1 work at all: DuckDB allows many concurrent
    # READ-ONLY connections to the same file, but not a read-only connection
    # alongside a read-write one, and the old default (read-write) would
    # have blocked every worker process from opening its own connection.
    store = Store(args.data, read_only=True)

    # -- Universe --------------------------------------------------------
    universe = chronological_universe(store, args.since, args.until)
    requested = len(universe)
    eligible_by_swaps = [(m, ts) for m, ts, n in universe if n >= MIN_SWAPS_PER_TOKEN]
    excluded_by_swaps = requested - len(eligible_by_swaps)

    real_creation_path = Path(args.real_creation_file) if args.real_creation_file \
        else Path(args.data) / "real_creation_times.json"
    cache = None
    if not args.no_age_filter and real_creation_path.exists():
        try:
            cache = load_real_creation_cache(real_creation_path)
        except Exception as e:
            print(f"  (could not read {real_creation_path}: {e} -- age filter disabled)")

    excluded_too_old = 0
    excluded_no_real_creation = 0
    if cache is not None:
        eligible, excluded_too_old, excluded_no_real_creation = filter_by_real_creation(
            eligible_by_swaps, cache, args.max_observed_age_hours)
    else:
        eligible = eligible_by_swaps

    limited = eligible[: args.limit]
    excluded_by_limit = len(eligible) - len(limited)
    limited_mints = [m for m, _ in limited]

    print("=" * 78)
    print("UNIVERSE")
    print("=" * 78)
    print(f"  requested (any swap, in [{args.since}, {args.until}]): {requested}")
    print(f"  excluded  (< {MIN_SWAPS_PER_TOKEN} swaps):                {excluded_by_swaps}")
    print(f"  eligible-by-swaps (>= {MIN_SWAPS_PER_TOKEN} swaps):        {len(eligible_by_swaps)}")
    if cache is not None:
        print(f"  excluded  (already >{args.max_observed_age_hours:.1f}h old, real creation "
              f"time, when first observed -- D55): {excluded_too_old}")
        print(f"  excluded  (no real-creation-time data in cache):        {excluded_no_real_creation}")
        print(f"  eligible-after-age-filter:                    {len(eligible)}")
    else:
        print(f"  NOTE: age filter DISABLED -- no real-creation-time cache at "
              f"{real_creation_path} (or --no-age-filter set).")
        print(f"        Universe may mix genuinely new launches with old survivor tokens")
        print(f"        that merely traded again recently (D55, docs/DECISIONS.md).")
        print(f"        Run `python scripts/fetch_real_creation_times.py --data {args.data}` "
              f"to enable it.")
    print(f"  excluded  (beyond --limit {args.limit}, earliest kept):   {excluded_by_limit}")
    print(f"  considered for build:                        {len(limited_mints)}")

    if len(limited_mints) < 30:
        print(f"\nstatus=collecting_data  eligible_mints={len(limited_mints)}  (need >= 30)")
        return 2

    cfg = BarrierConfig(args.upper, args.lower, int(args.horizon_min * 60_000))
    if args.workers > 1:
        print(f"  running build() across {args.workers} worker process(es) "
              f"(D114, docs/DECISIONS.md)...", flush=True)
        rows, y, groups, reasons, used_mints = build_parallel(
            args.data, limited_mints, cfg, args.bar_fraction, args.stride,
            args.non_overlap, args.workers)
    else:
        rows, y, groups, reasons, used_mints = build(
            store, limited_mints, cfg, args.bar_fraction, args.stride, args.non_overlap)
    print(f"  excluded  (< {MIN_BARS_PER_TOKEN} bars):                  {reasons['insufficient_bars']}")
    print(f"  excluded  (no usable, non-truncated label):  {reasons['no_usable_labels']}")
    print(f"  used                                         {len(used_mints)}")
    print(f"  rows (bar-labels, pre-split)                 {len(y)}")

    if len(y) < 500:
        print(f"\nstatus=collecting_data  usable_rows={len(y)}")
        return 2

    # -- Chronological 3-way split, by TOKEN -----------------------------
    # `used_mints` preserves the order `limited_mints` was iterated in, which
    # is itself chronological (from chronological_universe) -- so this list
    # is already sorted by first-seen time; no re-sort needed or trusted.
    screen_mints, val_mints, test_mints = split_tokens(
        used_mints, args.screen_frac, args.val_frac, args.test_frac)
    screen_set, val_set, test_set = set(screen_mints), set(val_mints), set(test_mints)
    is_screen = np.array([g in screen_set for g in groups])
    is_val = np.array([g in val_set for g in groups])
    is_test = np.array([g in test_set for g in groups])

    ts_by_mint = {m: ts for m, ts in eligible}

    def _bounds(mints):
        if not mints:
            return None, None
        tss = [ts_by_mint[m] for m in mints]
        return min(tss), max(tss)

    print("\n" + "=" * 78)
    print("TIME  (chronological split by TOKEN -- never by row, never randomly)")
    print("=" * 78)
    for label, mints, mask in (("screen", screen_mints, is_screen),
                               ("validation", val_mints, is_val),
                               ("final_test", test_mints, is_test)):
        lo, hi = _bounds(mints)
        print(f"  {label:11s} tokens={len(mints):5d}  rows={int(mask.sum()):6d}  "
              f"first_ts=[{lo}, {hi}]")
    if args.val_frac == 0:
        print("  NOTE: --val-frac=0 -- this is a 2-stage screen/final_test design.")
        print("        The best feature's edge is never independently replicated before")
        print("        the one-shot final evaluation below; only the permutation null and")
        print("        the final held-out CI guard against a false positive here.")
    if not test_mints or not screen_mints:
        print("\nstatus=collecting_data  not enough tokens to fill every split (need more history)")
        return 2

    names = sorted({k for r in rows for k in r})

    print("\n" + "=" * 78)
    print("LABELS")
    print("=" * 78)
    print(f"  horizon={args.horizon_min}min  upper_multiple={args.upper}  lower_pct={args.lower}  "
          f"stride={args.stride}  non_overlap={args.non_overlap}")
    print(f"  screen base_rate={y[is_screen].mean():.4f}   "
          f"validation base_rate={(y[is_val].mean() if is_val.any() else float('nan')):.4f}   "
          f"final_test base_rate={y[is_test].mean():.4f}")

    # -- Features: individual + family, on the SCREEN set ONLY -----------
    print("\n" + "=" * 78)
    print(f"FEATURES  (n={len(names)}, evaluated on the SCREEN set only -- this table")
    print("          NEVER sees validation or final_test rows)")
    print("=" * 78)
    screen_results = feature_table(rows, y, groups, is_screen, names)
    print(f"{'feature':38s} {'AUC':>8s} {'edge':>8s} {'direction':>10s} {'n':>8s}")
    print("-" * 78)
    for name, a, edge, d, n in screen_results[: args.top]:
        print(f"{name:38s} {a:8.4f} {edge:8.4f} {d:>10s} {n:8d}")

    print("\nFEATURE FAMILIES (best-in-family, screen set; window-length suffixes "
          "{3,10,30} collapsed -- see family_of())")
    fam_results = family_table(screen_results)
    print(f"{'family':30s} {'best member':38s} {'AUC':>8s} {'edge':>8s} {'dir':>10s} {'#in fam':>8s}")
    print("-" * 100)
    for fam, best_name, a, edge, d, k in fam_results[: args.top]:
        print(f"{fam:30s} {best_name:38s} {a:8.4f} {edge:8.4f} {d:>10s} {k:8d}")

    if not screen_results:
        print("\nVERDICT: no_signal -- no feature produced a finite AUC on the screen set.")
        return 1

    best_name, best_auc, best_edge, best_dir, best_n = screen_results[0]

    # -- Multiple-testing correction: permutation null on the SCREEN set --
    null = permutation_null_max_edge(
        [r for r, s in zip(rows, is_screen) if s],
        y[is_screen], groups[is_screen], names, args.n_perm, args.seed)
    p_value = float((1 + (null >= best_edge).sum()) / (len(null) + 1))

    print("\n" + "=" * 78)
    print("MULTIPLE-TESTING NULL")
    print("=" * 78)
    print(f"  {len(names)} features tested on screen; permutation replicates={args.n_perm}")
    print(f"  null max|AUC-0.5|: mean={null.mean():.4f}  p95={np.percentile(null, 95):.4f}  "
          f"p99={np.percentile(null, 99):.4f}")
    print(f"  observed best (screen): {best_name}  edge={best_edge:.4f}")
    print(f"  permutation p-value: {p_value:.4f}  "
          f"({'below' if p_value < 0.05 else 'NOT below'} 0.05)")

    # -- Validation replication check (does NOT re-select) ----------------
    val_confirms = None
    if val_mints:
        col = np.array([_f(r.get(best_name)) for r in rows])
        a_val = auc(col[is_val], y[is_val])
        val_confirms = (not np.isnan(a_val)) and (direction_of(a_val) == best_dir) and abs(a_val - 0.5) > 0.0
        print("\n" + "=" * 78)
        print("VALIDATION (replication check of the SAME pre-specified feature; never re-selects)")
        print("=" * 78)
        print(f"  {best_name}  AUC(validation)={a_val:.4f}  direction={direction_of(a_val)}  "
              f"(screen direction was {best_dir})")

    # -- The one-shot final evaluation ------------------------------------
    print("\n" + "=" * 78)
    print("BEST PRE-SPECIFIED FEATURE")
    print("=" * 78)
    print(f"  {best_name}  (family: {family_of(best_name)})")
    print(f"  chosen on the SCREEN set only, before final_test was read")

    col = np.array([_f(r.get(best_name)) for r in rows])
    a_final = auc(col[is_test], y[is_test])
    n_test_obs = int(is_test.sum())
    print(f"\n  [bootstrap CI] running {args.n_boot} token-cluster bootstrap replicates "
          f"on {n_test_obs} final_test row(s)...", flush=True)
    _t_boot = time.time()
    lo, hi = auc_cluster_bootstrap(col[is_test], y[is_test], groups[is_test], auc, n_boot=args.n_boot, seed=args.seed)
    print(f"  [bootstrap CI] done in {time.time() - _t_boot:.1f}s", flush=True)
    n_test_tokens = len(test_mints)

    print("\n" + "=" * 78)
    print("FINAL HELD-OUT EVALUATION (final_test, touched once, never used to select)")
    print("=" * 78)
    print(f"  MAX EDGE (final_test): AUC={a_final:.4f}  edge={abs(a_final - 0.5):.4f}  "
          f"direction={direction_of(a_final)}")
    print(f"  95% CI (token-cluster bootstrap, {args.n_boot} reps): [{lo:.4f}, {hi:.4f}]")
    print(f"  test tokens={n_test_tokens}  test observations={n_test_obs}")
    ci_excludes_half = (not np.isnan(lo)) and (not np.isnan(hi)) and (lo > 0.5 or hi < 0.5)
    print(f"  CI excludes 0.5: {ci_excludes_half}")

    print("\n" + "=" * 78)
    signal = (p_value < 0.05) and ci_excludes_half
    if signal:
        print("VERDICT: signal_present -- GATE PASSED.")
        print("  At least one pre-specified feature shows a statistically detectable")
        print("  out-of-sample association with the defined future barrier outcome:")
        print("  its screen-set edge clears the run's own multiple-testing null (p<0.05),")
        print("  AND its final_test AUC's 95% CI excludes 0.5 on tokens that played no")
        print("  role in choosing it.")
        print("  This is NOT a claim that trading this feature is profitable -- costs,")
        print("  slippage, sizing and execution are entirely untested here.")
    else:
        print("VERDICT: no_edge_found -- GATE FAILED.")
        print(f"  permutation p-value={p_value:.4f} (want <0.05), "
              f"final_test CI excludes 0.5: {ci_excludes_half}.")
        print("  Try a different HORIZON or a different UNIVERSE (deeper band, longer")
        print("  tapes, more tokens). Do NOT reach for a bigger model: if nothing")
        print("  predicts maximum favourable excursion under a clean split, nothing")
        print("  will predict profit either.")
    return 0 if signal else 1


if __name__ == "__main__":
    raise SystemExit(main())
