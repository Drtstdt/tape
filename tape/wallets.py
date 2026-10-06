"""D135/D136: family W -- point-in-time wallet track records and copy-trading
triggers. Pure functions; the scripts (build_wallet_ledger.py, run_family_W.py)
do the I/O.

Ledger (one row per wallet x token): the wallet's flows on that token inside
the token's first SETTLE_MS after create, and the position left over at the end
of that window marked at the token's last observed price in the window:

    pnl = sol_out - sol_in + max(tok_in - tok_out, 0) * p_last

  * sol_in  = buys' curve SOL + fee (what the wallet paid);
    sol_out = sells' quote_amount (what the wallet received, vendor semantics D126)
  * the record becomes usable at settle_ts = create_ts + SETTLE_MS -- every
    swap it summarises happened before that moment (no look-ahead);
  * a token that graduated inside the window is marked at its last bonding-curve
    price (the AMM leg is not in the data, D134).

Weekly snapshots (Monday 00:00 UTC): a wallet's score at snapshot S uses the
records with settle_ts <= S only:  score = sum(pnl) / (n + PRIOR_N), eligible
when n >= MIN_N. Classes by percentile of score among the eligible wallets:
top1 (>= 99th), top5 (>= 95th), placebo (40th-60th).
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
MONDAY0 = 4 * DAY_MS                  # 1970-01-05 00:00 UTC was a Monday
SETTLE_MS = DAY_MS                    # a token's first 24 h
PRIOR_N = 10
MIN_N = 10
CLASSES = {"top1": (0.99, 1.0), "top5": (0.95, 1.0), "placebo": (0.40, 0.60)}
MIN_TRIGGER_SOL = 0.1
MAX_TRIGGER_MS = 60 * 60_000
MIRROR_FRACTION = 0.5


def week_start(ts_ms):
    """Monday 00:00 UTC at or before ts (works on scalars and arrays)."""
    t = np.asarray(ts_ms, dtype="int64")
    return (t - MONDAY0) // WEEK_MS * WEEK_MS + MONDAY0


def first_snapshot_at_or_after(ts_ms):
    t = np.asarray(ts_ms, dtype="int64")
    ws = week_start(t)
    return np.where(ws == t, t, ws + WEEK_MS)


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def ledger_frame(df, create_ts: Dict[str, int], settle_ms: int = SETTLE_MS):
    """df: deduped swaps of many tokens, ordered (mint, ts, slot, ...), columns
    mint, wallet, side, ts_ms, quote_amount, fee_sol, base_amount, price.
    Returns one row per (wallet, mint) with flows inside [create, create+settle]."""
    import pandas as pd
    if len(df) == 0:
        return pd.DataFrame(columns=["wallet", "mint", "create_ts", "settle_ts", "sol_in", "sol_out",
                                     "tok_in", "tok_out", "p_last", "n_trades", "pnl"])
    d = df[["mint", "wallet", "side", "ts_ms", "quote_amount", "fee_sol", "base_amount", "price"]].copy()
    d["create_ts"] = d["mint"].map(create_ts)
    d = d[d["create_ts"].notna()]
    d["create_ts"] = d["create_ts"].astype("int64")
    d = d[(d["ts_ms"] >= d["create_ts"]) & (d["ts_ms"] <= d["create_ts"] + settle_ms)]
    # the token's last observed price inside its window (before dropping walletless rows)
    p_last = d.groupby("mint", sort=False)["price"].last()
    d = d[d["wallet"].notna() & (d["wallet"].astype(str) != "")]
    buy = (d["side"] == "buy").to_numpy()
    qa = d["quote_amount"].to_numpy(dtype=float)
    fee = np.nan_to_num(d["fee_sol"].to_numpy(dtype=float), nan=0.0)
    ba = d["base_amount"].to_numpy(dtype=float)
    d = d.assign(sol_in=np.where(buy, qa + fee, 0.0), sol_out=np.where(buy, 0.0, qa),
                 tok_in=np.where(buy, ba, 0.0), tok_out=np.where(buy, 0.0, ba))
    g = d.groupby(["wallet", "mint"], sort=False).agg(
        create_ts=("create_ts", "first"), sol_in=("sol_in", "sum"), sol_out=("sol_out", "sum"),
        tok_in=("tok_in", "sum"), tok_out=("tok_out", "sum"), n_trades=("side", "size")).reset_index()
    g["p_last"] = g["mint"].map(p_last).astype(float)
    g["settle_ts"] = g["create_ts"] + settle_ms
    left = np.maximum(g["tok_in"].to_numpy() - g["tok_out"].to_numpy(), 0.0)
    g["pnl"] = g["sol_out"] - g["sol_in"] + left * np.nan_to_num(g["p_last"].to_numpy(), nan=0.0)
    return g[["wallet", "mint", "create_ts", "settle_ts", "sol_in", "sol_out", "tok_in", "tok_out",
              "p_last", "n_trades", "pnl"]]


# ---------------------------------------------------------------------------
# weekly snapshots -> classes
# ---------------------------------------------------------------------------

def weekly_classes(wallet: np.ndarray, settle_ts: np.ndarray, pnl: np.ndarray, snapshots: Iterable[int],
                   prior_n: int = PRIOR_N, min_n: int = MIN_N,
                   classes: Dict[str, Tuple[float, float]] = CLASSES):
    """Returns (membership, stats):
      membership: {wallet: {class: bitmask over snapshot positions}}
      stats: list of per-snapshot dicts (snapshot, n_eligible, thresholds).
    A wallet is in a class at snapshot k iff, using records with settle_ts <=
    snapshots[k], it has n >= min_n and its score percentile lies in the class
    range (upper bound inclusive for the top classes)."""
    snaps = list(snapshots)
    codes, uniq = _factorize(wallet)
    order = np.argsort(settle_ts, kind="mergesort")
    st, cd, pn = settle_ts[order], codes[order], pnl[order]
    n_tot = np.zeros(len(uniq))
    p_tot = np.zeros(len(uniq))
    membership: Dict[str, Dict[str, int]] = {}
    stats = []
    pos = 0
    for k, s in enumerate(snaps):
        hi = int(np.searchsorted(st, s, side="right"))
        if hi > pos:
            np.add.at(n_tot, cd[pos:hi], 1.0)
            np.add.at(p_tot, cd[pos:hi], pn[pos:hi])
            pos = hi
        elig = np.flatnonzero(n_tot >= min_n)
        rec = {"snapshot": int(s), "n_eligible": int(len(elig))}
        if len(elig):
            score = p_tot[elig] / (n_tot[elig] + prior_n)
            pct = _percentile_rank(score)
            for cls, (lo, up) in classes.items():
                m = (pct >= lo) & ((pct <= up) if up >= 1.0 else (pct < up))
                rec[f"n_{cls}"] = int(m.sum())
                rec[f"min_score_{cls}"] = float(score[m].min()) if m.any() else float("nan")
                for w in uniq[elig[m]]:
                    membership.setdefault(w, {}).setdefault(cls, 0)
                    membership[w][cls] |= (1 << k)
        stats.append(rec)
    return membership, stats


def _factorize(values):
    uniq, codes = np.unique(np.asarray(values, dtype=object).astype(str), return_inverse=True)
    return codes, uniq


def _percentile_rank(x: np.ndarray) -> np.ndarray:
    """Share of eligible wallets with a strictly lower score, in [0, 1)."""
    s = np.sort(x)
    return np.searchsorted(s, x, side="left") / len(x)


def in_class(membership: Dict[str, Dict[str, int]], wallet, cls: str, snap_pos: int) -> bool:
    m = membership.get(wallet)
    return bool(m and (m.get(cls, 0) >> snap_pos) & 1)


# ---------------------------------------------------------------------------
# triggers and the mirror exit (inside one token's ordered tape)
# ---------------------------------------------------------------------------

def find_trigger(ts: np.ndarray, buy: np.ndarray, qa: np.ndarray, wallets: np.ndarray, create_ts: int,
                 membership, cls: str, snap_pos_of, min_sol: float = MIN_TRIGGER_SOL,
                 max_ms: int = MAX_TRIGGER_MS) -> Optional[int]:
    """Index of the first buy >= min_sol by a wallet of class `cls` (membership
    at the snapshot of that swap's week, `snap_pos_of(ts) -> position|None`)
    within max_ms of create; None if there is none."""
    for i in range(len(ts)):
        if ts[i] - create_ts > max_ms:
            break
        if not buy[i] or qa[i] < min_sol:
            continue
        w = wallets[i]
        if w is None or w != w:
            continue
        k = snap_pos_of(int(ts[i]))
        if k is not None and in_class(membership, w, cls, k):
            return i
    return None


def mirror_exit_index(buy: np.ndarray, ba: np.ndarray, wallets: np.ndarray, trig: int,
                      fraction: float = MIRROR_FRACTION) -> Optional[int]:
    """First sell by the trigger wallet after `trig` that brings its cumulative
    sells since the trigger to >= fraction of its holdings at the trigger
    (its buys minus sells up to and including the trigger swap)."""
    w = wallets[trig]
    mine = (wallets[:trig + 1] == w)
    hold = float(np.sum(np.where(buy[:trig + 1] & mine, ba[:trig + 1], 0.0))
                 - np.sum(np.where(~buy[:trig + 1] & mine, ba[:trig + 1], 0.0)))
    if not hold > 0:
        return None
    sold = 0.0
    for j in range(trig + 1, len(buy)):
        if wallets[j] == w and not buy[j]:
            sold += float(ba[j])
            if sold >= fraction * hold:
                return j
    return None


def pit_fee_ratios(buy: np.ndarray, qa: np.ndarray, fee: np.ndarray, upto: int,
                   default: float = 0.0125) -> Tuple[float, float]:
    """Median fee/quote ratio of buys and of sells at or before `upto`
    (point-in-time); sells fall back to the buy ratio, buys to `default`."""
    sl = slice(0, upto + 1)
    b, q, f = buy[sl], qa[sl], np.nan_to_num(fee[sl], nan=np.nan)
    ok = (q > 0) & np.isfinite(f)
    rb = f[ok & b] / q[ok & b]
    rs = f[ok & ~b] / q[ok & ~b]
    fb = float(np.median(rb)) if len(rb) else default
    fs = float(np.median(rs)) if len(rs) else fb
    return fb, fs
