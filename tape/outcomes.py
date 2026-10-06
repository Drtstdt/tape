"""D124 (docs/DECISIONS.md) -- realised NET outcome of one 2-SOL paper trade on
the pump.fun bonding curve, by replaying the token's own swaps against a
COUNTERFACTUAL curve that contains our position.

Why not a flat cost number: at 2 SOL against ~35 SOL of virtual reserve our
own buy moves the price ~6%, our sell moves it back, and every other trade in
between happens on a curve that already contains us. The constant-product
curve makes this exact (D120: +60% market -> ~+51% for us at Q=35).

Model (assumptions stated, each one a parameter or a flag):
  * reserves are the vendor's virtual reserves after each swap; k = Q*T is the
    curve invariant (checked per token: `k_rel_spread`);
  * the swap's SOL amount moves the curve's SOL side (fees are charged on top,
    D121: fee_lamports ~ 1.25-1.73% of lamports_amount) -- checked per token
    as the fraction of swaps whose reserve change equals the amount
    (`curve_amount_match`);
  * other traders' BUYS keep their SOL amount, their SELLS keep their token
    amount, when replayed on our curve (exact-in both ways);
  * we pay the token's measured fee ratio (median fee_sol/quote_amount of the
    swaps seen before the decision) on both legs, plus `fixed_cost_sol` per
    transaction (priority fee / tip; unmeasured -> sensitivity);
  * latency: entry after every swap with ts <= decision_ts + latency; an exit
    triggered at swap j executes after every swap with ts <= ts_j + latency;
  * barriers on our REALISABLE net value (what we would get by selling now);
  * horizon: exit at the curve state at entry_ts + horizon;
  * graduation (bonding_complete) inside the holding window: exit at the last
    curve state before it (flagged, `graduated`);
  * the holding window must lie in collected hours, else CENS (D119).

Timestamps are whole seconds (D121), so "latency" is in whole seconds too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Set

import numpy as np

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class TradeConfig:
    size_sol: float = 2.0
    take_profit: float = 0.60        # net return on our money
    stop_loss: float = -0.30
    horizon_ms: int = 30 * 60_000
    latency_s: int = 1
    fixed_cost_sol: float = 0.0      # per transaction; sensitivity, not measured


@dataclass
class Outcome:
    status: str          # TP | SL | TIME | CENS | NOENTRY
    net_ret: float       # net return on (size + fixed costs); NaN if CENS/NOENTRY
    exit_ts: int
    hold_s: float
    entry_q: float       # virtual SOL reserve at entry (before our buy)
    entry_impact: float  # our average entry price / spot - 1
    fee_ratio: float
    n_after: int         # other swaps replayed while holding
    graduated: bool
    max_net: float       # best realisable net return while holding
    min_net: float


def window_collected(t0_ms: int, t1_ms: int, collected: Set[int]) -> bool:
    for h in range(int(t0_ms) // HOUR_MS, int(t1_ms) // HOUR_MS + 1):
        if h not in collected:
            return False
    return True


def _sell_value(q: float, k: float, tokens: float, fee: float) -> float:
    """SOL we receive for selling `tokens` into a curve with SOL reserve q."""
    t = k / q
    gross = q - k / (t + tokens)
    return gross * (1.0 - fee)


def simulate(ts: np.ndarray, side_buy: np.ndarray, quote_amt: np.ndarray, base_amt: np.ndarray,
             q_after: np.ndarray, t_after: np.ndarray, dec_idx: int, fee: float,
             cfg: TradeConfig, collected: Optional[Set[int]] = None,
             bonding_ts: Optional[int] = None) -> Outcome:
    """Arrays: the token's swaps in time order (deduped). `dec_idx` = index of
    the swap at which the decision is taken (features may use swaps <= dec_idx
    only; this function is the ONLY place that looks further)."""
    n = len(ts)
    t_dec = int(ts[dec_idx])
    t_entry_cut = t_dec + cfg.latency_s * 1000
    nan = float("nan")
    if collected is not None and not window_collected(t_dec, t_entry_cut + cfg.horizon_ms, collected):
        return Outcome("CENS", nan, t_dec, 0.0, nan, nan, fee, 0, False, nan, nan)
    e = dec_idx
    while e + 1 < n and ts[e + 1] <= t_entry_cut:
        e += 1
    if bonding_ts is not None and bonding_ts <= ts[e]:
        return Outcome("NOENTRY", nan, int(ts[e]), 0.0, nan, nan, fee, 0, True, nan, nan)
    q0, t0 = float(q_after[e]), float(t_after[e])
    if not (q0 > 0 and t0 > 0):
        return Outcome("NOENTRY", nan, int(ts[e]), 0.0, nan, nan, fee, 0, False, nan, nan)
    k = q0 * t0
    S = cfg.size_sol
    sol_in = S / (1.0 + fee)                      # curve gets this, fee on top
    q = q0 + sol_in
    tokens = t0 - k / q
    spot = q0 / t0
    impact = (sol_in / tokens) / spot - 1.0
    cost_basis = S + cfg.fixed_cost_sol
    entry_ts = int(ts[e])
    deadline = entry_ts + cfg.horizon_ms

    def net_now(qq: float) -> float:
        return (_sell_value(qq, k, tokens, fee) - cfg.fixed_cost_sol) / cost_basis - 1.0

    best, worst = net_now(q), net_now(q)
    j = e + 1
    n_after = 0
    status = "TIME"
    exit_ts = deadline
    grad = False
    while j < n and ts[j] <= deadline:
        if bonding_ts is not None and ts[j] >= bonding_ts:
            grad = True
            status = "TIME"
            exit_ts = int(ts[j - 1]) if j - 1 > e else entry_ts
            break
        if side_buy[j]:
            q = q + float(quote_amt[j])
        else:
            q = k / (k / q + float(base_amt[j]))
        n_after += 1
        v = net_now(q)
        best, worst = max(best, v), min(worst, v)
        hit = "TP" if v >= cfg.take_profit else ("SL" if v <= cfg.stop_loss else None)
        if hit:
            cut = int(ts[j]) + cfg.latency_s * 1000
            while j + 1 < n and ts[j + 1] <= cut and not (bonding_ts is not None and ts[j + 1] >= bonding_ts):
                j += 1
                if side_buy[j]:
                    q = q + float(quote_amt[j])
                else:
                    q = k / (k / q + float(base_amt[j]))
                n_after += 1
            status = hit
            exit_ts = int(ts[j])
            break
        j += 1
    net = net_now(q)
    return Outcome(status, net, exit_ts, (exit_ts - entry_ts) / 1000.0, q0, impact, fee, n_after,
                   grad, max(best, net), min(worst, net))


def curve_checks(side_buy: np.ndarray, quote_amt: np.ndarray, q_after: np.ndarray,
                 t_after: np.ndarray) -> dict:
    """Per-token evidence for the model's two assumptions."""
    k = q_after * t_after
    good = k > 0
    k_rel = float((k[good].max() - k[good].min()) / np.median(k[good])) if good.sum() > 1 else 0.0
    dq = np.diff(q_after)
    exp = np.where(side_buy[1:], quote_amt[1:], -quote_amt[1:])
    ok = np.isclose(dq, exp, rtol=1e-6, atol=1e-6)
    return {"k_rel_spread": k_rel, "curve_amount_match": float(ok.mean()) if len(ok) else float("nan"),
            "n_pairs": int(len(ok))}


# ---------------------------------------------------------------------------
# D126: ANCHORED model (replaces `simulate` for research; `simulate` is kept
# only so D124's tests and record stay reproducible)
# ---------------------------------------------------------------------------

def simulate_anchored(ts: np.ndarray, q_mkt: np.ndarray, dec_idx: int, k: float,
                      fee_buy: float, fee_sell: float, cfg: TradeConfig,
                      collected: Optional[Set[int]] = None,
                      bonding_ts: Optional[int] = None,
                      exit_idx: Optional[int] = None,
                      slots: Optional[np.ndarray] = None,
                      latency_slots: Optional[int] = None) -> Outcome:
    """`q_mkt`: the market's virtual SOL reserve after each swap (V + real
    reserve, tape/curve.py) in execution order; k: the token's curve invariant.
    Our position is added ON TOP of the observed market path: our SOL stays in
    the curve (exact for other traders' SOL-exact buys; sells approximate).
    Fees: buy pays fee_buy on top of the curve amount; a sell receives
    gross / (1 + fee_sell) (vendor fee = ratio x what the user receives).
    `exit_idx` (D136, copy-trading 'mirror' exit): when the replay reaches that
    swap (inside the horizon) we exit at the state after it plus the latency,
    status MIRROR -- e.g. the swap at which the copied wallet sells.
    `latency_slots` (D140): with `slots` given, the reaction delay (entry and
    exits) is counted in SLOTS instead of whole seconds -- we act at the state
    after the last swap with slot <= trigger slot + latency_slots (0 = same
    block, right after the trigger: back-running; 1 = next slot, ~0.4 s).
    cfg.latency_s is then ignored; timestamps still bound the horizon."""
    n = len(ts)
    nan = float("nan")
    t_dec = int(ts[dec_idx])
    by_slot = latency_slots is not None
    if by_slot and slots is None:
        raise ValueError("latency_slots needs slots")

    def react(i: int) -> int:
        """Last index we can still see before acting on swap i."""
        if by_slot:
            cut_s = int(slots[i]) + int(latency_slots)
            while i + 1 < n and int(slots[i + 1]) <= cut_s and not (bonding_ts is not None and ts[i + 1] >= bonding_ts):
                i += 1
            return i
        cut = int(ts[i]) + cfg.latency_s * 1000
        while i + 1 < n and ts[i + 1] <= cut and not (bonding_ts is not None and ts[i + 1] >= bonding_ts):
            i += 1
        return i

    t_entry_cut = t_dec + (0 if by_slot else cfg.latency_s * 1000)
    if collected is not None and not window_collected(t_dec, t_entry_cut + cfg.horizon_ms, collected):
        return Outcome("CENS", nan, t_dec, 0.0, nan, nan, fee_buy, 0, False, nan, nan)
    if by_slot:
        e = dec_idx
        cut_s = int(slots[dec_idx]) + int(latency_slots)
        while e + 1 < n and int(slots[e + 1]) <= cut_s:
            e += 1
    else:
        e = dec_idx
        while e + 1 < n and ts[e + 1] <= t_entry_cut:
            e += 1
    if bonding_ts is not None and bonding_ts <= ts[e]:
        return Outcome("NOENTRY", nan, int(ts[e]), 0.0, nan, nan, fee_buy, 0, True, nan, nan)
    q0 = float(q_mkt[e])
    if not (q0 > 0 and k > 0):
        return Outcome("NOENTRY", nan, int(ts[e]), 0.0, nan, nan, fee_buy, 0, False, nan, nan)
    S = cfg.size_sol
    sol_in = S / (1.0 + fee_buy)
    N = k / q0 - k / (q0 + sol_in)
    impact = (sol_in / N) / (q0 * q0 / k) - 1.0
    basis = S + cfg.fixed_cost_sol

    def net_at(qm: float) -> float:
        qq = qm + sol_in
        gross = qq - k / (k / qq + N)
        return (gross / (1.0 + fee_sell) - cfg.fixed_cost_sol) / basis - 1.0

    entry_ts = int(ts[e])
    deadline = entry_ts + cfg.horizon_ms
    cur = q0
    best = worst = net_at(cur)
    status, exit_ts, grad, n_after = "TIME", deadline, False, 0
    j = e + 1
    while j < n and ts[j] <= deadline:
        if bonding_ts is not None and ts[j] >= bonding_ts:
            grad = True
            exit_ts = int(ts[j - 1]) if j - 1 > e else entry_ts
            break
        cur = float(q_mkt[j])
        n_after += 1
        v = net_at(cur)
        best, worst = max(best, v), min(worst, v)
        hit = "TP" if v >= cfg.take_profit else ("SL" if v <= cfg.stop_loss else None)
        if hit is None and exit_idx is not None and j >= exit_idx:
            hit = "MIRROR"
        if hit:
            j2 = react(j)
            n_after += j2 - j
            j = j2
            cur = float(q_mkt[j])
            status, exit_ts = hit, int(ts[j])
            break
        j += 1
    net = net_at(cur)
    return Outcome(status, net, exit_ts, (exit_ts - entry_ts) / 1000.0, q0, impact, fee_buy, n_after,
                   grad, max(best, net), min(worst, net))


# ---------------------------------------------------------------------------
# D132: LONG holds (hours) on a corpus with uncollected hours
# ---------------------------------------------------------------------------

@dataclass
class LongOutcome:
    status: str          # TP | TIME | GRAD | NO_LATER_SWAP | NOENTRY
    net_ret: float       # primary valuation
    net_pessimistic: float   # NO_LATER_SWAP valued at the worst case (everyone else dumped)
    exit_ts: int
    exit_delay_s: float  # TIME: first observed swap after the deadline minus the deadline
    entry_q: float


def simulate_long(ts: np.ndarray, q_mkt: np.ndarray, flow: np.ndarray, dec_idx: int, k: float, v: float,
                  fee_buy: float, fee_sell: float, size_sol: float, latency_s: int, horizon_ms: int,
                  take_profit: Optional[float], bonding_ts: Optional[int]) -> LongOutcome:
    """Hold for `horizon_ms` (optionally exit earlier at `take_profit`), no stop loss.

    Only OBSERVED swaps are seen (hours are missing, D122), so:
      * TP is checked at observed post-swap states (a spike inside a gap is
        missed -> conservative);
      * the time exit uses the state just BEFORE the first observed swap at or
        after the deadline -- exact (pre-state = post-state - flow), whatever
        happened in the gaps; the exit is then at that swap's time
        (exit_delay_s reports how late);
      * graduation (bonding_complete): exit at the curve state after the last
        swap with ts <= bonding_ts (the completing swap included);
      * no observed swap after the deadline and no graduation: NO_LATER_SWAP,
        valued at the last observed state (primary) and at the worst case
        -- every other holder dumped -- (net_pessimistic)."""
    n = len(ts)
    nan = float("nan")
    t_cut = int(ts[dec_idx]) + latency_s * 1000
    e = dec_idx
    while e + 1 < n and ts[e + 1] <= t_cut:
        e += 1
    if bonding_ts is not None and bonding_ts <= ts[e]:
        return LongOutcome("NOENTRY", nan, nan, int(ts[e]), 0.0, nan)
    q0 = float(q_mkt[e])
    sol_in = size_sol / (1.0 + fee_buy)
    N = k / q0 - k / (q0 + sol_in)

    def net_at(qm: float) -> float:
        qq = qm + sol_in
        return (qq - k / (k / qq + N)) / (1.0 + fee_sell) / size_sol - 1.0

    worst = net_at(v)                 # everyone else gone: the market curve back at its start
    entry_ts = int(ts[e])
    deadline = entry_ts + horizon_ms
    j = e + 1
    while j < n and ts[j] < deadline:
        if bonding_ts is not None and ts[j] > bonding_ts:
            val = net_at(float(q_mkt[j - 1]))
            return LongOutcome("GRAD", val, val, int(ts[j - 1]), 0.0, q0)
        if take_profit is not None and net_at(float(q_mkt[j])) >= take_profit:
            cut = int(ts[j]) + latency_s * 1000
            while j + 1 < n and ts[j + 1] <= cut and not (bonding_ts is not None and ts[j + 1] > bonding_ts):
                j += 1
            val = net_at(float(q_mkt[j]))
            return LongOutcome("TP", val, val, int(ts[j]), 0.0, q0)
        j += 1
    if bonding_ts is not None and bonding_ts < deadline:
        last = j - 1
        while last > e and ts[last] > bonding_ts:
            last -= 1
        val = net_at(float(q_mkt[last]))
        return LongOutcome("GRAD", val, val, int(bonding_ts), 0.0, q0)
    if j < n:
        pre = float(q_mkt[j]) - float(flow[j])
        val = net_at(pre)
        return LongOutcome("TIME", val, val, int(ts[j]), (int(ts[j]) - deadline) / 1000.0, q0)
    val = net_at(float(q_mkt[n - 1]))
    return LongOutcome("NO_LATER_SWAP", val, worst, int(ts[n - 1]), nan, q0)


# ---------------------------------------------------------------------------
# D134: partial take-profit (sell a fraction at TP, the rest follows a rule)
# ---------------------------------------------------------------------------

def simulate_partial(ts: np.ndarray, q_mkt: np.ndarray, flow: np.ndarray, dec_idx: int, k: float, v: float,
                     fee_buy: float, fee_sell: float, size_sol: float, latency_s: int, horizon_ms: int,
                     take_profit: float, sell_frac: float, rest: str,
                     bonding_ts: Optional[int]) -> LongOutcome:
    """Like simulate_long(take_profit=...), but at the TP only `sell_frac` of the
    tokens is sold; the rest then follows `rest`:
      'time'       hold to the same deadline (time exit / graduation / no later
                   swap exactly as simulate_long);
      'breakeven'  additionally exit at the first observed swap whose market
                   state is back at or below the ENTRY state (price back to the
                   entry price), after the latency.
    Without a TP hit the result equals simulate_long(..., take_profit, ...)
    with the status of the time exit. The sales are path-additive on the curve:
    after selling N1 tokens at state q1 our net SOL in the curve is sol_in - g1,
    so selling everything at one state in two parts equals selling it at once.
    Statuses: TP-path ones are prefixed PART_ (PART_TIME, PART_STOP, PART_GRAD,
    PART_NLS); otherwise TIME | GRAD | NO_LATER_SWAP | NOENTRY."""
    if rest not in ("time", "breakeven"):
        raise ValueError(rest)
    n = len(ts)
    nan = float("nan")
    t_cut = int(ts[dec_idx]) + latency_s * 1000
    e = dec_idx
    while e + 1 < n and ts[e + 1] <= t_cut:
        e += 1
    if bonding_ts is not None and bonding_ts <= ts[e]:
        return LongOutcome("NOENTRY", nan, nan, int(ts[e]), 0.0, nan)
    q0 = float(q_mkt[e])
    sol_in = size_sol / (1.0 + fee_buy)
    N = k / q0 - k / (q0 + sol_in)

    def sell(qm: float, add: float, tokens: float) -> float:
        qq = qm + add
        if not qq > 0:
            return 0.0
        return max(qq - k / (k / qq + tokens), 0.0)

    def net_full(qm: float) -> float:
        return sell(qm, sol_in, N) / (1.0 + fee_sell) / size_sol - 1.0

    entry_ts = int(ts[e])
    deadline = entry_ts + horizon_ms
    grad_after = (lambda t: bonding_ts is not None and t > bonding_ts)

    def latency_walk(j: int) -> int:
        cut = int(ts[j]) + latency_s * 1000
        while j + 1 < n and ts[j + 1] <= cut and not grad_after(ts[j + 1]):
            j += 1
        return j

    # phase 1: full position until the TP (no stop), as simulate_long
    j = e + 1
    tp_j = None
    while j < n and ts[j] < deadline:
        if grad_after(ts[j]):
            val = net_full(float(q_mkt[j - 1]))
            return LongOutcome("GRAD", val, val, int(ts[j - 1]), 0.0, q0)
        if net_full(float(q_mkt[j])) >= take_profit:
            tp_j = latency_walk(j)
            break
        j += 1
    if tp_j is None:
        if bonding_ts is not None and bonding_ts < deadline:
            last = j - 1
            while last > e and ts[last] > bonding_ts:
                last -= 1
            val = net_full(float(q_mkt[last]))
            return LongOutcome("GRAD", val, val, int(bonding_ts), 0.0, q0)
        if j < n:
            val = net_full(float(q_mkt[j]) - float(flow[j]))
            return LongOutcome("TIME", val, val, int(ts[j]), (int(ts[j]) - deadline) / 1000.0, q0)
        val = net_full(float(q_mkt[n - 1]))
        return LongOutcome("NO_LATER_SWAP", val, net_full(v), int(ts[n - 1]), nan, q0)

    # the partial sale
    n1 = N * sell_frac
    g1 = sell(float(q_mkt[tp_j]), sol_in, n1)
    p1 = g1 / (1.0 + fee_sell)
    add, n2 = sol_in - g1, N - n1

    def total(qm: float) -> float:
        return (p1 + sell(qm, add, n2) / (1.0 + fee_sell)) / size_sol - 1.0

    # phase 2: the rest
    j = tp_j + 1
    while j < n and ts[j] < deadline:
        if grad_after(ts[j]):
            val = total(float(q_mkt[j - 1]))
            return LongOutcome("PART_GRAD", val, val, int(ts[j - 1]), 0.0, q0)
        if rest == "breakeven" and float(q_mkt[j]) <= q0:
            j = latency_walk(j)
            val = total(float(q_mkt[j]))
            return LongOutcome("PART_STOP", val, val, int(ts[j]), 0.0, q0)
        j += 1
    if bonding_ts is not None and bonding_ts < deadline:
        last = j - 1
        while last > tp_j and ts[last] > bonding_ts:
            last -= 1
        val = total(float(q_mkt[last]))
        return LongOutcome("PART_GRAD", val, val, int(bonding_ts), 0.0, q0)
    if j < n:
        val = total(float(q_mkt[j]) - float(flow[j]))
        return LongOutcome("PART_TIME", val, val, int(ts[j]), (int(ts[j]) - deadline) / 1000.0, q0)
    val = total(float(q_mkt[n - 1]))
    return LongOutcome("PART_NLS", val, total(v), int(ts[n - 1]), nan, q0)
