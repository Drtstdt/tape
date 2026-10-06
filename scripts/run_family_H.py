#!/usr/bin/env python3
"""D132: family H -- LONG holds (2 h / 6 h / 24 h), no stop loss.

Hypothesis (user, D132): near the bottom of the bonding curve the downside is
bounded (D128: about -6.5% at Q=31 even if every other holder dumps) while
a token that graduates is worth ~14x the floor price -- so holding for hours
could pay even if most tokens go nowhere.

Fixed BEFORE the first run (docs/DECISIONS.md D132):
  entry sets (reproduced, never re-chosen; the run stops on a mismatch)
    E000   LightGBM out-of-fold selection, pred > 0 (n 16,392, mean -0.0104)
    FLOOR  family-A q_gain_sol rule: direction/threshold from the first half
           (trials/family_A/results.json), applied to the second half
           (n 30,555, mean -0.0346)
  trade    2 SOL, latency 1 s, standard curve state from real reserves
           (anchored), token fee ratios exactly as in the research set
  grid     horizon {2 h, 6 h, 24 h} x exit {time only, TP +100% else time}
           x entry set = 12 trials, plus (D134, added before any H run) two
           partial exits at 6 h -- sell 50% at +100%, the rest (a) held to
           6 h or (b) out when the price is back at the entry price -- x 2
           entry sets = 16 trials (family budget 16)
  gaps     tape/outcomes.simulate_long: TP checked at observed swaps only
           (conservative); time exit at the exact state just before the first
           observed swap after the deadline (delay reported); graduation exits
           at the completed curve; tokens with no later observed swap valued at
           the last state (primary) AND at the worst case (pessimistic bound)
  pass     pooled mean net day-block bootstrap 95% CI > 0 for BOTH the primary
           and the pessimistic valuation AND >= 60% of test weeks positive
           AND BH q < 0.10 (one-sided bootstrap p, primary)
Refuses to run twice.

D133 (before any H run): research set v3; the expected entry sets come from
the v3 replications (E000: trials/family_E_v3/results.json -- n, mint sha256,
mean; FLOOR: trials/family_A_v3/results.json -- threshold, direction, n,
mean), not from hard-coded v2 numbers. A consistency check like family F's:
the base config (+60/-30/30 min) replayed on the SAME fetch window as the
research set must reproduce o_base_net (|diff| < 1e-6) before anything is
registered.

    python scripts\\run_family_H.py --workers 4
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tape import pf_store as ps  # noqa: E402
from tape import trials as tr  # noqa: E402
from tape import zones as zn  # noqa: E402
from tape import curve as cv  # noqa: E402
from tape.outcomes import simulate_anchored, simulate_long, simulate_partial  # noqa: E402
from tape.registry import Registry, spec_hash  # noqa: E402

HORIZONS_H = (2, 6, 24)
EXITS = (None, 1.0)                      # time only | TP +100% else time
# (horizon_h, take_profit, partial) -- partial = (fraction sold at the TP, rule for the rest) | None
CONFIGS = [(h, x, None) for h, x in itertools.product(HORIZONS_H, EXITS)] + [
    (6, 1.0, (0.5, "time")),             # D134: sell 50% at +100%, the rest held to 6 h
    (6, 1.0, (0.5, "breakeven")),        # D134: sell 50% at +100%, the rest exits back at the entry price
]


def config_label(h, x, part) -> str:
    if part:
        return f"{h}h 50%@TP+100% rest:{part[1]}"
    return f"{h}h {'TP+100%' if x else 'time'}"
SETS = ("E000", "FLOOR")
TOL = 0.0005
SET_PAD_MS = 30 * 60_000 + 10 * 60_000   # the research set's fetch window after MAX_DEC_MS (build_research_set_v2)
MAX_DEC_MS = 60 * 60_000
FETCH_AFTER_MS = 48 * 3_600_000          # look for the first swap after a 24 h deadline up to 48 h out


_W = {}


def _init(store, mem, tmp):
    _W["store"] = store
    _W["con"] = ps.open_connection(memory_limit=mem, threads=1, temp_dir=tmp)


def work_bucket(task):
    b, meta = task
    import build_research_set_v2 as b2
    con = _W["con"]
    files = ps.bucket_files(_W["store"], b)
    flist = "[" + ", ".join(ps._q(f.as_posix()) for f in files) + "]"
    con.register("want_df", meta[["mint", "create_ts"]])
    df = con.execute(
        f"WITH j AS (SELECT s.* FROM read_parquet({flist}, union_by_name=true) s JOIN want_df w "
        f"ON s.mint = w.mint WHERE s.ts_ms <= w.create_ts + {MAX_DEC_MS + max(HORIZONS_H) * 3_600_000 + FETCH_AFTER_MS}) "
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM j) WHERE rn = 1 "
        f"ORDER BY {ps._FETCH_ORDER}").df()
    con.unregister("want_df")
    m_index = meta.set_index("mint")
    out, mism = [], 0
    base_cfg = b2.VARIANTS["base"]
    if len(df):
        m_arr = df["mint"].to_numpy()
        cuts = np.flatnonzero(m_arr[1:] != m_arr[:-1]) + 1
        for s_, e_ in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(df)]])):
            mint = m_arr[s_]
            info = m_index.loc[mint]
            tok = df.iloc[s_:e_]
            # consistency replay: exactly the research set's fetch window and outcome model
            short = tok[tok["ts_ms"] <= int(info["create_ts"]) + MAX_DEC_MS + SET_PAD_MS]
            g2, q2, p2, _ = b2.prepare_token(short)
            ts2 = g2["ts_ms"].to_numpy(dtype="int64")
            c2 = b2.bar_close_indices(g2["quote_amount"].to_numpy(dtype=float),
                                      float(g2["quote_reserve_after"].to_numpy()[0]))
            g, q_mkt, params, _ = b2.prepare_token(tok)
            ts = g["ts_ms"].to_numpy(dtype="int64")
            closes = b2.bar_close_indices(g["quote_amount"].to_numpy(dtype=float),
                                          float(g["quote_reserve_after"].to_numpy()[0]))
            if len(closes) < 10 or int(ts[closes[9]]) != int(info["decision_ts"]):
                mism += 1
                continue
            flow = cv.signed_flow(g["side"].to_numpy() == "buy", g["quote_amount"].to_numpy(dtype=float),
                                  g["fee_sol"].to_numpy(dtype=float))
            bt = info["bonding_ts"]
            bt = None if bt != bt else int(bt)
            row = {"mint": mint, "chk_base_net": float("nan"), "chk_long_fitted": bool(params["fitted"]),
                   "chk_long_k_rel": float(params["k"] / cv.K_STD)}
            if len(c2) >= 10 and int(ts2[c2[9]]) == int(info["decision_ts"]):
                row["chk_base_net"] = simulate_anchored(ts2, q2, c2[9], p2["k"], float(info["fee_b"]),
                                                        float(info["fee_s"]), base_cfg, None, bt).net_ret
            for i, (h, x, part) in enumerate(CONFIGS):
                if part is None:
                    o = simulate_long(ts, q_mkt, flow, closes[9], params["k"], params["V"], float(info["fee_b"]),
                                      float(info["fee_s"]), 2.0, 1, int(h * 3_600_000), x, bt)
                else:
                    o = simulate_partial(ts, q_mkt, flow, closes[9], params["k"], params["V"], float(info["fee_b"]),
                                         float(info["fee_s"]), 2.0, 1, int(h * 3_600_000), x, part[0], part[1], bt)
                row[f"net_{i}"] = o.net_ret
                row[f"pes_{i}"] = o.net_pessimistic
                row[f"st_{i}"] = o.status
                row[f"delay_{i}"] = o.exit_delay_s
            out.append(row)
    return b, out, mism


def main() -> int:
    holder = []
    try:
        return _main(holder)
    finally:
        for f in holder:
            f.close()


def _main(holder) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sets", default=r"E:\tape_research\sets_v3\discovery")
    ap.add_argument("--expect-e", default=r"E:\tape_research\trials\family_E_v3\results.json")
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--zones", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--family-a", default=r"E:\tape_research\trials\family_A_v3\results.json")
    ap.add_argument("--out", default=r"E:\tape_research\trials\family_H_v3")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--temp-dir", default=r"E:\tape_store_v2\_duckdb_tmp")
    a = ap.parse_args()
    import pandas as pd
    import run_family_E as rfe

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lf = open(out / "log.txt", "a", encoding="utf-8")
    holder.append(lf)

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    reg = Registry(a.registry)
    specs = {(sname, i): {"family": "H", "entries": sname, "horizon_h": h, "tp": x, "sl": None, "size_sol": 2.0,
                          "partial": (None if part is None else {"sell_frac": part[0], "rest": part[1]}),
                          "latency_s": 1, "set": "sets_v3", "valuation": "primary + pessimistic bound"}
             for sname in SETS for i, (h, x, part) in enumerate(CONFIGS)}
    if any(spec_hash(s) in {t.get("spec_hash") for t in reg.trials().values()} for s in specs.values()):
        log("REFUSED: family H trials with these specs are already registered (no re-rolls).")
        return 2
    ja = json.loads(Path(a.family_a).read_text(encoding="utf-8"))
    je = json.loads(Path(a.expect_e).read_text(encoding="utf-8"))
    for nm, j in (("family A", ja), ("family E", je)):
        if not j.get("replication") or j.get("set") != "sets_v3":
            log(f"STOP: {nm} results are not the sets_v3 replication -- run its `--replicate` first")
            return 5
    ra = ja["results"]["q_gain_sol"]
    expect = {"E000": (je["e000_entry"]["n"], je["e000_entry"]["mean"]),
              "FLOOR": (ra["n_selected"], ra["mean_sel_second"])}
    t0 = time.time()
    log(f"\n=== run_family_H {datetime.now(timezone.utc).isoformat()} sets={a.sets} ===")

    disc = set(zn.load_zone(a.zones, "discovery"))
    df = rfe.load(Path(a.sets), log)
    df = df[df["K"] == rfe.K]
    if not set(df["mint"]).issubset(disc):
        log("REFUSED: research set contains mints outside the frozen discovery zone")
        return 3
    fit, model_name = rfe.make_model()
    u, cols, X, y, ts, week, day, weeks = rfe.prepare_universe(df)
    pred, _, _, _ = rfe.walk_forward(u, X, y, ts, week, weeks, fit, log)
    sel_e = np.isfinite(pred) & (pred > 0)
    first = np.arange(len(u)) < len(u) // 2
    xq = u["q_gain_sol"].to_numpy(float)
    sel_f = (~first) & ((xq >= ra["threshold"]) if ra["direction"] > 0 else (xq <= ra["threshold"]))
    for name, sel in (("E000", sel_e), ("FLOOR", sel_f)):
        n_, m_ = int(sel.sum()), float(np.nanmean(y[sel]))
        log(f"  reproduced {name}: n={n_:,} mean net {m_:+.4f} (replication n={expect[name][0]:,} "
            f"mean {expect[name][1]:+.4f})")
        bad_hash = (name == "E000" and hashlib.sha256("\n".join(sorted(u["mint"].to_numpy()[sel].astype(str)))
                                                     .encode()).hexdigest() != je["e000_entry"]["mints_sha256"])
        if n_ != expect[name][0] or abs(m_ - expect[name][1]) > TOL or bad_hash:
            log(f"STOP: entry set {name} did not reproduce -- family H is not run on a different set.")
            return 5
    u["in_E000"], u["in_FLOOR"], u["week"], u["day"] = sel_e, sel_f, week, day
    ent = u[sel_e | sel_f].copy()
    ent["fee_b"] = ent["fee_buy_ratio"].where(ent["fee_buy_ratio"].notna(),
                                              ent["fee_ratio"].where(ent["fee_ratio"].notna(), 0.0125))
    ent["fee_s"] = ent["fee_sell_ratio"].where(ent["fee_sell_ratio"].notna(), ent["fee_b"])
    ev = ps.load_events(a.store)
    bc = ev[(ev["event_type"] == "bonding_complete") & ev["ts_ms"].notna()].groupby("mint")["ts_ms"].min()
    ent["bonding_ts"] = ent["mint"].map(bc)
    ent["bucket"] = [ps.bucket_of(m) for m in ent["mint"]]
    del df, X, ev
    log(f"  tokens to replay: {len(ent):,} (E000 {int(sel_e.sum()):,}, FLOOR {int(sel_f.sum()):,}, "
        f"both {int((sel_e & sel_f).sum()):,})")

    tasks = [(int(b), g[["mint", "create_ts", "decision_ts", "fee_b", "fee_s", "bonding_ts"]].reset_index(drop=True))
             for b, g in ent.groupby("bucket")]
    rows, mism = [], 0
    t1 = time.time()
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                             initargs=(a.store, "2GB", a.temp_dir)) as ex:
        for i, (b, r_, m_) in enumerate(ex.map(work_bucket, tasks), start=1):
            rows.extend(r_)
            mism += m_
            if i % 4 == 0 or i == len(tasks):
                el = time.time() - t1
                log(f"  [replay] {i}/{len(tasks)} buckets  tokens={len(rows):,}  elapsed={el:.0f}s "
                    f"ETA~{el / i * (len(tasks) - i):.0f}s")
    res = pd.DataFrame(rows).merge(ent[["mint", "week", "day", "in_E000", "in_FLOOR", "o_base_net"]], on="mint")
    log(f"  replayed {len(res):,} of {len(ent):,}; decision mismatches skipped: {mism}")
    diff = np.abs(res["chk_base_net"] - res["o_base_net"])
    agree = float(np.nanmax(diff)) if diff.notna().any() else float("nan")
    n_nan = int(res["chk_base_net"].isna().sum())
    log(f"  consistency: base config on the research set's window vs o_base_net, max |diff| = {agree:.2e}"
        f" (no decision in the short window: {n_nan})")
    log(f"  long tape fitted as non-standard curve: {int(res['chk_long_fitted'].sum()):,} of {len(res):,} "
        f"(standard within the first window by universe definition)")
    if not (agree < 1e-6 and n_nan == 0 and mism == 0):
        bad = res.assign(diff=diff)
        bad = bad[~(bad["diff"] < 1e-6)].sort_values("diff", ascending=False)
        bad[["mint", "o_base_net", "chk_base_net", "diff"]].to_csv(out / "consistency_diff.csv", index=False)
        log(f"  tokens failing: {len(bad):,}; decision mismatches {mism} -> {out / 'consistency_diff.csv'}")
        log("STOP: the replay does not reproduce the research set -- nothing registered.")
        return 6
    res.to_parquet(out / "replay.parquet", index=False)

    rng = np.random.default_rng(17)
    ids, results = {}, {}
    for sname in SETS:
        sub = res[res[f"in_{sname}"]].reset_index(drop=True)
        dd, wk_ = sub["day"].to_numpy(), sub["week"].to_numpy()
        days_u = np.unique(dd)
        idx_by_day = [np.flatnonzero(dd == d_) for d_ in days_u]
        allsel = np.ones(len(sub), dtype=bool)
        for i, (h, x, part) in enumerate(CONFIGS):
            yv = sub[f"net_{i}"].to_numpy(float)
            yp = sub[f"pes_{i}"].to_numpy(float)
            ci = tr.day_block_bootstrap(yv, allsel, dd, 2000, seed=21)["sel_mean_ci"]
            ci_p = tr.day_block_bootstrap(yp, allsel, dd, 2000, seed=22)["sel_mean_ci"]
            means = [np.nanmean(yv[np.concatenate([idx_by_day[j] for j in rng.integers(0, len(days_u), len(days_u))])])
                     for _ in range(2000)]
            p_one = float((1 + np.sum(np.array(means) <= 0)) / 2001)
            wkm = [float(np.nanmean(yv[wk_ == w])) for w in np.unique(wk_)]
            st = sub[f"st_{i}"].value_counts(normalize=True).round(4).to_dict()
            grad = sub[f"st_{i}"].isin(["GRAD", "PART_GRAD"])
            r = {"entries": sname, "horizon_h": h, "tp": x, "partial": part, "n": int(len(sub)), "mean": float(np.nanmean(yv)),
                 "median": float(np.nanmedian(yv)), "ci": ci, "mean_pessimistic": float(np.nanmean(yp)),
                 "ci_pessimistic": ci_p, "p_one_sided": p_one, "weeks_positive": int(sum(v > 0 for v in wkm)),
                 "weeks": len(wkm), "status": st, "grad_mean_net": float(np.nanmean(yv[grad])) if grad.any() else None,
                 "time_exit_delay_median_s": float(np.nanmedian(sub[f"delay_{i}"])) if sub[f"delay_{i}"].notna().any() else None,
                 "p99_net": float(np.nanquantile(yv, 0.99))}
            key = f"{sname}_{i}"
            results[key] = r
            ids[key] = reg.register("H", f"H {sname} hold {config_label(h, x, part)}",
                                    "holding for hours with no stop loss is profitable", specs[(sname, i)],
                                    zone="discovery", metrics=r, p_value=p_one)
            log(f"  {ids[key]} {sname:5s} {config_label(h, x, part):28s}: mean {r['mean']:+.4f} "
                f"CI{[round(c, 4) for c in ci]} | pessimistic {r['mean_pessimistic']:+.4f} "
                f"CI{[round(c, 4) for c in ci_p]} | median {r['median']:+.4f} p99 {r['p99_net']:+.2f} "
                f"weeks>0 {r['weeks_positive']}/{r['weeks']} {st}")
    q = reg.bh_qvalues()
    log("\n  verdicts (primary CI>0 AND pessimistic CI>0 AND >=60% weeks positive AND q<0.10):")
    for key, r in results.items():
        ok = (r["ci"][0] > 0 and r["ci_pessimistic"][0] > 0 and r["weeks_positive"] >= 0.6 * r["weeks"]
              and q[ids[key]] < 0.10)
        reg.update(ids[key], status="passed_discovery" if ok else "screened_out", q_bh=q[ids[key]])
        log(f"    {ids[key]} {key}: q={q[ids[key]]:.4f} -> {'PASSED discovery' if ok else 'screened out'}")
    (out / "results.json").write_text(json.dumps({"results": results, "ids": ids}, indent=1, default=float),
                                      encoding="utf-8")
    log(f"  done in {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
