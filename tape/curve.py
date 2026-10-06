"""D126 (docs/DECISIONS.md) -- curve state reconstructed from the fields that
are actually consistent, per the D125 diagnosis.

Measured on 3,000 discovery tokens (diag_curve_semantics, D126):
  * `real_quote_reserve_after` chains exactly when the swap's signed curve
    flow is  buy: +lamports_amount,  sell: -(lamports_amount + fee_lamports)
    (the vendor's lamports_amount is what the USER pays / receives; the fee
    sits on top for buys and comes out of the curve for sells);
  * rows inside one slot are NOT in execution order (sorted by signature);
    the order is recoverable by chaining real reserves;
  * the vendor's VIRTUAL reserve fields are wrong for a minority of tokens
    (virtual SOL below the 30 SOL start, token reserve above the 1.073e9
    start, k = Q*T jumping) -- they are not used for anything any more.

So: order each slot by chaining real reserves; curve parameters (virtual SOL
offset V and invariant k) are FITTED per token from real-reserve changes and
token amounts (standard pump.fun: V = 30, k = 30 * 1.073e9); the curve state
is then exact: Q = V + real, T = k / Q.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

V_STD = 30.0
K_STD = 30.0 * 1.073e9


def signed_flow(side_buy: np.ndarray, amt: np.ndarray, fee: np.ndarray) -> np.ndarray:
    """Change of the curve's REAL SOL reserve caused by each swap."""
    fee = np.nan_to_num(np.asarray(fee, dtype=float), nan=0.0)
    amt = np.asarray(amt, dtype=float)
    return np.where(side_buy, amt, -(amt + fee))


def order_within_slots(slot: np.ndarray, side_buy: np.ndarray, amt: np.ndarray, fee: np.ndarray,
                       real_after: np.ndarray, start_real: float = 0.0,
                       tol_abs: float = 2e-9, tol_rel: float = 1e-7) -> Tuple[np.ndarray, float]:
    """Permutation of row indices (rows already sorted by ts, slot, sig) that puts
    every slot into execution order: each swap's pre-state (real_after - flow)
    must equal the previous swap's real_after. Rows of different slots are
    never reordered. Returns (perm, share of swaps whose pre-state matched).
    A swap with no exact predecessor is placed by the closest pre-state and
    counted as unmatched -- the share is the evidence, not a hidden fix."""
    n = len(slot)
    flow = signed_flow(side_buy, amt, fee)
    pre = np.asarray(real_after, dtype=float) - flow
    perm = np.empty(n, dtype=np.int64)
    matched = 0
    state = float(start_real)
    i = 0
    out = 0
    slot = np.asarray(slot)
    while i < n:
        j = i
        while j + 1 < n and slot[j + 1] == slot[i]:
            j += 1
        rem = list(range(i, j + 1))
        while rem:
            d = [abs(pre[r] - state) for r in rem]
            kbest = int(np.argmin(d))
            r = rem.pop(kbest)
            if d[kbest] <= max(tol_abs, tol_rel * max(abs(state), 1.0)):
                matched += 1
            perm[out] = r
            out += 1
            state = float(real_after[r])
        i = j + 1
    return perm, (matched / n if n else float("nan"))


def fit_curve(real_pre: np.ndarray, real_post: np.ndarray, tokens: np.ndarray,
              min_flow_sol: float = 0.01, max_swaps: int = 400) -> Tuple[float, float, float, int]:
    """Fit virtual SOL offset V and invariant k from (real_pre, real_post, |token
    amount|) of swaps: |k (1/(V+pre) - 1/(V+post))| = tokens. Returns
    (V, k, share of fitted swaps within 1e-3 of k, n swaps used). Falls back
    to the standard curve when there is too little to fit (n < 3)."""
    pre = np.asarray(real_pre, dtype=float)
    post = np.asarray(real_post, dtype=float)
    tok = np.abs(np.asarray(tokens, dtype=float))
    use = (np.abs(post - pre) >= min_flow_sol) & (tok > 0) & np.isfinite(pre) & np.isfinite(post)
    idx = np.flatnonzero(use)
    if len(idx) < 3:
        return V_STD, K_STD, float("nan"), int(len(idx))
    if len(idx) > max_swaps:
        idx = idx[np.argsort(-np.abs(post - pre)[idx])[:max_swaps]]
    p, q, t = pre[idx], post[idx], tok[idx]
    lo = max(1e-3, -min(p.min(), q.min()) + 1e-6)

    def disp(V):
        d = np.abs(1.0 / (V + p) - 1.0 / (V + q))
        lk = np.log(t / d)
        return np.median(np.abs(lk - np.median(lk)))

    grid = np.geomspace(max(lo, 0.5), 2000.0, 500)
    vals = np.array([disp(v) for v in grid])
    b = int(np.argmin(vals))
    a_, c_ = grid[max(b - 1, 0)], grid[min(b + 1, len(grid) - 1)]
    fine = np.linspace(a_, c_, 400)
    fv = np.array([disp(v) for v in fine])
    V = float(fine[int(np.argmin(fv))])
    d = np.abs(1.0 / (V + p) - 1.0 / (V + q))
    ks = t / d
    k = float(np.median(ks))
    share = float(np.mean(np.abs(ks / k - 1.0) < 1e-3))
    return V, k, share, int(len(idx))


def is_standard(V: float, k: float) -> bool:
    return abs(V - V_STD) < 0.05 and abs(k / K_STD - 1.0) < 1e-3


def k_share(real_pre, real_post, tokens, V: float, k: float, min_flow_sol: float = 0.01) -> Tuple[float, int]:
    """Share of swaps (flow >= min_flow_sol) whose token amount matches curve (V, k) within 1e-3."""
    pre = np.asarray(real_pre, dtype=float)
    post = np.asarray(real_post, dtype=float)
    tok = np.abs(np.asarray(tokens, dtype=float))
    use = (np.abs(post - pre) >= min_flow_sol) & (tok > 0)
    if not use.any():
        return float("nan"), 0
    pred = np.abs(k * (1.0 / (V + pre[use]) - 1.0 / (V + post[use])))
    return float(np.mean(np.abs(pred / tok[use] - 1.0) < 1e-3)), int(use.sum())


def curve_params(real_pre, real_post, tokens) -> dict:
    """Standard pump.fun curve if it explains the swaps (>= 99%), else a fit.
    `usable` = the chosen parameters explain >= 99% of the fitted swaps."""
    s_std, n = k_share(real_pre, real_post, tokens, V_STD, K_STD)
    if n == 0:
        return {"V": V_STD, "k": K_STD, "share": float("nan"), "n": 0, "fitted": False, "usable": False}
    if s_std >= 0.99:
        return {"V": V_STD, "k": K_STD, "share": s_std, "n": n, "fitted": False, "usable": True}
    V, k, s, nf = fit_curve(real_pre, real_post, tokens)
    return {"V": V, "k": k, "share": s, "n": nf, "fitted": True,
            "usable": bool(s == s and s >= 0.99 and nf >= 5)}
