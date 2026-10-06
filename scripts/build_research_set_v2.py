#!/usr/bin/env python3
"""D126: research set v2 -- v1 (D124) with the D125/D126 data corrections:
rows put into execution order inside each slot (chained real reserves), curve
state from the REAL reserve (vendor virtual fields ignored), curve parameters
standard-or-fitted per token, and the ANCHORED outcome model
(tape/outcomes.simulate_anchored). Outcome columns are only to be used where
`outcome_usable` (chain match >= 0.99 and curve parameters explain >= 99% of
swaps) -- pre-registered before any v2 number was seen.

v1 docstring follows (decision rule and variants unchanged).

D124: build the point-in-time research set for ONE frozen zone.

For every token of the zone and every K in --ks: the decision is the swap that
closes the token's K-th dollar bar (same bars as info_audit_v2: 1% of the
first observed depth), if that happens within --max-decision-min of the
create. Tokens that never get there produce no row (point-in-time universe,
D122: no whole-tape filter). Per row:
  * features from swaps <= decision only (tape/pit_features.py + the 53
    TokenState features at the K-th bar, prefixed st_);
  * realised NET outcome of a 2 SOL trade replayed on a counterfactual curve
    (tape/outcomes.py) for 4 pre-registered variants:
        base  latency 1 s, 2 SOL      lat0  latency 0 s, 2 SOL
        lat3  latency 3 s, 2 SOL      s05   latency 1 s, 0.5 SOL
  * per-token curve checks (k invariant spread, share of swaps whose reserve
    change equals the swap amount) -- evidence for the outcome model.

Refuses sealed zones. Workers write one parquet part per (bucket); a rerun
skips finished parts (resumable). Read-only on the store.

    python scripts\\build_research_set_v1.py --zone discovery --workers 4
"""

from __future__ import annotations

import argparse
import json
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
from tape import zones as zn  # noqa: E402
from tape import pit_features as pf  # noqa: E402
from tape.outcomes import TradeConfig, simulate_anchored  # noqa: E402
from tape import curve as cv  # noqa: E402
from tape.bars import BarBuilder, band_bar_threshold  # noqa: E402

VARIANTS = {
    "base": TradeConfig(size_sol=2.0, latency_s=1),
    "lat0": TradeConfig(size_sol=2.0, latency_s=0),
    "lat3": TradeConfig(size_sol=2.0, latency_s=3),
    "s05": TradeConfig(size_sol=0.5, latency_s=1),
}
FETCH_COLS = ["mint", "ts_ms", "slot", "sig", "side", "base_amount", "quote_amount", "wallet",
              "base_reserve_after", "quote_reserve_after", "real_quote_reserve_after", "fee_sol",
              "price", "source", "venue", "pool", "quote_mint"]


def bar_close_indices(quote_amt: np.ndarray, first_depth: float, fraction: float = 0.01):
    """Indices of the swaps that close each dollar bar -- identical to
    tape.bars.BarBuilder('dollar', band_bar_threshold(depth, fraction))
    (tested): a bar closes on the swap at which its accumulated quote volume
    reaches the threshold; the next swap opens the next bar."""
    thr = band_bar_threshold(first_depth, fraction)
    out, acc = [], 0.0
    for i, v in enumerate(quote_amt):
        acc += float(v)
        if acc >= thr:
            out.append(i)
            acc = 0.0
    return out


def tokenstate_features(rows_df, create_ts: int, first_depth: float, fraction: float = 0.01):
    from tape.swaps_cache import rows_to_swaps
    from tape.features import TokenState
    bb = BarBuilder("dollar", threshold=band_bar_threshold(first_depth, fraction))
    st = TokenState("m", created_ts_ms=create_ts)
    last = None
    for s in rows_to_swaps(rows_df):
        bar = bb.push(s)
        if bar is not None:
            st.update(bar)
            last = st.features()
    return last or {}



def prepare_token(g):
    """Execution order inside slots + standard-curve state for features (PIT) +
    per-token curve parameters for outcomes. Returns (g, q_mkt, params, chain_share)."""
    buy = (g["side"].to_numpy() == "buy")
    qa = g["quote_amount"].to_numpy(dtype=float)
    fee = np.nan_to_num(g["fee_sol"].to_numpy(dtype=float), nan=0.0)
    real = g["real_quote_reserve_after"].to_numpy(dtype=float)
    perm, chain_share = cv.order_within_slots(g["slot"].to_numpy(), buy, qa, fee, real, start_real=0.0)
    g = g.iloc[perm].reset_index(drop=True)
    buy, qa, fee, real = buy[perm], qa[perm], fee[perm], real[perm]
    pre = real - cv.signed_flow(buy, qa, fee)
    params = cv.curve_params(pre, real, g["base_amount"].to_numpy(dtype=float))
    q_std = cv.V_STD + real
    g = g.assign(quote_reserve_after=q_std, base_reserve_after=cv.K_STD / q_std)
    q_mkt = params["V"] + real
    return g, q_mkt, params, chain_share


def process_token_rows(g, info, mint, ks, max_dec_ms, collected):
    """All rows (one per K reached) for one token. `g`: its deduped swaps sorted
    by (ts, slot, sig). Features see g[:dec+1] only (after in-slot reordering);
    simulate_anchored() is the only reader of later swaps."""
    rows = []
    create_ts = int(info["create_ts"])
    g, q_mkt, params, chain_share = prepare_token(g)
    ts = g["ts_ms"].to_numpy(dtype="int64")
    buy = (g["side"].to_numpy() == "buy")
    qa = g["quote_amount"].to_numpy(dtype=float)
    ba = g["base_amount"].to_numpy(dtype=float)
    qr = g["quote_reserve_after"].to_numpy(dtype=float)
    depth = float(qr[0]) if len(qr) else 30.0
    closes = bar_close_indices(qa, depth)
    bt = info["bonding_ts"]
    bt = None if bt is None or bt != bt else int(bt)
    creator = info["creator"]
    creator = None if creator is None or creator != creator else str(creator)
    usable = bool(chain_share == chain_share and chain_share >= 0.99 and params["usable"])
    for K in ks:
        if len(closes) < K:
            continue
        d = closes[K - 1]
        if ts[d] - create_ts > max_dec_ms:
            continue
        cut = slice(0, d + 1)
        feats = pf.token_features(
            ts[cut], buy[cut], qa[cut], ba[cut], g["wallet"].to_numpy(dtype=object)[cut],
            g["slot"].to_numpy()[cut], qr[cut], g["real_quote_reserve_after"].to_numpy(dtype=float)[cut],
            g["price"].to_numpy(dtype=float)[cut], g["fee_sol"].to_numpy(dtype=float)[cut],
            create_ts, creator, info["is_mayhem_mode"],
            {k: info[k] for k in ("creator_prior_launches", "creator_prior_grads",
                                  "creator_prior_grad_rate", "creator_launches_24h")},
            {"regime_creates_prev_hour": info["regime_creates_prev_hour"]})
        stf = tokenstate_features(g.iloc[cut], create_ts, depth)
        row = {"mint": mint, "K": K, "create_ts": create_ts, "decision_ts": int(ts[d]),
               "dec_idx": int(d), **feats,
               **{f"st_{k}": (np.nan if v is None else float(v)) for k, v in stf.items()},
               "chk_chain_match": chain_share, "chk_curve_share": params["share"],
               "chk_curve_n": params["n"], "chk_curve_fitted": params["fitted"],
               "chk_curve_V": params["V"], "chk_curve_k_rel": params["k"] / cv.K_STD,
               "outcome_usable": usable}
        fb = feats["fee_buy_ratio"]
        if not fb == fb:
            fb = feats["fee_ratio"] if feats["fee_ratio"] == feats["fee_ratio"] else 0.0125
        fs = feats["fee_sell_ratio"] if feats["fee_sell_ratio"] == feats["fee_sell_ratio"] else fb
        for name, cfg in VARIANTS.items():
            o = simulate_anchored(ts, q_mkt, d, params["k"], fb, fs, cfg, collected, bt)
            row[f"o_{name}_status"] = o.status
            row[f"o_{name}_net"] = o.net_ret
            if name == "base":
                row.update({"o_base_hold_s": o.hold_s, "o_base_entry_q": o.entry_q,
                            "o_base_impact": o.entry_impact, "o_base_max_net": o.max_net,
                            "o_base_min_net": o.min_net, "o_base_graduated": o.graduated,
                            "o_base_n_after": o.n_after, "o_base_exit_ts": o.exit_ts})
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------

_W = {}


def _init(store, out_dir, zone, ks, max_dec_min, collected, mem, tmp):
    _W.update(store=store, out_dir=out_dir, zone=zone, ks=ks, max_dec_ms=int(max_dec_min * 60_000),
              collected=set(collected))
    _W["con"] = ps.open_connection(memory_limit=mem, threads=1, temp_dir=tmp)


def work_bucket(task):
    b, meta = task                       # meta: DataFrame mint, create_ts, creator, is_mayhem, bonding_ts, ctx...
    import pandas as pd
    out_path = Path(_W["out_dir"]) / f"part-b{b:02d}.parquet"
    if out_path.exists():
        return b, "skipped", {}
    t0 = time.time()
    con = _W["con"]
    files = ps.bucket_files(_W["store"], b)
    flist = "[" + ", ".join(ps._q(f.as_posix()) for f in files) + "]"
    horizon_pad = max(c.horizon_ms for c in VARIANTS.values()) + 10 * 60_000
    con.register("want_df", meta[["mint", "create_ts"]])
    df = con.execute(
        f"WITH j AS (SELECT s.* FROM read_parquet({flist}, union_by_name=true) s "
        f"JOIN want_df w ON s.mint = w.mint WHERE s.ts_ms <= w.create_ts + {_W['max_dec_ms'] + horizon_pad}) "
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM j) WHERE rn = 1 "
        f"ORDER BY {ps._FETCH_ORDER}").df()
    con.unregister("want_df")
    t_q = time.time() - t0
    m_index = meta.set_index("mint")
    rows = []
    stats = {"tokens": 0, "with_swaps": 0, "rows": 0}
    if len(df):
        m_arr = df["mint"].to_numpy()
        cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
        for s_, e_ in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(df)]])):
            mint = m_arr[s_]
            stats["with_swaps"] += 1
            rows.extend(process_token_rows(df.iloc[s_:e_], m_index.loc[mint], mint, _W["ks"],
                                           _W["max_dec_ms"], _W["collected"]))
    stats["tokens"] = int(len(meta))
    stats["rows"] = len(rows)
    out = pd.DataFrame(rows)
    tmp = out_path.with_suffix(".tmp")
    out.to_parquet(tmp, index=False)
    os.replace(tmp, out_path)
    stats.update(query_s=t_q, total_s=time.time() - t0)
    return b, "done", stats


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--zones", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--zone", default="discovery", choices=["discovery", "validation"])
    ap.add_argument("--out", default=r"E:\tape_research\sets_v3")   # D133: v2 had merged same-amount swaps
    ap.add_argument("--ks", default="5,10,20")
    ap.add_argument("--max-decision-min", type=float, default=60.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--worker-memory", default="2GB")
    ap.add_argument("--temp-dir", default=r"E:\tape_store_v2\_duckdb_tmp")
    a = ap.parse_args()

    import pandas as pd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from explore_facts_v2 import collected_hours

    out_dir = Path(a.out) / a.zone
    out_dir.mkdir(parents=True, exist_ok=True)
    log_f = open(out_dir / "build_log.txt", "a", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        log_f.write(m + "\n")
        log_f.flush()

    t0 = time.time()
    ks = [int(x) for x in a.ks.split(",")]
    log(f"\n=== build_research_set_v2 {datetime.now(timezone.utc).isoformat()} zone={a.zone} ks={ks} "
        f"variants={list(VARIANTS)} ===")
    mints = zn.load_zone(a.zones, a.zone)          # verifies the frozen sha256
    log(f"  zone {a.zone}: {len(mints):,} tokens (sha256 verified)")
    cov = ps.load_coverage(a.store)
    collected, partial = collected_hours(cov.to_dict("records"))
    log(f"  collected hours: {len(collected):,} (partial files excluded: {len(partial)})")

    ev = ps.load_events(a.store)
    cr = (ev[(ev["event_type"] == "create") & ev["ts_ms"].notna()].sort_values("ts_ms")
          .drop_duplicates("mint")[["mint", "ts_ms", "creator", "is_mayhem_mode"]]
          .rename(columns={"ts_ms": "create_ts"}))
    bc = (ev[(ev["event_type"] == "bonding_complete") & ev["ts_ms"].notna()]
          .groupby("mint")["ts_ms"].min())
    cr["bonding_ts"] = cr["mint"].map(bc)
    cr["create_ts"] = cr["create_ts"].astype("int64")
    ch = pf.creator_history(cr["create_ts"].to_numpy(), cr["creator"].to_numpy(dtype=object),
                            cr["bonding_ts"].to_numpy(dtype=float))
    for k, v in ch.items():
        cr[k] = v
    cr["regime_creates_prev_hour"] = pf.creates_prev_hour(
        cr["create_ts"].to_numpy(), np.sort(cr["create_ts"].to_numpy()), collected)
    log(f"  creates {len(cr):,}; creator/regime context computed ({time.time() - t0:.0f}s)")
    meta = cr[cr["mint"].isin(set(mints))].copy()
    meta["bucket"] = [ps.bucket_of(m) for m in meta["mint"]]
    del ev, cr
    tasks = [(int(b), g.drop(columns=["bucket"]).reset_index(drop=True))
             for b, g in meta.groupby("bucket")]
    log(f"  tasks: {len(tasks)} buckets; parts already done: "
        f"{sum(1 for b, _ in tasks if (out_dir / f'part-b{b:02d}.parquet').exists())}")

    t1 = time.time()
    tot = {"rows": 0, "tokens": 0, "with_swaps": 0}
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                             initargs=(a.store, str(out_dir), a.zone, ks, a.max_decision_min,
                                       sorted(collected), a.worker_memory, a.temp_dir)) as ex:
        for i, (b, st, s) in enumerate(ex.map(work_bucket, tasks), start=1):
            for k in tot:
                tot[k] += s.get(k, 0)
            el = time.time() - t1
            log(f"  [bucket {b:02d} {st}] {i}/{len(tasks)}  rows={s.get('rows', 0):,}  "
                f"({s.get('total_s', 0):.0f}s)  elapsed={el:.0f}s ETA~{el / i * (len(tasks) - i):.0f}s")

    parts = sorted(out_dir.glob("part-b*.parquet"))
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    log(f"\n  rows total: {len(df):,} from {len(parts)} parts")
    tok = df.drop_duplicates("mint")
    log(f"  checks per token: chain match median={tok['chk_chain_match'].median():.4f} "
        f"share>=0.99: {(tok['chk_chain_match'] >= 0.99).mean():.4f}; curve fitted (non-standard): "
        f"{tok['chk_curve_fitted'].mean():.4f}; outcome_usable: {tok['outcome_usable'].mean():.4f}")
    log(f"  non-standard curves by mayhem flag: "
        f"{tok.groupby(tok['is_mayhem'].fillna(-1))['chk_curve_fitted'].mean().round(4).to_dict()}")
    for K in ks:
        d = df[df["K"] == K]
        du = d[d["outcome_usable"]]
        st = du["o_base_status"].value_counts(normalize=True).round(4).to_dict()
        unc = du[~du["o_base_status"].isin(["CENS", "NOENTRY"])]
        log(f"  K={K:2d}: tokens={len(d):,} usable={len(du):,}  status(usable)={st}")
        if len(unc):
            log(f"        base (2 SOL, 1 s): P(TP)={(unc['o_base_status'] == 'TP').mean():.4f}  "
                f"mean net={unc['o_base_net'].mean():+.4f}  median net={unc['o_base_net'].median():+.4f}  "
                f"entry impact median={unc['o_base_impact'].median():.4f}")
            for v in ("lat0", "lat3", "s05"):
                u2 = du[~du[f"o_{v}_status"].isin(["CENS", "NOENTRY"])]
                log(f"        {v}: P(TP)={(u2[f'o_{v}_status'] == 'TP').mean():.4f}  mean net={u2[f'o_{v}_net'].mean():+.4f}")
    log("  NOTE: zone-wide baselines ('enter every token at K'); no selection has been made.")
    log(f"  done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
