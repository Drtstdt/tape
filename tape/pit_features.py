"""D124 (docs/DECISIONS.md) -- point-in-time features at the decision swap.

D126: `q_after` must be the STANDARD-curve virtual SOL reserve 30 + real
reserve (tape/curve.py) -- the vendor's virtual fields are unreliable -- and
rows must be in execution order (curve.order_within_slots).

`token_features(...)` receives arrays already CUT at the decision index (the
caller slices [:dec+1]); it never sees later swaps -- tests/test_research_set.py
checks that changing or removing every later swap leaves the features
bit-for-bit unchanged. Creator / regime context comes in as plain numbers the
caller computed from events strictly BEFORE the token's create time.

Families (EXPLORATION_PROMPT): A = tempo / age / curve progress, C = wallet
microstructure (same-slot buyers = bundle proxy, holder concentration, dev
behaviour), plus creator history and regime context. Every value is float;
NaN means "not measurable here", never 0.
"""

from __future__ import annotations

import math
from typing import Dict, Optional

import numpy as np

Q_INIT_SOL = 30.0   # pump.fun virtual SOL reserve at create (D93, verified constant)

FEATURE_NAMES = [
    # A: tempo / age / progress
    "secs_since_create", "n_swaps", "n_buys", "n_sells", "swaps_per_min", "sol_buy", "sol_sell",
    "net_flow_sol", "q_now", "q_gain_sol", "real_q_now", "ret_since_first", "ret_peak",
    "drawdown_from_peak", "ret_last10", "secs_last10", "buy_share_count", "buy_share_sol",
    # C: wallets / microstructure
    "unique_buyers", "unique_sellers", "repeat_buyer_share", "first_slot_buyers", "first_slot_sol",
    "same_slot_max_buyers", "top1_hold_share", "top5_hold_share", "dev_buy_sol", "dev_sold_sol",
    "dev_hold_share", "largest_buy_sol", "median_buy_sol",
    # token / cost
    "fee_ratio", "fee_buy_ratio", "fee_sell_ratio", "curve_std_share", "is_mayhem",
    # creator history (computed by caller from events before create)
    "creator_prior_launches", "creator_prior_grads", "creator_prior_grad_rate",
    "creator_launches_24h",
    # regime / time
    "regime_creates_prev_hour", "hour_utc", "dow",
]


def token_features(ts, side_buy, quote_amt, base_amt, wallet, slot, q_after, real_q_after,
                   price, fee_sol, create_ts: int, creator: Optional[str], is_mayhem,
                   creator_ctx: Dict[str, float], regime_ctx: Dict[str, float]) -> Dict[str, float]:
    nan = float("nan")
    n = len(ts)
    f: Dict[str, float] = {k: nan for k in FEATURE_NAMES}
    if n == 0:
        return f
    t_dec = int(ts[-1])
    buys = np.asarray(side_buy, dtype=bool)
    qa = np.asarray(quote_amt, dtype=float)
    ba = np.asarray(base_amt, dtype=float)
    secs = max((t_dec - create_ts) / 1000.0, 0.0)
    f["secs_since_create"] = secs
    f["n_swaps"] = float(n)
    f["n_buys"] = float(buys.sum())
    f["n_sells"] = float(n - buys.sum())
    f["swaps_per_min"] = n / max(secs / 60.0, 1.0 / 60.0)
    f["sol_buy"] = float(qa[buys].sum())
    f["sol_sell"] = float(qa[~buys].sum())
    f["net_flow_sol"] = f["sol_buy"] - f["sol_sell"]
    q = np.asarray(q_after, dtype=float)
    f["q_now"] = float(q[-1])
    f["q_gain_sol"] = float(q[-1] - Q_INIT_SOL)
    rq = np.asarray(real_q_after, dtype=float)
    f["real_q_now"] = float(rq[-1]) if len(rq) else nan
    p = np.asarray(price, dtype=float)
    pv = p[p > 0]
    if len(pv):
        f["ret_since_first"] = float(pv[-1] / pv[0] - 1.0)
        f["ret_peak"] = float(pv.max() / pv[0] - 1.0)
        f["drawdown_from_peak"] = float(pv[-1] / pv.max() - 1.0)
        if len(pv) > 10:
            f["ret_last10"] = float(pv[-1] / pv[-11] - 1.0)
    if n > 10:
        f["secs_last10"] = (t_dec - int(ts[-11])) / 1000.0
    f["buy_share_count"] = float(buys.mean())
    tot = qa.sum()
    f["buy_share_sol"] = float(qa[buys].sum() / tot) if tot > 0 else nan

    w = np.asarray(wallet, dtype=object)
    has_w = np.array([x is not None and x == x for x in w])
    if has_w.any():
        bw = w[buys & has_w]
        sw = w[~buys & has_w]
        ub = set(bw)
        f["unique_buyers"] = float(len(ub))
        f["unique_sellers"] = float(len(set(sw)))
        if len(bw):
            vals, cnt = np.unique(bw.astype(str), return_counts=True)
            f["repeat_buyer_share"] = float((cnt > 1).mean())
        sl = np.asarray(slot)
        first = sl[0]
        fs = (sl == first) & buys & has_w
        f["first_slot_buyers"] = float(len(set(w[fs]) - ({creator} if creator else set())))
        f["first_slot_sol"] = float(qa[(sl == first) & buys].sum())
        mx = 0
        for s_ in np.unique(sl[buys & has_w]):
            mx = max(mx, len(set(w[(sl == s_) & buys & has_w])))
        f["same_slot_max_buyers"] = float(mx)
        hold: Dict[str, float] = {}
        for wi, b, amt in zip(w[has_w], buys[has_w], ba[has_w]):
            hold[wi] = hold.get(wi, 0.0) + (amt if b else -amt)
        pos = sorted((v for v in hold.values() if v > 0), reverse=True)
        tot_h = sum(pos)
        if tot_h > 0:
            f["top1_hold_share"] = pos[0] / tot_h
            f["top5_hold_share"] = sum(pos[:5]) / tot_h
            if creator:
                f["dev_hold_share"] = max(hold.get(creator, 0.0), 0.0) / tot_h
        if creator:
            isdev = np.array([x == creator for x in w])
            f["dev_buy_sol"] = float(qa[isdev & buys].sum())
            f["dev_sold_sol"] = float(qa[isdev & ~buys].sum())
    if buys.any():
        f["largest_buy_sol"] = float(qa[buys].max())
        f["median_buy_sol"] = float(np.median(qa[buys]))

    fs_ = np.asarray(fee_sol, dtype=float)
    ok = (qa > 0) & np.isfinite(fs_)
    f["fee_ratio"] = float(np.median(fs_[ok] / qa[ok])) if ok.any() else nan
    if (ok & buys).any():
        f["fee_buy_ratio"] = float(np.median(fs_[ok & buys] / qa[ok & buys]))
    if (ok & ~buys).any():
        f["fee_sell_ratio"] = float(np.median(fs_[ok & ~buys] / qa[ok & ~buys]))
    from .curve import signed_flow, k_share, V_STD, K_STD
    rqa = np.asarray(real_q_after, dtype=float)
    if len(rqa) == n:
        share, n_used = k_share(rqa - signed_flow(buys, qa, fs_), rqa, ba, V_STD, K_STD)
        f["curve_std_share"] = share
    f["is_mayhem"] = nan if is_mayhem is None or (isinstance(is_mayhem, float) and math.isnan(is_mayhem)) \
        else float(bool(is_mayhem))
    for k in ("creator_prior_launches", "creator_prior_grads", "creator_prior_grad_rate",
              "creator_launches_24h"):
        f[k] = float(creator_ctx.get(k, nan))
    f["regime_creates_prev_hour"] = float(regime_ctx.get("regime_creates_prev_hour", nan))
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(t_dec / 1000, tz=_dt.timezone.utc)
    f["hour_utc"] = float(d.hour)
    f["dow"] = float(d.weekday())
    return f


def creator_history(create_ts: np.ndarray, creators: np.ndarray, bonding_ts: np.ndarray) -> Dict[str, np.ndarray]:
    """For every token (row), counts over the SAME creator's tokens created
    strictly earlier: launches, launches in the previous 24 h, and how many of
    them had graduated (bonding_ts) strictly before this token's create time.
    bonding_ts: NaN when never seen. Only events visible in collected hours
    exist -- the same limitation live (D121)."""
    n = len(create_ts)
    prior = np.full(n, np.nan)
    prior24 = np.full(n, np.nan)
    grads = np.full(n, np.nan)
    cr = np.array([("" if c is None or (isinstance(c, float) and c != c) else str(c)) for c in creators])
    order = np.lexsort((create_ts, cr))
    cr_sorted = cr[order]
    bounds = np.flatnonzero(cr_sorted[1:] != cr_sorted[:-1]) + 1
    for s, e in zip(np.concatenate([[0], bounds]), np.concatenate([bounds, [n]])):
        idx = order[s:e]
        if cr[idx[0]] == "":
            continue                                   # unknown creator -> NaN
        ct = create_ts[idx].astype(float)              # sorted ascending
        bt = np.sort(bonding_ts[idx].astype(float))
        bt = bt[np.isfinite(bt)]
        p_ = np.searchsorted(ct, ct, side="left")     # strictly earlier launches
        prior[idx] = p_
        prior24[idx] = p_ - np.searchsorted(ct, ct - 86_400_000, side="left")
        # a token graduates after its own create, so "graduated before ct" can only
        # be an EARLIER token of this creator
        grads[idx] = np.searchsorted(bt, ct, side="left")
    rate = np.where(prior > 0, grads / np.maximum(prior, 1), np.nan)
    return {"creator_prior_launches": prior, "creator_prior_grads": grads,
            "creator_prior_grad_rate": rate, "creator_launches_24h": prior24}


def creates_prev_hour(create_ts: np.ndarray, all_creates_sorted: np.ndarray, collected: set) -> np.ndarray:
    """Number of creates in [t-1h, t); NaN unless that whole window was collected."""
    lo = np.searchsorted(all_creates_sorted, create_ts - 3_600_000, side="left")
    hi = np.searchsorted(all_creates_sorted, create_ts, side="left")
    out = (hi - lo).astype(float)
    h_now = create_ts // 3_600_000
    ok = np.array([(h - 1) in collected and h in collected for h in h_now])
    out[~ok] = np.nan
    return out
