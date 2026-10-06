#!/usr/bin/env python3
"""D125: why does chk_curve_amount_match sit at ~0.19 instead of ~1 (D124)?

Read-only, ~1-3 min. Tests, on a random sample of DISCOVERY tokens, which
semantics the vendor columns actually have -- each hypothesis is a measured
match rate, nothing is assumed:

  after        q[i] - q[i-1]   == +-amount[i]        (reserves AFTER the swap; amount = curve flow)
  before       q[i+1] - q[i]   == +-amount[i]        (reserves BEFORE the swap)
  after_fee_in buy: dq == amount - fee ; sell: dq == -(amount + fee)
  after_fee_out buy: dq == amount/(1+r) ; sell: dq == -amount/(1-r)   (r = fee/amount)
  tokens_after t[i] - t[i-1]   == -+base[i]
  chain        q[i] - s*amount[i] equals SOME other post-state of the token (or the
               initial 30 SOL): amounts are curve flows, only the ORDER is unknown
  split by same-slot vs different-slot consecutive pairs (intra-slot order is
  not in the data: rows are sorted by ts, slot, sig)

Plus: initial reserve implied by the first swap; tokens whose k = q*t is not
constant and what the jump looks like; a few raw example rows.

    python scripts\\diag_curve_semantics.py
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape import pf_store as ps  # noqa: E402
from tape import zones as zn  # noqa: E402


def close(a, b, rel=1e-6, abs_=1e-9):
    return np.isclose(a, b, rtol=rel, atol=abs_)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--zones", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--out", default=r"E:\tape_research\diag_curve")
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    import pandas as pd

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lf = open(out / "diag_log.txt", "w", encoding="utf-8")

    def log(m=""):
        print(m, flush=True)
        lf.write(m + "\n")
        lf.flush()

    t0 = time.time()
    mints = zn.load_zone(a.zones, "discovery")
    sample = random.Random(a.seed).sample(mints, min(a.n, len(mints)))
    con = ps.open_connection(memory_limit="4GB", threads=4, temp_dir=str(Path(a.store) / "_duckdb_tmp"))
    df = ps.fetch_frame(con, a.store, sample)
    log(f"diag_curve_semantics: {len(sample)} discovery tokens, {len(df):,} swaps ({time.time() - t0:.0f}s)")
    ev = ps.load_events(a.store)
    may = (ev[ev["event_type"] == "create"].drop_duplicates("mint").set_index("mint")["is_mayhem_mode"])

    agg = {k: [0, 0] for k in ("after", "before", "after_fee_in", "after_fee_out", "tokens_after", "chain",
                               "after_sameslot", "after_diffslot", "chain_sameslot", "chain_diffslot")}
    q_init, k_bad, examples_k, rel_err = [], [], [], []
    sameslot_share = []
    for mint, g in df.groupby("mint", sort=False):
        n = len(g)
        if n < 3:
            continue
        s = np.where(g["side"].to_numpy() == "buy", 1.0, -1.0)
        amt = g["quote_amount"].to_numpy(float)
        base = g["base_amount"].to_numpy(float)
        fee = g["fee_sol"].to_numpy(float)
        q = g["quote_reserve_after"].to_numpy(float)
        t = g["base_reserve_after"].to_numpy(float)
        slot = g["slot"].to_numpy()
        dq = np.diff(q)
        same = slot[1:] == slot[:-1]
        sameslot_share.append(same.mean())

        def acc(key, m):
            agg[key][0] += int(m.sum())
            agg[key][1] += int(m.size)

        m_after = close(dq, s[1:] * amt[1:])
        acc("after", m_after)
        acc("after_sameslot", m_after[same])
        acc("after_diffslot", m_after[~same])
        acc("before", close(dq, s[:-1] * amt[:-1]))
        acc("after_fee_in", close(dq, np.where(s[1:] > 0, amt[1:] - fee[1:], -(amt[1:] + fee[1:]))))
        r = np.where(amt > 0, fee / np.where(amt > 0, amt, 1), 0)
        acc("after_fee_out", close(dq, np.where(s[1:] > 0, amt[1:] / (1 + r[1:]), -amt[1:] / (1 - r[1:]))))
        acc("tokens_after", close(np.diff(t), -s[1:] * base[1:]))
        pre = q - s * amt
        states = np.concatenate([q, [30.0]])
        srt = np.sort(states)
        pos = np.clip(np.searchsorted(srt, pre), 1, len(srt) - 1)
        ch = np.minimum(np.abs(srt[pos] - pre), np.abs(srt[pos - 1] - pre)) <= np.maximum(1e-9, 1e-6 * np.abs(pre))
        acc("chain", ch)
        acc("chain_sameslot", ch[1:][same])
        acc("chain_diffslot", ch[1:][~same])
        rel_err.extend(list(np.abs(dq[~same & ~m_after] - (s[1:] * amt[1:])[~same & ~m_after])
                            / np.maximum(amt[1:][~same & ~m_after], 1e-12)))
        q_init.append((pre[0], bool(may.get(mint, False)) if may.get(mint, None) is not None else None))
        k = q * t
        spread = (k.max() - k.min()) / np.median(k) if np.median(k) > 0 else np.nan
        if not (spread < 1e-6):
            k_bad.append((mint, spread, n))
            if len(examples_k) < 3:
                j = int(np.argmax(np.abs(np.diff(k)) / np.median(k))) + 1
                examples_k.append(g.iloc[max(0, j - 3):j + 3])

    log(f"\n-- match rates over consecutive swap pairs (sample) --")
    for k, (hit, tot) in agg.items():
        log(f"  {k:16s} {hit / max(tot, 1):7.4f}   ({hit:,}/{tot:,})")
    log(f"  share of consecutive pairs in the SAME slot: median per token {np.median(sameslot_share):.3f}")
    if rel_err:
        log(f"  diff-slot pairs that do NOT match 'after': |dq - amount|/amount quantiles "
            f"p10={np.quantile(rel_err, .1):.4g} p50={np.quantile(rel_err, .5):.4g} p90={np.quantile(rel_err, .9):.4g}")
    qi = np.array([x[0] for x in q_init])
    log(f"\n-- implied reserve BEFORE the first swap (expect 30 SOL) --")
    log(f"  quantiles: p01={np.quantile(qi, .01):.4f} p50={np.quantile(qi, .5):.4f} p99={np.quantile(qi, .99):.4f}; "
        f"share within 1e-6 of 30: {np.mean(np.isclose(qi, 30.0, atol=1e-6)):.3f}")
    for flag in (True, False):
        sub = np.array([x[0] for x in q_init if x[1] is flag])
        if len(sub):
            log(f"  mayhem={flag}: n={len(sub)} median={np.median(sub):.4f} share~30={np.mean(np.isclose(sub, 30.0, atol=1e-6)):.3f}")
    log(f"\n-- k = q*t constancy --")
    log(f"  tokens with k spread >= 1e-6: {len(k_bad)} of {df['mint'].nunique()} "
        f"(spread quantiles of those: {np.quantile([x[1] for x in k_bad], [.1, .5, .9]).round(4).tolist() if k_bad else []})")
    cols = ["ts_ms", "slot", "sig", "side", "quote_amount", "fee_sol", "quote_reserve_after",
            "base_amount", "base_reserve_after", "real_quote_reserve_after", "src"]
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 20)
    for i, ex in enumerate(examples_k):
        log(f"\n  k-jump example {i + 1} (mint {ex['mint'].iloc[0]}):")
        for line in ex[cols].assign(sig=ex["sig"].str[:10]).to_string(index=False).splitlines():
            log("    " + line)
    log("\n-- raw first rows of 3 tokens --")
    for mint in list(df["mint"].unique())[:3]:
        g = df[df["mint"] == mint].head(12)
        log(f"  mint {mint}")
        for line in g[cols].assign(sig=g["sig"].str[:10]).to_string(index=False).splitlines():
            log("    " + line)
    log(f"\ndone in {time.time() - t0:.0f}s -> {out / 'diag_log.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
