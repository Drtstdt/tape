#!/usr/bin/env python3
"""explore_facts_v1 -- measure the corpus BEFORE any exploration (D119).

Read-only. Reads the D118 mint-clustered cache (E:\\tape_cache\\swaps_by_mint)
and data/real_creation_times.json; writes only to --out (default
E:\\tape_research\\facts_v1).

Two passes over the 64 cache buckets:

  Pass A (no outcomes, whole corpus):
    * per source: rows (deduped), mints, min/max ts
    * rows per UTC hour -> which hours were actually COLLECTED. pumpfundata
      was bought as ~12 hourly files/day spread evenly over the clock
      (scripts/pumpfundata_fetch_plan.py::build_schedule -> even UTC hours),
      so the tape of every token has 1-hour holes. Measured here, not assumed.
    * per mint: first/last ts, raw and deduped swap counts, sources
    * mints per day: all / >=50 deduped swaps / real-creation age filter

  Pass B (labels -- ONLY tokens first seen before --label-until, a period
  that lies inside the future discovery zone under any 50/20/30 split; the
  later period is not looked at with labels here):
    * same bars/labels as info_audit_v2.build_one (dollar bars, 1% of first
      depth; triple barrier +60%/-30%/30min; stride 5)
    * every label classified OUTCOME-INDEPENDENTLY by data coverage:
        CENS  = label window [t0, t0+H] overlaps an hour that was not
                collected (or lies past the corpus end) -- censored by the
                COLLECTION schedule, exogenous to the token
        DEAD  = window fully collected, but the tape has no bar after the
                deadline: the token stopped trading -> a real outcome
                (today these are dropped as `truncated`)
        UP / DOWN / TIMEOUT = window fully collected, resolved normally
    * one-decision-per-token variants: decide at the K-th bar (K=5,10,20),
      for ALL tokens that reach K bars (no look-ahead) vs only those that
      also pass today's whole-tape filter (>=50 swaps & >=20 bars)
    * diagnostic: AUC of age_ms (and of minute-within-hour) under today's
      semantics vs on fully-collected windows only -- does the "younger is
      better" signal survive removing collection-gap artefacts?

    python scripts\\explore_facts_v1.py --workers 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.bars import BarBuilder, band_bar_threshold  # noqa: E402
from tape.labels import BarrierConfig, label_series, triple_barrier, UP, DOWN, TIMEOUT  # noqa: E402
from tape.schema import CanonicalSwap  # noqa: E402
from tape import swaps_cache as sc  # noqa: E402

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
MIN_SWAPS_PER_TOKEN = 50   # info_audit_v2 (whole-tape, look-ahead)
MIN_BARS_PER_TOKEN = 20    # info_audit_v2 (whole-tape, look-ahead)
DECISION_KS = (5, 10, 20)

CLS_UP, CLS_DOWN, CLS_TIMEOUT, CLS_DEAD, CLS_CENS = "UP", "DOWN", "TIMEOUT", "DEAD", "CENS"
CLASSES = (CLS_UP, CLS_DOWN, CLS_TIMEOUT, CLS_DEAD, CLS_CENS)
OLD_NAMES = {UP: "UP", DOWN: "DOWN", TIMEOUT: "TIMEOUT"}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_explore_facts.py)
# ---------------------------------------------------------------------------

def hour_of(ts_ms: int) -> int:
    return int(ts_ms) // HOUR_MS


def observed_hours_from_counts(counts: Dict[int, int], min_frac_of_median: float = 0.05,
                               min_rows: int = 1000) -> Tuple[Set[int], int]:
    """An hour counts as COLLECTED if its row count is at least
    max(min_rows, min_frac_of_median * median of the busier half of hours).
    The busier-half median is the typical collected hour even if half the
    hours are empty; boundary jitter (D99: a few hundred rows a few seconds
    outside a file's hour) stays far below it. Returns (hours, threshold)."""
    vals = sorted(v for v in counts.values() if v > 0)
    if not vals:
        return set(), min_rows
    busy = vals[len(vals) // 2:]
    med = busy[len(busy) // 2]
    thr = max(min_rows, int(min_frac_of_median * med))
    return {h for h, v in counts.items() if v >= thr}, thr


def window_fully_observed(t0_ms: int, t1_ms: int, observed: Set[int]) -> bool:
    """True iff every UTC hour touched by [t0, t1] was collected."""
    for h in range(hour_of(t0_ms), hour_of(t1_ms) + 1):
        if h not in observed:
            return False
    return True


def classify_label(outcome: int, truncated: bool, t0_ms: int, horizon_ms: int,
                   observed: Set[int]) -> str:
    """Outcome-INDEPENDENT censoring: whether a label is CENS depends only on
    t0, the horizon and the collection schedule -- never on what the price
    did. (Keeping labels that happened to resolve before a gap would keep
    fast resolutions and drop slow ones: selection on outcome.)"""
    if not window_fully_observed(t0_ms, t0_ms + horizon_ms, observed):
        return CLS_CENS
    if truncated:
        return CLS_DEAD
    return {UP: CLS_UP, DOWN: CLS_DOWN, TIMEOUT: CLS_TIMEOUT}[outcome]


def minutes_to_coverage_end(t_ms: int, observed: Set[int]) -> float:
    """Minutes from t to the end of the contiguous collected run containing t
    (0 if t's own hour is not collected)."""
    h = hour_of(t_ms)
    if h not in observed:
        return 0.0
    while h + 1 in observed:
        h += 1
    return ((h + 1) * HOUR_MS - t_ms) / 60_000.0


def auc(scores, y) -> float:
    """Rank AUC, ties averaged (same as info_audit_v2.auc)."""
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y, dtype=float)
    m = ~np.isnan(scores)
    s, yy = scores[m], y[m]
    n_pos = int(yy.sum())
    n_neg = int(len(yy) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    _, inv, counts = np.unique(s, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts), dtype=float)
    np.add.at(sums, inv, ranks)
    ranks = (sums / counts)[inv]
    return float((ranks[yy == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def process_token(mint: str, swaps: Sequence[CanonicalSwap], cfg: BarrierConfig,
                  observed: Set[int], bar_fraction: float = 0.01, stride: int = 5):
    """Bars + labels exactly as info_audit_v2.build_one, plus coverage
    classification. Returns (token_record: dict, label_rows: list[tuple])."""
    n_sw = len(swaps)
    rec = {"mint": mint, "n_swaps": n_sw, "n_bars": 0, "first_swap_ts": None,
           "passes_lookahead_filter": False}
    for k in DECISION_KS:
        rec[f"k{k}_cls"] = None
        rec[f"k{k}_old"] = None
        rec[f"k{k}_trunc"] = None
        rec[f"k{k}_t0"] = None
        rec[f"k{k}_ret_last"] = None
        rec[f"k{k}_qres"] = None
    if n_sw == 0:
        return rec, []
    rec["first_swap_ts"] = int(swaps[0].ts_ms)
    depth = next((s.quote_reserve_after for s in swaps if s.quote_reserve_after), None) or 1.0
    bb = BarBuilder("dollar", threshold=band_bar_threshold(depth, bar_fraction))
    h, l, c, t, q = [], [], [], [], []
    for s in swaps:
        bar = bb.push(s)
        if bar is None:
            continue
        h.append(bar.high)
        l.append(bar.low)
        c.append(bar.close)
        t.append(bar.close_ts_ms)
        q.append(bar.quote_reserve_close)
    nb = len(c)
    rec["n_bars"] = nb
    rec["passes_lookahead_filter"] = bool(n_sw >= MIN_SWAPS_PER_TOKEN and nb >= MIN_BARS_PER_TOKEN)
    if nb < 2:
        return rec, []

    first_ts = rec["first_swap_ts"]
    rows = []
    for lab in label_series(h, l, c, t, cfg, mint=mint, stride=stride):
        cls = classify_label(lab.outcome, lab.truncated, lab.t0_ms, cfg.horizon_ms, observed)
        ret_last = c[-1] / c[lab.bar_index] - 1.0 if c[lab.bar_index] > 0 else float("nan")
        rows.append((
            lab.bar_index,
            lab.t0_ms,
            lab.t0_ms - first_ts,                         # == TokenState.age_ms
            ((lab.t0_ms % HOUR_MS) / 60_000.0),           # minute within UTC hour
            minutes_to_coverage_end(lab.t0_ms, observed),
            lab.outcome,
            bool(lab.truncated),
            cls,
            ret_last,
        ))

    for k in DECISION_KS:
        i = k - 1
        if i < nb:   # reaching K bars is all that is known at decision time
            lab = triple_barrier(h, l, c, t, i, cfg, mint)
            rec[f"k{k}_cls"] = classify_label(lab.outcome, lab.truncated, lab.t0_ms,
                                              cfg.horizon_ms, observed)
            rec[f"k{k}_old"] = OLD_NAMES[lab.outcome]
            rec[f"k{k}_trunc"] = bool(lab.truncated)
            rec[f"k{k}_t0"] = int(lab.t0_ms)
            rec[f"k{k}_ret_last"] = (c[-1] / c[i] - 1.0) if c[i] > 0 else None
            rec[f"k{k}_qres"] = q[i]
    return rec, rows


# ---------------------------------------------------------------------------
# Workers (module level: Windows spawn)
# ---------------------------------------------------------------------------

_W: Dict = {}


def _init_worker(cache_dir: str, memory_limit: str, temp_dir: str,
                 observed: Optional[List[int]], cfg_tuple, stride: int, bar_fraction: float):
    _W["cache_dir"] = cache_dir
    _W["con"] = sc.open_cache_connection(memory_limit=memory_limit, threads=1, temp_dir=temp_dir)
    _W["observed"] = set(observed or [])
    _W["cfg"] = BarrierConfig(*cfg_tuple) if cfg_tuple else None
    _W["stride"] = stride
    _W["bar_fraction"] = bar_fraction


def _bucket_sql(b: int) -> str:
    return f"read_parquet({sc._q(sc.bucket_file(_W['cache_dir'], b).as_posix())})"


def _dedup_sql(src: str, where: str = "") -> str:
    return (f"SELECT * EXCLUDE (rn) FROM (SELECT *, {sc._DEDUP_WINDOW} AS rn FROM {src} {where}) "
            f"WHERE rn = 1")


def pass_a_bucket(b: int):
    con = _W["con"]
    t0 = time.time()
    d = _dedup_sql(_bucket_sql(b))
    con.execute(f"CREATE OR REPLACE TEMP TABLE d AS {d}")
    src = con.execute("SELECT source, count(*), count(DISTINCT mint), min(ts_ms), max(ts_ms) "
                      "FROM d GROUP BY source").fetchall()
    hours = con.execute(f"SELECT source, ts_ms // {HOUR_MS} AS h, count(*) FROM d "
                        "GROUP BY 1, 2").fetchall()
    raw = con.execute(f"SELECT mint, count(*) AS n_raw FROM {_bucket_sql(b)} GROUP BY mint").df()
    mt = con.execute("SELECT mint, min(ts_ms) AS first_ts, max(ts_ms) AS last_ts, "
                     "count(*) AS n_dedup, array_to_string(list_sort(list_distinct(list(source))), '+') AS sources "
                     "FROM d GROUP BY mint").df()
    con.execute("DROP TABLE d")
    mt = mt.merge(raw, on="mint", how="left")
    return b, src, hours, mt, time.time() - t0


def pass_b_bucket(args):
    b, mints = args
    import pandas as pd
    con = _W["con"]
    t0 = time.time()
    con.register("want_df", pd.DataFrame({"mint": list(mints)}))
    df = con.execute(
        _dedup_sql(_bucket_sql(b), "WHERE mint IN (SELECT mint FROM want_df)")
        + " ORDER BY mint, ts_ms, slot, sig").df()
    con.unregister("want_df")
    t_q = time.time() - t0
    recs, labels = [], []
    if len(df):
        m_arr = df["mint"].to_numpy()
        cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
        starts = np.concatenate([[0], cuts])
        ends = np.concatenate([cuts, [len(df)]])
        for s_, e_ in zip(starts, ends):
            mint = m_arr[s_]
            swaps = sc.rows_to_swaps(df.iloc[s_:e_])
            rec, rows = process_token(mint, swaps, _W["cfg"], _W["observed"],
                                      _W["bar_fraction"], _W["stride"])
            recs.append(rec)
            for r in rows:
                labels.append((mint,) + r + (rec["passes_lookahead_filter"],))
    return b, recs, labels, t_q, time.time() - t0 - t_q


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _day(ts_ms) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _iso(ts_ms) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _run_pool(fn, items, workers, initargs, label, log):
    out = []
    t0 = time.time()
    last = 0.0
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=initargs) as ex:
        for i, r in enumerate(ex.map(fn, items), start=1):
            out.append(r)
            el = time.time() - t0
            if el - last >= 5 or i == len(items):
                last = el
                eta = el / i * (len(items) - i)
                log(f"[{label}] {i}/{len(items)} buckets  elapsed={el:.0f}s  ETA~{eta:.0f}s")
    return out


def _q(xs, ps=(0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)):
    import pandas as pd
    xs = pd.to_numeric(pd.Series(list(xs), dtype=object), errors="coerce").dropna().to_numpy(dtype=float)
    if len(xs) == 0:
        return {}
    return {f"p{int(p * 100):02d}": float(np.quantile(xs, p)) for p in ps} | {"n": int(len(xs))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--swaps-cache", default=r"E:\tape_cache\swaps_by_mint")
    ap.add_argument("--allow-stale-cache", action="store_true")
    ap.add_argument("--out", default=r"E:\tape_research\facts_v1")
    ap.add_argument("--temp-dir", default=r"E:\duckdb_tmp_facts")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--worker-memory", default="2GB")
    ap.add_argument("--label-until", default="2026-02-17",
                    help="labels only for tokens first seen BEFORE this UTC date (discovery-safe)")
    ap.add_argument("--max-observed-age-hours", type=float, default=6.0)
    ap.add_argument("--upper", type=float, default=1.6)
    ap.add_argument("--lower", type=float, default=-0.30)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--bar-fraction", type=float, default=0.01)
    ap.add_argument("--skip-labels", action="store_true")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log_f = open(out / "facts_log.txt", "w", encoding="utf-8")

    def log(msg=""):
        print(msg, flush=True)
        log_f.write(msg + "\n")
        log_f.flush()

    import pandas as pd

    log(f"explore_facts_v1 start {datetime.now(timezone.utc).isoformat()}  pid={os.getpid()}")
    log(f"  cache={a.swaps_cache}  out={out}  workers={a.workers}  label_until={a.label_until}")
    try:
        sc.check_cache_fresh(a.swaps_cache, a.data)
        log("  cache fingerprint matches data/swaps: OK")
    except RuntimeError as e:
        if not a.allow_stale_cache:
            log(f"  {e}")
            return 2
        log(f"  WARNING (allowed): {e}")
    sc.commit_preflight(a.workers * sc.parse_gb(a.worker_memory) + 4.0, "explore_facts_v1", log)

    cfg_tuple = (a.upper, a.lower, int(a.horizon_min * 60_000))   # same as info_audit_v2: --upper IS the multiple
    base_init = (a.swaps_cache, a.worker_memory, a.temp_dir)

    # ---------------- Pass A -------------------------------------------
    log("\n== PASS A: corpus facts (no outcomes) ==")
    res = _run_pool(pass_a_bucket, list(range(sc.N_BUCKETS)), a.workers,
                    base_init + (None, None, a.stride, a.bar_fraction), "passA", log)
    src_tot: Dict[str, List] = {}
    hour_src: Dict[str, Dict[int, int]] = {}
    mts = []
    for _, src, hours, mt, _el in res:
        for s, n, nm, lo, hi in src:
            v = src_tot.setdefault(s, [0, 0, lo, hi])
            v[0] += n
            v[1] += nm
            v[2] = min(v[2], lo)
            v[3] = max(v[3], hi)
        for s, hh, n in hours:
            d = hour_src.setdefault(s, {})
            d[int(hh)] = d.get(int(hh), 0) + int(n)
        mts.append(mt)
    mint_tab = pd.concat(mts, ignore_index=True)
    del mts, res

    facts: Dict = {"generated_utc": datetime.now(timezone.utc).isoformat(), "args": vars(a)}
    log("\n-- per source (deduped rows; mints exact: buckets partition mints) --")
    facts["sources"] = {}
    for s, (n, nm, lo, hi) in sorted(src_tot.items(), key=lambda kv: -kv[1][0]):
        log(f"  {s:12s} rows={n:>11,}  mints={nm:>8,}  {_iso(lo)} .. {_iso(hi)} UTC")
        facts["sources"][s] = {"rows": n, "mints": nm, "min_ts": lo, "max_ts": hi,
                               "min_utc": _iso(lo), "max_utc": _iso(hi)}
    multi = int((mint_tab["sources"].str.contains(r"\+")).sum())
    log(f"  mints seen by >1 source: {multi:,}")

    # coverage
    pf = hour_src.get("pumpfundata", {})
    observed, thr = observed_hours_from_counts(pf)
    log(f"\n-- collection coverage (pumpfundata rows per UTC hour) --")
    vals = np.array(sorted(pf.values()))
    log(f"  hours with any pumpfundata row: {len(vals)}; rows/hour quantiles: "
        f"{json.dumps({k: round(v) for k, v in _q(vals).items()})}")
    log(f"  threshold for 'collected': {thr:,} rows -> collected hours: {len(observed)}; "
        f"partial hours (0<rows<thr): {int(((vals > 0) & (vals < thr)).sum())}")
    days = sorted({h // 24 for h in pf})
    cov_map = {}
    for dd in days:
        row = "".join("#" if (dd * 24 + i) in observed else ("." if pf.get(dd * 24 + i, 0) == 0 else "~")
                      for i in range(24))
        cov_map[_day(dd * DAY_MS)] = row
    log("  per day (UTC hour 0..23; #=collected, ~=a few rows only, .=none):")
    for k_, v_ in cov_map.items():
        log(f"    {k_}  {v_}  ({v_.count('#')}h)")
    runs = []
    hs = sorted(observed)
    if hs:
        st = pr = hs[0]
        for h_ in hs[1:]:
            if h_ != pr + 1:
                runs.append(pr - st + 1)
                st = h_
            pr = h_
        runs.append(pr - st + 1)
    log(f"  contiguous collected runs (hours): {dict(sorted(pd.Series(runs).value_counts().items()))}")
    facts["coverage"] = {"threshold_rows": thr, "n_collected_hours": len(observed),
                         "per_day": cov_map, "run_lengths": [int(r) for r in runs]}
    for s in hour_src:
        if s != "pumpfundata":
            hh = hour_src[s]
            log(f"  {s}: rows in collected hours={sum(v for k, v in hh.items() if k in observed):,} "
                f"outside={sum(v for k, v in hh.items() if k not in observed):,}")
    (out / "collected_hours.json").write_text(json.dumps(sorted(observed)), encoding="utf-8")

    # mints per day + filters
    log("\n-- universe & filters --")
    cr_path = Path(a.data) / "real_creation_times.json"
    t0 = time.time()
    with open(cr_path, "r", encoding="utf-8") as f:
        cr = json.load(f)
    log(f"  real_creation_times.json: {len(cr):,} entries ({time.time() - t0:.0f}s)")
    real = []
    for m in mint_tab["mint"].to_numpy():
        r = cr.get(m)
        real.append(r.get("real_created_ts_ms") if (r and r.get("status") == "ok") else None)
    del cr
    mint_tab["real_created_ts"] = pd.array(real, dtype="Int64")
    gap_ms = mint_tab["first_ts"] - mint_tab["real_created_ts"]
    mint_tab["has_real"] = mint_tab["real_created_ts"].notna()
    mint_tab["age_ok"] = (gap_ms <= a.max_observed_age_hours * HOUR_MS).fillna(False).astype(bool)
    mint_tab["ge50_raw"] = mint_tab["n_raw"] >= MIN_SWAPS_PER_TOKEN
    mint_tab["ge50_dedup"] = mint_tab["n_dedup"] >= MIN_SWAPS_PER_TOKEN
    mint_tab["day"] = pd.to_datetime(mint_tab["first_ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    mint_tab["first_hour_collected"] = (mint_tab["first_ts"] // HOUR_MS).isin(observed)
    mint_tab["first_min_of_hour"] = (mint_tab["first_ts"] % HOUR_MS) / 60_000.0
    n_all = len(mint_tab)
    log(f"  mints total: {n_all:,}; with real creation time: {int(mint_tab['has_real'].sum()):,}; "
        f"age<= {a.max_observed_age_hours}h: {int(mint_tab['age_ok'].sum()):,}")
    log(f"  >=50 swaps raw: {int(mint_tab['ge50_raw'].sum()):,}  deduped: {int(mint_tab['ge50_dedup'].sum()):,}"
        f"  (raw-only passes: {int((mint_tab['ge50_raw'] & ~mint_tab['ge50_dedup']).sum()):,})")
    elig = mint_tab["age_ok"] & mint_tab["ge50_raw"]
    log(f"  today's eligible (age_ok & >=50 raw, as info_audit_v2): {int(elig.sum()):,}")
    log(f"  age_ok mints dropped by >=50 filter: "
        f"{int((mint_tab['age_ok'] & ~mint_tab['ge50_dedup']).sum()):,} of {int(mint_tab['age_ok'].sum()):,}")
    log(f"  n_dedup quantiles (age_ok): {json.dumps({k: round(v, 1) for k, v in _q(mint_tab.loc[mint_tab['age_ok'], 'n_dedup']).items()})}")
    g = gap_ms[mint_tab["has_real"]] / 60_000.0
    log(f"  first_seen - real_created [min] quantiles: {json.dumps({k: round(v, 2) for k, v in _q(g).items()})}")
    born_in_hole = (mint_tab["has_real"] & mint_tab["first_hour_collected"]
                    & ~(mint_tab["real_created_ts"].fillna(0) // HOUR_MS).isin(observed)
                    & (gap_ms > 60_000).fillna(False).astype(bool))
    born_in_hole = born_in_hole.fillna(False).astype(bool)
    log(f"  first seen in a collected hour but CREATED in an uncollected hour (>1 min earlier): "
        f"{int(born_in_hole.sum()):,} ({born_in_hole.mean():.1%} of all)")
    mint_tab["born_in_hole"] = born_in_hole

    per_day = mint_tab.groupby("day").agg(
        all=("mint", "size"), age_ok=("age_ok", "sum"), ge50=("ge50_dedup", "sum"),
        eligible=("ge50_raw", lambda s: int((s & mint_tab.loc[s.index, "age_ok"]).sum())))
    log("  mints by first-seen UTC day (all / age_ok / >=50 dedup / today's eligible):")
    for d_, r_ in per_day.iterrows():
        log(f"    {d_}  {int(r_['all']):>7,}  {int(r_['age_ok']):>7,}  {int(r_['ge50']):>6,}  {int(r_['eligible']):>6,}")
    facts["per_day"] = {d_: {k: int(v) for k, v in r_.items()} for d_, r_ in per_day.iterrows()}
    facts["universe"] = {"mints": n_all, "has_real": int(mint_tab["has_real"].sum()),
                         "age_ok": int(mint_tab["age_ok"].sum()),
                         "ge50_raw": int(mint_tab["ge50_raw"].sum()),
                         "ge50_dedup": int(mint_tab["ge50_dedup"].sum()),
                         "eligible_today": int(elig.sum()),
                         "born_in_hole": int(mint_tab["born_in_hole"].sum())}
    mint_tab.to_parquet(out / "mints.parquet", index=False)
    log(f"  wrote {out / 'mints.parquet'}")

    if a.skip_labels:
        (out / "facts.json").write_text(json.dumps(facts, indent=1, default=str), encoding="utf-8")
        return 0

    # ---------------- Pass B -------------------------------------------
    cut_ms = int(datetime.strptime(a.label_until, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    sel = mint_tab[mint_tab["age_ok"] & (mint_tab["first_ts"] < cut_ms)]
    log(f"\n== PASS B: labels on {len(sel):,} age_ok mints first seen before {a.label_until} "
        f"(NO >=50 filter; today's eligible among them: {int(sel['ge50_raw'].sum()):,}) ==")
    by_b: Dict[int, List[str]] = {}
    for m in sel["mint"].to_numpy():
        by_b.setdefault(sc.bucket_of(m), []).append(m)
    items = sorted(by_b.items())
    del mint_tab
    res = _run_pool(pass_b_bucket, items, a.workers,
                    base_init + (sorted(observed), cfg_tuple, a.stride, a.bar_fraction), "passB", log)
    recs, labs = [], []
    tq = tc = 0.0
    for _, r_, l_, q_, c_ in res:
        recs.extend(r_)
        labs.extend(l_)
        tq += q_
        tc += c_
    del res
    log(f"  summed worker time: query={tq:.0f}s compute={tc:.0f}s")
    tok = pd.DataFrame(recs)
    lab = pd.DataFrame(labs, columns=["mint", "bar_index", "t0_ms", "age_ms", "min_of_hour",
                                      "min_to_cov_end", "old_outcome", "truncated", "cls",
                                      "ret_to_last", "passes_lookahead_filter"])
    tok.to_parquet(out / "tokens_labelperiod.parquet", index=False)
    lab.to_parquet(out / "labels_labelperiod.parquet", index=False)
    log(f"  tokens={len(tok):,} labels={len(lab):,}  -> parquet written")

    # -- stride labels, today's eligible tokens, today's semantics vs corrected
    L = lab[lab["passes_lookahead_filter"]]
    log("\n-- stride-5 labels of TODAY's eligible tokens (>=50 swaps & >=20 bars) --")
    n = len(L)
    old = L["old_outcome"].map(OLD_NAMES)
    log(f"  labels: {n:,}; truncated (dropped today): {int(L['truncated'].sum()):,} ({L['truncated'].mean():.1%})")
    ct = pd.crosstab(old.where(~L["truncated"], "TRUNC"), L["cls"])
    log("  crosstab today's outcome (rows) x coverage class (cols):")
    for line in ct.to_string().splitlines():
        log("    " + line)
    kept_today = L[~L["truncated"]]
    br_today = float((kept_today["old_outcome"] == UP).mean())
    corr = L[L["cls"] != CLS_CENS]
    br_corr = float((corr["cls"] == CLS_UP).mean())
    log(f"  base rate TODAY (UP / non-truncated): {br_today:.4f}  n={len(kept_today):,}")
    log(f"  base rate CORRECTED (UP / non-CENS, DEAD counted as not-UP): {br_corr:.4f}  n={len(corr):,}")
    log(f"  share CENS: {(L['cls'] == CLS_CENS).mean():.1%}   share DEAD: {(L['cls'] == CLS_DEAD).mean():.1%}")
    log(f"  DEAD ret_to_last quantiles: "
        f"{json.dumps({k: round(v, 3) for k, v in _q(L.loc[L['cls'] == CLS_DEAD, 'ret_to_last']).items()})}")

    log("\n-- DIAGNOSTIC T000 (registry): age_ms / minute-of-hour vs y, row-level AUC --")
    diag = {}
    y_today = (kept_today["old_outcome"] == UP).astype(float).to_numpy()
    y_corr = (corr["cls"] == CLS_UP).astype(float).to_numpy()
    for name, col in (("age_ms", "age_ms"), ("minute_of_hour", "min_of_hour"),
                      ("min_to_coverage_end", "min_to_cov_end")):
        a1 = auc(kept_today[col].to_numpy(dtype=float), y_today)
        a2 = auc(corr[col].to_numpy(dtype=float), y_corr)
        diag[name] = {"auc_today_semantics": a1, "auc_cens_removed": a2}
        log(f"  {name:22s} AUC today-semantics={a1:.4f}   CENS removed={a2:.4f}")
    early = corr[corr["min_of_hour"] < 30]
    a3 = auc(early["age_ms"].to_numpy(dtype=float), (early["cls"] == CLS_UP).astype(float).to_numpy())
    log(f"  age_ms AUC, CENS removed, t0 in first 30 min of hour: {a3:.4f} (n={len(early):,})")
    diag["age_ms_cens_removed_first30"] = a3
    facts["diag_T000"] = diag

    # -- one decision per token at K-th bar
    log("\n-- one decision per token at the K-th bar (point-in-time universe vs today's filter) --")
    kres = {}
    for k in DECISION_KS:
        col = f"k{k}_cls"
        reach = tok[tok[col].notna()]
        filt = reach[reach["passes_lookahead_filter"]]
        row = {}
        for nm, df_ in (("all_reaching_K", reach), ("with_lookahead_filter", filt)):
            nc = df_[df_[col] != CLS_CENS]
            shares = nc[col].value_counts(normalize=True).to_dict()
            row[nm] = {"tokens": int(len(df_)), "cens": int((df_[col] == CLS_CENS).sum()),
                       "n_uncensored": int(len(nc)),
                       "base_rate_up": float((nc[col] == CLS_UP).mean()) if len(nc) else None,
                       "shares_uncensored": {kk: round(float(vv), 4) for kk, vv in shares.items()}}
            log(f"  K={k:2d} {nm:22s} tokens={len(df_):>7,} CENS={row[nm]['cens']:>6,} "
                f"uncensored={len(nc):>7,} P(UP)={row[nm]['base_rate_up'] if row[nm]['base_rate_up'] is not None else float('nan'):.4f} "
                f"{json.dumps(row[nm]['shares_uncensored'])}")
        kres[k] = row
        dq = reach.loc[reach[col] != CLS_CENS, f"k{k}_qres"]
        log(f"        quote reserve at decision (SOL) quantiles: {json.dumps({kk: round(vv, 2) for kk, vv in _q(dq).items()})}")
    facts["decision_K"] = kres
    facts["labels"] = {"n_today_eligible_labels": n, "base_rate_today": br_today,
                       "base_rate_corrected": br_corr,
                       "share_truncated": float(L["truncated"].mean()),
                       "share_cens": float((L["cls"] == CLS_CENS).mean()),
                       "share_dead": float((L["cls"] == CLS_DEAD).mean())}

    blob = json.dumps(facts, indent=1, default=str)
    (out / "facts.json").write_text(blob, encoding="utf-8")
    log(f"\nwrote {out / 'facts.json'} sha256={hashlib.sha256(blob.encode()).hexdigest()[:16]}")
    log(f"done {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
