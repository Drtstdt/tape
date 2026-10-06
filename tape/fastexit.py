"""D142: vectorised exit grid -- the same outcome as tape.outcomes.simulate_anchored
(slot latency, fixed per-transaction cost, graduation exit) for MANY
(take-profit, stop-loss, horizon) combinations of one entry at once.

Inputs are one token's path from the decision swap on (index 0 = the decision
swap), in execution order:
    rel_ts  ms since the decision swap      slot_rel  slots since the decision swap
    q       market virtual SOL reserve after each swap (q_mkt)
    grad    index of the first swap with ts >= bonding_ts (len(path) if none)
Exactness against simulate_anchored is tested (tests/test_fastexit.py).
"""

from __future__ import annotations

import numpy as np

NO_TP = 1e9
NO_SL = -1e9


def react_index(slot_rel: np.ndarray, grad: int, latency_slots: int) -> np.ndarray:
    """react[j] = last index we see before acting on swap j: the last swap with
    slot <= slot[j] + L, never stepping onto a swap at/after graduation."""
    n = len(slot_rel)
    lim = np.searchsorted(slot_rel, slot_rel + latency_slots, side="right") - 1
    cap = np.where(np.arange(n) < grad, grad - 1, np.arange(n))      # cannot step past grad-1
    return np.maximum(np.minimum(lim, cap), np.arange(n))


def exit_grid(rel_ts: np.ndarray, slot_rel: np.ndarray, q: np.ndarray, grad: int, k: float,
              fee_b: float, fee_s: float, size: float, latency_slots: int, tip_sol: float,
              tps: np.ndarray, sls: np.ndarray, horizons_ms: np.ndarray):
    """Net returns, shape (len(tps), len(sls), len(horizons_ms)); NaN for NOENTRY."""
    tps, sls, hs = np.asarray(tps, float), np.asarray(sls, float), np.asarray(horizons_ms, np.int64)
    out = np.full((len(tps), len(sls), len(hs)), np.nan)
    n = len(q)
    e = int(np.searchsorted(slot_rel, latency_slots, side="right") - 1)
    if grad <= e:
        return out                                                     # NOENTRY (graduated before entry)
    q0 = float(q[e])
    if not (q0 > 0 and k > 0):
        return out
    sol_in = size / (1.0 + fee_b)
    N = k / q0 - k / (q0 + sol_in)
    basis = size + tip_sol

    def net(qm):
        qq = qm + sol_in
        return ((qq - k / (k / qq + N)) / (1.0 + fee_s) - tip_sol) / basis - 1.0

    t_e = int(rel_ts[e])
    last_any = int(np.searchsorted(rel_ts, t_e + hs.max(), side="right") - 1)
    hi = min(last_any, grad - 1)                                       # last index the loop can process
    if hi <= e:
        v = net(q0)
        out[:] = v
        return out
    J = np.arange(e + 1, hi + 1)
    v = net(q[J].astype(float))
    cmax = np.maximum.accumulate(v)
    cmin = np.minimum.accumulate(v)
    tp_pos = np.searchsorted(cmax, tps, side="left")                   # first pos with v >= TP
    sl_pos = np.searchsorted(-cmin, -sls, side="left")                 # first pos with v <= SL
    hit = np.minimum(tp_pos[:, None], sl_pos[None, :])                 # (TP, SL), len(J) = none
    react = react_index(slot_rel, grad, latency_slots)
    # per horizon: last processed position (deadline, graduation)
    end_idx = np.minimum(np.searchsorted(rel_ts, t_e + hs, side="right") - 1, grad - 1)
    end_pos = end_idx - (e + 1)                                        # -1 => nothing processed
    exit_net_hit = np.where(hit < len(J), net(q[react[J[np.minimum(hit, len(J) - 1)]]].astype(float)), np.nan)
    for h in range(len(hs)):
        ep = int(end_pos[h])
        no_hit_val = net(float(q[e + 1 + ep])) if ep >= 0 else net(q0)
        valid = hit <= ep
        out[:, :, h] = np.where(valid, exit_net_hit, no_hit_val)
    return out
