#!/usr/bin/env python3
"""explore_facts_v2 -- corpus facts on the pumpfundata store v2 (D121).

Same discipline as explore_facts_v1 (D119), on E:\\tape_store_v2 (D120):
read-only, outcome-free facts on the WHOLE corpus, labels only for a
discovery-safe early period.

  Part A (no outcomes, whole corpus) -- from manifest/events/mint index only:
    * coverage: collected UTC hours per day/month; a file counts as collected
      only if its swap count is >= --partial-frac x the median file of its day
      (a vendor file with a fraction of the normal volume is a PARTIAL hour)
    * measured fee ratio (vendor fee_lamports / lamports_amount) per month
    * creates / bonding_complete / graduation share per month, creators
    * universe U0 = mints whose `create` event is in the store (born in a
      collected hour -> full tape from birth, true age known); tokens per day
    * share of U0 removed by today's whole-tape >=50-swap filter
  Part B (labels; U0 tokens created before --label-until):
    * explore_facts_v1.process_token (same bars/labels as info_audit_v2)
      + CENS/DEAD/UP/DOWN/TIMEOUT classes, base rates, K-th-bar decisions
    * T000: AUC of age (from first swap AND from real creation) and of
      minute-of-hour, today's semantics vs CENS removed

    python scripts\\explore_facts_v2.py --workers 4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import explore_facts_v1 as v1  # noqa: E402
from tape import pf_store as ps  # noqa: E402
from tape.labels import BarrierConfig, UP  # noqa: E402

HOUR_MS = ps.HOUR_MS


# ---------------------------------------------------------------------------
# Pure helpers (tests/test_explore_facts.py)
# ---------------------------------------------------------------------------

def collected_hours(cov_rows: List[dict], partial_frac: float = 0.25) -> Tuple[Set[int], List[dict]]:
    """cov_rows: dicts with date, hour, status, n_swaps (one per raw file).
    Returns (set of collected UTC hour indices, list of rows judged partial).
    Collected = usable status AND n_swaps >= partial_frac * median n_swaps of
    the usable files of the same UTC day."""
    by_day: Dict[str, List[dict]] = {}
    for r in cov_rows:
        if r.get("status") in ps.DONE_STATUSES:
            by_day.setdefault(r["date"], []).append(r)
    hours, partial = set(), []
    for d, rows in by_day.items():
        med = float(np.median([float(r.get("n_swaps") or 0) for r in rows]))
        for r in rows:
            if float(r.get("n_swaps") or 0) >= partial_frac * med:
                hours.add(ps.hour_window_ms(r["date"], int(r["hour"]))[0] // HOUR_MS)
            else:
                partial.append({**r, "day_median": med})
    return hours, partial


# ---------------------------------------------------------------------------
# Pass B worker (module level: Windows spawn)
# ---------------------------------------------------------------------------

_W: Dict = {}


def _init_worker(root, memory_limit, temp_dir, observed, cfg_tuple, stride, bar_fraction):
    _W["root"] = root
    _W["con"] = ps.open_connection(memory_limit=memory_limit, threads=1, temp_dir=temp_dir)
    _W["observed"] = set(observed)
    _W["cfg"] = BarrierConfig(*cfg_tuple)
    _W["stride"] = stride
    _W["bar_fraction"] = bar_fraction


def pass_b_bucket(args):
    b, mints = args
    import pandas as pd
    from tape.swaps_cache import rows_to_swaps
    con = _W["con"]
    t0 = time.time()
    files = ps.bucket_files(_W["root"], b)
    flist = "[" + ", ".join(ps._q(f.as_posix()) for f in files) + "]"
    con.register("want_df", pd.DataFrame({"mint": list(mints)}))
    df = con.execute(
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM read_parquet({flist}, "
        f"union_by_name=true) WHERE mint IN (SELECT mint FROM want_df)) WHERE rn = 1 "
        f"ORDER BY {ps._FETCH_ORDER}").df()
    con.unregister("want_df")
    t_q = time.time() - t0
    recs, labels = [], []
    if len(df):
        m_arr = df["mint"].to_numpy()
        cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
        for s_, e_ in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(df)]])):
            mint = m_arr[s_]
            swaps = rows_to_swaps(df.iloc[s_:e_])
            rec, rows = v1.process_token(mint, swaps, _W["cfg"], _W["observed"],
                                         _W["bar_fraction"], _W["stride"])
            recs.append(rec)
            for r in rows:
                labels.append((mint,) + r + (rec["passes_lookahead_filter"],))
    return b, recs, labels, t_q, time.time() - t0 - t_q


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--out", default=r"E:\tape_research\facts_v2")
    ap.add_argument("--temp-dir", default=r"E:\tape_store_v2\_duckdb_tmp")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--worker-memory", default="2GB")
    ap.add_argument("--partial-frac", type=float, default=0.25)
    ap.add_argument("--label-until", default="2026-03-01",
                    help="labels only for U0 tokens CREATED before this UTC date (discovery-safe)")
    ap.add_argument("--upper", type=float, default=1.6)
    ap.add_argument("--lower", type=float, default=-0.30)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--bar-fraction", type=float, default=0.01)
    ap.add_argument("--skip-labels", action="store_true")
    a = ap.parse_args()

    import pandas as pd

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log_f = open(out / "facts_log.txt", "w", encoding="utf-8")

    def log(msg=""):
        print(msg, flush=True)
        log_f.write(msg + "\n")
        log_f.flush()

    root = Path(a.store)
    log(f"explore_facts_v2 start {datetime.now(timezone.utc).isoformat()} pid={os.getpid()}")
    log(f"  store={root}  out={out}  label_until={a.label_until}  workers={a.workers}")
    comp = json.loads((root / "compacted.json").read_text(encoding="utf-8"))
    log(f"  compacted months: {sorted(comp)}")
    facts: Dict = {"generated_utc": datetime.now(timezone.utc).isoformat(), "args": vars(a),
                   "compacted": comp}

    # ---------------- coverage -------------------------------------------
    t0 = time.time()
    cov = ps.load_coverage(root)
    cov = cov[cov["date"].str[:7].isin(comp)]
    observed, partial = collected_hours(cov.to_dict("records"), a.partial_frac)
    log(f"\n== COVERAGE ({time.time() - t0:.0f}s) ==")
    log(f"  raw files: {len(cov):,}; statuses {cov['status'].value_counts().to_dict()}")
    log(f"  collected hours: {len(observed):,}; partial files (< {a.partial_frac} x day median): {len(partial)}")
    for p_ in sorted(partial, key=lambda r: (r["date"], r["hour"]))[:40]:
        log(f"    partial {p_['date']} {int(p_['hour']):02d}h  n_swaps={int(p_.get('n_swaps') or 0):,} "
            f"(day median {p_['day_median']:,.0f})")
    if len(partial) > 40:
        log(f"    ... {len(partial) - 40} more")
    cov["hidx"] = [ps.hour_window_ms(d, int(h))[0] // HOUR_MS for d, h in zip(cov["date"], cov["hour"])]
    cov["collected"] = cov["hidx"].isin(observed)
    cov["month"] = cov["date"].str[:7]
    per_day_cov = cov.groupby("date")["collected"].sum()
    for m, g in cov.groupby("month"):
        days = per_day_cov[per_day_cov.index.str[:7] == m]
        fr = pd.to_numeric(g.get("fee_ratio_p50"), errors="coerce")
        log(f"  {m}: days={len(days):3d}  collected h/day: min={int(days.min())} median={int(days.median())} "
            f"max={int(days.max())}  swaps={int(pd.to_numeric(g['n_swaps'], errors='coerce').fillna(0).sum()):>12,}  "
            f"fee ratio p50 (median over files)={fr.median():.5f}")
    hs = sorted(observed)
    runs, st, pr = [], (hs[0] if hs else None), (hs[0] if hs else None)
    for h_ in hs[1:]:
        if h_ != pr + 1:
            runs.append(pr - st + 1)
            st = h_
        pr = h_
    if hs:
        runs.append(pr - st + 1)
    log(f"  contiguous collected runs (hours -> count): {dict(sorted(pd.Series(runs).value_counts().items()))}")
    (out / "collected_hours.json").write_text(json.dumps(hs), encoding="utf-8")
    facts["coverage"] = {"n_files": int(len(cov)), "collected_hours": len(observed),
                         "partial_files": [{k: v for k, v in p_.items() if k in ("date", "hour", "n_swaps", "day_median")}
                                           for p_ in partial],
                         "run_lengths": {int(k): int(v) for k, v in pd.Series(runs).value_counts().items()}}

    # ---------------- events / universe ------------------------------------
    t0 = time.time()
    ev = ps.load_events(root)
    creates = ev[ev["event_type"] == "create"].dropna(subset=["ts_ms"])
    cr = creates.sort_values("ts_ms").drop_duplicates("mint")[["mint", "ts_ms", "creator", "is_mayhem_mode"]]
    cr = cr.rename(columns={"ts_ms": "create_ts"})
    bc = (ev[ev["event_type"] == "bonding_complete"].dropna(subset=["ts_ms"])
          .groupby("mint", as_index=False)["ts_ms"].min().rename(columns={"ts_ms": "bonding_ts"}))
    mi = ps.load_mint_index(root)
    log(f"\n== UNIVERSE ({time.time() - t0:.0f}s) ==")
    log(f"  events: {len(ev):,} (create {len(creates):,}, bonding_complete {int((ev['event_type'] == 'bonding_complete').sum()):,}); "
        f"mints with swaps: {len(mi):,}")
    u = mi.merge(cr, on="mint", how="left").merge(bc, on="mint", how="left")
    u["has_create"] = u["create_ts"].notna()
    u["graduated"] = u["bonding_ts"].notna()
    u0 = u[u["has_create"]].copy()
    u0["day"] = pd.to_datetime(u0["create_ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    u0["month"] = u0["day"].str[:7]
    u0["first_minus_create_s"] = (u0["first_ts"] - u0["create_ts"]) / 1000.0
    u0["ge50_raw"] = u0["n_raw"] >= v1.MIN_SWAPS_PER_TOKEN
    log(f"  mints with swaps but no create event (born outside collected hours/before corpus): "
        f"{int((~u['has_create']).sum()):,} ({(~u['has_create']).mean():.1%})")
    log(f"  U0 (create event in store): {len(u0):,}; graduated (bonding_complete seen): {int(u0['graduated'].sum()):,} "
        f"({u0['graduated'].mean():.2%}) -- lower bound: graduations in uncollected hours are not seen")
    log(f"  first swap - create [s] quantiles: "
        f"{json.dumps({k: round(v, 1) for k, v in v1._q(u0['first_minus_create_s']).items()})}")
    log(f"  U0 dropped by today's >=50 raw swaps (whole-tape) filter: {int((~u0['ge50_raw']).sum()):,} "
        f"({(~u0['ge50_raw']).mean():.1%})")
    log(f"  n_raw quantiles (U0): {json.dumps({k: round(v, 1) for k, v in v1._q(u0['n_raw']).items()})}")
    cl = u0.groupby("creator").size()
    log(f"  creators: {len(cl):,}; launches/creator quantiles {json.dumps({k: round(v, 1) for k, v in v1._q(cl).items()})}; "
        f"share of U0 from creators with >=10 launches: {u0['creator'].map(cl).ge(10).mean():.1%}")
    log(f"  is_mayhem_mode share: {pd.to_numeric(u0['is_mayhem_mode'], errors='coerce').mean():.2%}")
    log("  per month: U0 tokens / graduated % / >=50 swaps %")
    pm = u0.groupby("month").agg(n=("mint", "size"), grad=("graduated", "mean"), ge50=("ge50_raw", "mean"))
    for m, r in pm.iterrows():
        log(f"    {m}: {int(r.n):>8,}  {r.grad:6.2%}  {r.ge50:6.1%}")
    pdy = u0.groupby("day").agg(n=("mint", "size"), grad=("graduated", "sum"), ge50=("ge50_raw", "sum"))
    pdy = pdy.join(per_day_cov.rename("collected_hours"), how="left")
    pdy.to_parquet(out / "per_day.parquet")
    log(f"  per-day table -> {out / 'per_day.parquet'} ({len(pdy)} days); U0/day quantiles "
        f"{json.dumps({k: round(v) for k, v in v1._q(pdy['n']).items()})}")
    u0.to_parquet(out / "universe_u0.parquet", index=False)
    facts["universe"] = {"mints_with_swaps": int(len(mi)), "u0": int(len(u0)),
                         "no_create": int((~u['has_create']).sum()),
                         "graduated_share_u0": float(u0["graduated"].mean()),
                         "ge50_share_u0": float(u0["ge50_raw"].mean()),
                         "per_month": {m: {"n": int(r.n), "grad": float(r.grad), "ge50": float(r.ge50)}
                                       for m, r in pm.iterrows()}}

    if a.skip_labels:
        (out / "facts.json").write_text(json.dumps(facts, indent=1, default=str), encoding="utf-8")
        return 0

    # ---------------- Pass B ------------------------------------------------
    cut_ms = int(datetime.strptime(a.label_until, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    sel = u0[u0["create_ts"] < cut_ms]
    create_ts = dict(zip(sel["mint"], sel["create_ts"]))
    log(f"\n== PASS B: labels for {len(sel):,} U0 tokens created before {a.label_until} (no >=50 filter) ==")
    by_b: Dict[int, List[str]] = {}
    for m, b in zip(sel["mint"], sel["bucket"]):
        by_b.setdefault(int(b), []).append(m)
    items = sorted(by_b.items())
    del u, mi, ev, creates
    cfg_tuple = (a.upper, a.lower, int(a.horizon_min * 60_000))
    res = []
    t1 = time.time()
    last = 0.0
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_init_worker,
                             initargs=(str(root), a.worker_memory, a.temp_dir, hs, cfg_tuple,
                                       a.stride, a.bar_fraction)) as ex:
        for i, r in enumerate(ex.map(pass_b_bucket, items), start=1):
            res.append(r)
            el = time.time() - t1
            if el - last >= 5 or i == len(items):
                last = el
                log(f"  [passB] {i}/{len(items)} buckets  elapsed={el:.0f}s  ETA~{el / i * (len(items) - i):.0f}s")
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
    lab["age_real_ms"] = lab["t0_ms"] - lab["mint"].map(create_ts)
    tok["create_ts"] = tok["mint"].map(create_ts)
    tok.to_parquet(out / "tokens_labelperiod.parquet", index=False)
    lab.to_parquet(out / "labels_labelperiod.parquet", index=False)
    log(f"  tokens={len(tok):,} labels={len(lab):,} -> parquet written")

    L = lab[lab["passes_lookahead_filter"]]
    log("\n-- stride-5 labels of TODAY's eligible tokens (>=50 swaps & >=20 bars) --")
    old = L["old_outcome"].map(v1.OLD_NAMES)
    log(f"  labels: {len(L):,}; truncated (dropped today): {int(L['truncated'].sum()):,} ({L['truncated'].mean():.1%})")
    ct = pd.crosstab(old.where(~L["truncated"], "TRUNC"), L["cls"])
    for line in ct.to_string().splitlines():
        log("    " + line)
    kept_today = L[~L["truncated"]]
    corr = L[L["cls"] != v1.CLS_CENS]
    br_today = float((kept_today["old_outcome"] == UP).mean())
    br_corr = float((corr["cls"] == v1.CLS_UP).mean())
    log(f"  base rate TODAY (UP / non-truncated): {br_today:.4f} n={len(kept_today):,}")
    log(f"  base rate CORRECTED (UP / non-CENS; DEAD = not UP): {br_corr:.4f} n={len(corr):,}")
    log(f"  share CENS {(L['cls'] == v1.CLS_CENS).mean():.1%}  DEAD {(L['cls'] == v1.CLS_DEAD).mean():.1%}")
    log(f"  DEAD ret_to_last quantiles: "
        f"{json.dumps({k: round(v, 3) for k, v in v1._q(L.loc[L['cls'] == v1.CLS_DEAD, 'ret_to_last']).items()})}")

    log("\n-- DIAGNOSTIC T000: row-level AUC vs y --")
    y_today = (kept_today["old_outcome"] == UP).astype(float).to_numpy()
    y_corr = (corr["cls"] == v1.CLS_UP).astype(float).to_numpy()
    diag = {}
    for name in ("age_ms", "age_real_ms", "min_of_hour", "min_to_cov_end"):
        a1 = v1.auc(kept_today[name].to_numpy(dtype=float), y_today)
        a2 = v1.auc(corr[name].to_numpy(dtype=float), y_corr)
        diag[name] = {"today": a1, "cens_removed": a2}
        log(f"  {name:16s} AUC today-semantics={a1:.4f}   CENS removed={a2:.4f}")
    facts["diag_T000"] = diag

    log("\n-- one decision per token at the K-th bar --")
    kres = {}
    for k in v1.DECISION_KS:
        col = f"k{k}_cls"
        reach = tok[tok[col].notna()]
        row = {}
        for nm, df_ in (("all_reaching_K", reach), ("with_lookahead_filter", reach[reach["passes_lookahead_filter"]])):
            nc = df_[df_[col] != v1.CLS_CENS]
            row[nm] = {"tokens": int(len(df_)), "cens": int((df_[col] == v1.CLS_CENS).sum()),
                       "n_uncensored": int(len(nc)),
                       "base_rate_up": float((nc[col] == v1.CLS_UP).mean()) if len(nc) else None,
                       "shares": {kk: round(float(vv), 4) for kk, vv in nc[col].value_counts(normalize=True).items()}}
            br = row[nm]["base_rate_up"]
            log(f"  K={k:2d} {nm:22s} tokens={len(df_):>8,} CENS={row[nm]['cens']:>7,} "
                f"uncensored={len(nc):>8,} P(UP)={(br if br is not None else float('nan')):.4f} "
                f"{json.dumps(row[nm]['shares'])}")
        dq = reach.loc[reach[col] != v1.CLS_CENS, f"k{k}_qres"]
        log(f"        quote reserve at decision (SOL): {json.dumps({kk: round(vv, 2) for kk, vv in v1._q(dq).items()})}")
        dt = (reach[f"k{k}_t0"] - reach["create_ts"]) / 1000.0
        log(f"        seconds from creation to decision: {json.dumps({kk: round(vv, 1) for kk, vv in v1._q(dt).items()})}")
        kres[k] = row
    facts["decision_K"] = kres
    facts["labels"] = {"base_rate_today": br_today, "base_rate_corrected": br_corr,
                       "share_truncated": float(L["truncated"].mean()),
                       "share_cens": float((L["cls"] == v1.CLS_CENS).mean()),
                       "share_dead": float((L["cls"] == v1.CLS_DEAD).mean())}

    blob = json.dumps(facts, indent=1, default=str)
    (out / "facts.json").write_text(blob, encoding="utf-8")
    log(f"\nwrote {out / 'facts.json'} sha256={hashlib.sha256(blob.encode()).hexdigest()[:16]}")
    log(f"done {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
