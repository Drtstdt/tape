#!/usr/bin/env python3
"""D147: family R stage 2 -- the metadata rails (socials, Twitter link type and
reuse, copycat name) added to the stage-1 replay, and the ONE remaining trial:

  R003  the live learner walked forward week by week (exactly as R000) with ALL
        rails: stage-1 on-chain rails + the live metadata rails (live defaults:
        >= 1 social, no link / X handle reused by > 2 other tokens in 7 days, no
        name+symbol copy in 24 h; tape/pumplive/rails.create_rails, point-in-time
        via replay.meta_rails_in_order). Gate: champion trades pooled, day-block
        95% CI > 0 AND one-sided p < 0.05/4 (family R, 4 trials).

Universe: stage-1 decisions whose token metadata state is KNOWN today:
JSON fetched (ok), or really absent (no uri on chain / the uri is not JSON ->
'no_metadata', live fails closed the same way). Tokens whose JSON could not be
fetched now ('fail', DAS missing/error) are EXCLUDED (unknown), and their share
and mean are reported. Registered before any outcome statistic.

    python scripts\\fetch_token_meta.py           (first)
    python scripts\\replay_stage2.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay_pumplive as rpl  # noqa: E402
from fetch_token_meta import done_mints  # noqa: E402
from tape import pf_store as ps  # noqa: E402
from tape.pumplive import replay as rp  # noqa: E402
from tape.pumplive.engine import Settings  # noqa: E402
from tape.pumplive.meta import twitter_kind  # noqa: E402

N_FAMILY = 4
R003_NAME = "R learner walk-forward (all rails incl. metadata)"


def known_metas(das: dict, ipfs: dict) -> dict:
    """mint -> JSON fields | None (really no metadata); unknown mints are absent (pure, tested)."""
    out = {}
    for m, d in das.items():
        if d.get("status") != "ok":
            continue                                       # DAS missing/error: unknown
        if not d.get("uri"):
            out[m] = None                                  # on-chain metadata without uri
            continue
        r = ipfs.get(m)
        if r is None or r.get("status") == "fail":
            continue                                       # not fetchable today: unknown
        out[m] = None if r.get("status") in ("bad_json", "no_uri") else \
            {k: r.get(k) for k in ("twitter", "telegram", "website")}
    return out


def walk_forward(dts, day, nets, take, close, s, cids, log):
    z = rp.learner_z(len(cids))
    win = int(s.window_days * rp.DAY_MS)
    weeks = sorted(set(((dts - rp.MONDAY0) // rp.WEEK_MS).tolist()))
    got, got_day, champs, nb_net, nb_day = [], [], [], [], []
    t1 = time.time()
    for i, w in enumerate(weeks, start=1):
        ws = w * rp.WEEK_MS + rp.MONDAY0
        cidx, rows = rp.champion_at(ws, close, nets, take, z, win, s.min_trades)
        in_w = (dts >= ws) & (dts < ws + rp.WEEK_MS)
        msg = f"    week {i}/{len(weeks)} {datetime.fromtimestamp(ws / 1000, tz=timezone.utc).date()}: "
        if cidx is None:
            msg += "ABSTAIN" + (f" (best LCB {rows[0][5]:+.4f} {cids[rows[0][0]]} n={rows[0][1]})" if rows else "")
        else:
            m = in_w & take[:, cidx] & np.isfinite(nets[:, cidx])
            got.extend(nets[m, cidx].tolist())
            got_day.extend(day[m].tolist())
            champs.append(cids[cidx])
            lcb_c = next(r[5] for r in rows if r[0] == cidx)
            msg += (f"champion {cids[cidx]} (LCB {lcb_c:+.4f}) -> next week n={m.sum()} "
                    f"mean {np.nanmean(nets[m, cidx]) if m.any() else float('nan'):+.4f}")
        nb = [r for r in rows if r[1] >= s.min_trades]
        if nb:
            cb = max(nb, key=lambda r: r[3])[0]
            m = in_w & take[:, cb] & np.isfinite(nets[:, cb])
            nb_net.extend(nets[m, cb].tolist())
            nb_day.extend(day[m].tolist())
        el = time.time() - t1
        log(msg + f"  [elapsed {el:.0f}s ETA~{el / i * (len(weeks) - i):.0f}s]")
    return weeks, got, got_day, champs, nb_net, nb_day


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--replay", default=r"E:\tape_research\replay_R\discovery")
    ap.add_argument("--meta", default=r"E:\tape_research\token_meta")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    a = ap.parse_args(argv)
    import pandas as pd
    from tape.registry import Registry
    from tape import trials as tr

    s = Settings()                                         # FROZEN live defaults (D144)
    out_dir = Path(a.replay)
    lf = open(out_dir / "stage2_log.txt", "a", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    try:
        t0 = time.time()
        log(f"\n=== replay_stage2 {datetime.now(timezone.utc).isoformat()} ===")
        parts = sorted(out_dir.glob("part-b*.parquet"))
        if len(parts) < ps.N_BUCKETS:
            log(f"STOP: only {len(parts)} replay parts -- run replay_pumplive.py first")
            return 4
        das = done_mints(Path(a.meta) / "das.jsonl")
        ipfs = done_mints(Path(a.meta) / "ipfs.jsonl")
        df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        df = df.sort_values(["decision_ts", "mint", "K"]).reset_index(drop=True)
        toks = df.drop_duplicates("mint")[["mint", "create_ts"]].sort_values("create_ts")
        n_tok = len(toks)
        rail_cols = [f"r_{r}" for r in rp.ALL_RAILS]
        kept_m = set(df.loc[~df[rail_cols].any(axis=1), "mint"])
        attempted = lambda m: m in ipfs or (m in das and not (das.get(m) or {}).get("uri"))  # noqa: E731
        cov_k = sum(1 for m in kept_m if m in das and attempted(m))
        cov_all = sum(1 for m in toks["mint"] if attempted(m))
        log(f"  fetch coverage: stage-1-kept tokens {cov_k:,}/{len(kept_m):,}; all tokens (reuse index) "
            f"{cov_all:,}/{n_tok:,} = {cov_all / n_tok:.1%}")
        if cov_k < 0.99 * len(kept_m):
            log("STOP: metadata of the stage-1-kept tokens incomplete -- let scripts\\fetch_token_meta.py "
                "finish them first (they are fetched first)")
            return 5
        metas = known_metas(das, ipfs)
        log(f"  tokens {n_tok:,}: metadata known {sum(1 for m in toks['mint'] if m in metas):,} "
            f"(JSON {sum(1 for m in toks['mint'] if metas.get(m)):,}, really none "
            f"{sum(1 for m in toks['mint'] if m in metas and metas[m] is None):,}); unknown today "
            f"{sum(1 for m in toks['mint'] if m not in metas):,}")

        reg = Registry(a.registry)
        have = {t["name"]: tid for tid, t in reg.trials().items() if t["family"] == "R"}
        if R003_NAME in have:
            tid = have[R003_NAME]
            log(f"  R003: already registered as {tid} (re-analysis of the same frozen spec)")
        else:
            tid = reg.register("R", R003_NAME, "the live learner's weekly champion with all rails (on-chain + "
                               "metadata), chosen only from earlier trades, nets > 0 the next week",
                               {"settings": asdict(s), "stage": 2, "meta_rails": list(rp.META_RAILS),
                                "onchain_rails": list(rp.ALL_RAILS), "unknown_metadata": "excluded",
                                "gate": f"CI > 0 and p < 0.05/{N_FAMILY}"}, "discovery")
            log(f"  R003: registered as {tid} BEFORE any outcome statistic")

        names = {m: (das.get(m) or {}) for m in toks["mint"]}
        order = [(m, int(ct), "", names[m].get("name"), names[m].get("symbol"))
                 for m, ct in zip(toks["mint"], toks["create_ts"])]
        t1 = time.time()
        mr = rp.meta_rails_in_order(s, order, metas)
        log(f"  metadata rails computed point-in-time for {len(mr):,} tokens ({time.time() - t1:.0f}s)")
        known = df["mint"].isin(set(mr)).to_numpy()
        for r in rp.META_RAILS:
            df[f"m_{r}"] = [r in mr.get(m, ()) for m in df["mint"]]
        df["tw_kind"] = [twitter_kind((metas.get(m) or {}).get("twitter")) if m in metas else "unknown"
                         for m in df["mint"]]

        nets, elig, rails1, close = rpl.build_matrix(df, s)
        meta_ok = ~df[[f"m_{r}" for r in rp.META_RAILS]].to_numpy().any(axis=1)
        E = len(rp.exit_combos(s))
        y = np.nanmean(nets[:, :E], axis=1)
        day = df["day"].to_numpy()
        dts = df["decision_ts"].to_numpy(dtype=np.int64)
        log(f"\n  UNIVERSE: decisions {len(df):,}; metadata known {known.mean():.1%} "
            f"(y known {np.nanmean(y[known]):+.4f}, unknown {np.nanmean(y[~known]) if (~known).any() else float('nan'):+.4f})")
        base = known & rails1
        log(f"  METADATA RAILS on decisions kept by the stage-1 rails and with known metadata "
            f"(n={base.sum():,}, y={np.nanmean(y[base]):+.4f}):")
        for r in rp.META_RAILS:
            v = df[f"m_{r}"].to_numpy() & base
            k_ = base & ~df[f"m_{r}"].to_numpy()
            log(f"    {r:24s} veto {v.sum() / max(base.sum(), 1):6.2%}  y vetoed "
                f"{np.nanmean(y[v]) if v.any() else float('nan'):+.4f}  kept {np.nanmean(y[k_]):+.4f}")
        full = base & meta_ok
        log(f"    ALL RAILS                keep {full.sum() / max(base.sum(), 1):6.2%} of those  "
            f"y kept {np.nanmean(y[full]):+.4f}  (n={full.sum():,})")
        log("  y by Twitter link kind (stage-1 kept, known metadata):")
        for kd, g in df[base].groupby("tw_kind"):
            log(f"    {kd:10s} n={len(g):7,d}  y={np.nanmean(y[g.index.to_numpy()]):+.4f}")
        okf = np.isfinite(y) & base
        bb = tr.day_block_bootstrap(y[okf], full[okf], day[okf], 2000, seed=81)
        log(f"  diagnostic: metadata rails on top of stage 1: diff {np.nanmean(y[full]) - np.nanmean(y[base]):+.4f} "
            f"CI{[round(v, 4) for v in bb['diff_ci']]}")

        cids = rp.config_ids(s)
        take = elig & full[:, None]
        z = rp.learner_z(len(cids))
        st = []
        for c in range(len(cids)):
            m = take[:, c] & np.isfinite(nets[:, c])
            st.append((c, *rp.lcb_stats(nets[m, c], close[m, c] // rp.DAY_MS, z)))
        st.sort(key=lambda r: -r[5])
        log("\n  FIXED CONFIGS, all rails, whole discovery (in-sample diagnostic) -- top 10 by LCB:")
        for c, n, D, mean, se, lcb in st[:10]:
            log(f"    {cids[c]:32s} n={n:6,d} days={D:3d} mean={mean:+.4f} se={se:.4f} LCB={lcb:+.4f}")
        log(f"    configs with mean > 0: {sum(1 for r in st if r[3] > 0)}/{len(st)}; LCB > 0: "
            f"{sum(1 for r in st if r[5] > 0)}")

        log(f"\n  R003 LEARNER WALK-FORWARD, all rails (window {s.window_days:.0f} d, n >= {s.min_trades}, >= 7 days, "
            f"Bonferroni-144 LCB > 0 else ABSTAIN):")
        weeks, got, got_day, champs, nb_net, nb_day = walk_forward(dts, day, nets, take, close, s, cids, log)
        if len(got) >= 30:
            mean0, ci0, p0 = rpl.day_boot(np.array(got), np.array(got_day), seed=83)
            passed = ci0[0] > 0 and p0 < 0.05 / N_FAMILY
            log(f"  R003: {len(champs)} weeks with a champion, n={len(got)} mean {mean0:+.4f} "
                f"CI{[round(v, 4) for v in ci0]} p={p0:.4f} -> {'PASS' if passed else 'fail'}")
            reg.update(tid, status="passed_discovery" if passed else "screened_out",
                       metrics={"weeks_with_champion": len(champs), "champions": champs, "n": len(got),
                                "mean": mean0, "ci": ci0, "meta_known_share": float(known.mean())}, p_value=p0)
        else:
            log(f"  R003: ABSTAIN in {len(weeks) - len(champs)} of {len(weeks)} weeks; {len(got)} trades -> no strategy")
            reg.update(tid, status="screened_out", metrics={"weeks_with_champion": len(champs), "n": len(got),
                                                            "meta_known_share": float(known.mean())}, p_value=1.0)
        if len(nb_net) >= 30:
            mb, cib, _ = rpl.day_boot(np.array(nb_net), np.array(nb_day), seed=84)
            log(f"  diagnostic (NOT a trial): learner WITHOUT the brake: n={len(nb_net)} mean {mb:+.4f} "
                f"CI{[round(v, 4) for v in cib]}")
        q = reg.bh_qvalues(["R"])
        log(f"  family R BH q-values: {', '.join(f'{k} {v:.4f}' for k, v in sorted(q.items()))}")
        (out_dir / "stage2_summary.json").write_text(json.dumps(
            {"utc": datetime.now(timezone.utc).isoformat(), "trial": tid, "registry": reg.trials()[tid]},
            indent=1, default=str), encoding="utf-8")
        log(f"  done in {time.time() - t0:.0f}s")
        return 0
    finally:
        lf.close()


if __name__ == "__main__":
    raise SystemExit(main())
