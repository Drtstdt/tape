"""The cost model. Get this wrong and every other number in the project is
decoration.

v3 charges only a PROPORTIONAL fee (1% per side) and no fixed cost. Real Solana
trading also pays a base fee plus a priority fee per transaction, and a fixed
cost does not shrink with the position:

    position SOL   fee 0.00001   0.0001    0.001     0.005
           0.002          0.5%     5.0%    50.0%    250.0%
           0.006          0.2%     1.7%    16.7%     83.3%   <- v3's size
           0.100          0.0%     0.1%     1.0%      5.0%
           0.500          0.0%     0.0%     0.2%      1.0%

At 0.006 SOL a modest 0.0001 SOL priority fee DOUBLES the round trip. This is
why the venue change (thin bonding curve -> real pool) is worth more than any
parameter in the system: it is the only way to make the position large enough
in absolute terms that a fixed fee stops mattering.

Measure `fixed_cost_sol` against real landed transactions before trusting any
backtest. Until it exists, every result is optimistic by an unmeasured amount,
and the amount is larger than any edge found so far.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class CostModel:
    fee_pct: float = 0.01              # proportional, per side
    fixed_cost_sol: float = 0.0        # base + priority fee, per transaction
    extra_slippage_pct: float = 0.0    # beyond the AMM curve; for adverse selection

    def round_trip_pct(self, position_sol: float) -> float:
        """Total cost of a round trip as a fraction of the position."""
        if position_sol <= 0:
            return float("inf")
        prop = 2.0 * (self.fee_pct + self.extra_slippage_pct)
        fixed = 2.0 * self.fixed_cost_sol / position_sol
        return prop + fixed

    def min_profitable_multiple(self, position_sol: float, margin: float = 1.0) -> float:
        """Price multiple that must be reached just to break even, times margin."""
        return (1.0 + self.round_trip_pct(position_sol)) * margin

    def breakeven_win_rate(self, avg_win_pct: float, avg_loss_pct: float) -> float:
        """p such that p*win + (1-p)*loss == 0. avg_loss_pct is negative."""
        if avg_win_pct <= 0 or avg_loss_pct >= 0:
            raise ValueError("expected positive avg_win_pct and negative avg_loss_pct")
        return -avg_loss_pct / (avg_win_pct - avg_loss_pct)


def constant_product_buy(quote_reserve: float, base_reserve: float,
                         quote_in: float, fee_pct: float) -> tuple:
    """Exact constant-product fill. Returns (base_out, avg_price, price_after).

    Ported from v3's verified executionModel. Do not re-derive it: an
    approximation here (v1 used `liquidity + 30`) makes every backtest
    incomparable with live.
    """
    if quote_reserve <= 0 or base_reserve <= 0 or quote_in <= 0:
        return 0.0, 0.0, 0.0
    effective_in = quote_in * (1.0 - fee_pct)
    k = quote_reserve * base_reserve
    new_quote = quote_reserve + effective_in
    new_base = k / new_quote
    base_out = base_reserve - new_base
    avg_price = quote_in / base_out if base_out > 0 else 0.0
    return base_out, avg_price, new_quote / new_base


def constant_product_sell(quote_reserve: float, base_reserve: float,
                          base_in: float, fee_pct: float) -> tuple:
    """Returns (quote_out, avg_price, price_after)."""
    if quote_reserve <= 0 or base_reserve <= 0 or base_in <= 0:
        return 0.0, 0.0, 0.0
    k = quote_reserve * base_reserve
    new_base = base_reserve + base_in
    new_quote = k / new_base
    gross_out = quote_reserve - new_quote
    quote_out = gross_out * (1.0 - fee_pct)
    avg_price = quote_out / base_in if base_in > 0 else 0.0
    return quote_out, avg_price, new_quote / new_base


def price_impact_pct(quote_reserve: float, position_sol: float) -> Optional[float]:
    """Your own impact, both ways. On a thin pool your exit IS the rug."""
    if quote_reserve is None or quote_reserve <= 0:
        return None
    return position_sol / quote_reserve


def cost_floor_ok(position_sol: float, expected_multiple: float,
                  cost: CostModel, min_multiple: float = 3.0) -> bool:
    """Reject unless the expected move clears the round trip by `min_multiple`.

    v1's 1%-each-way fee meant a position that round-tripped at breakeven still
    lost ~2%, and 76,098 of its trades did exactly that.
    """
    rt = cost.round_trip_pct(position_sol)
    expected_gain = expected_multiple - 1.0
    return expected_gain >= rt * min_multiple


# ---------------------------------------------------------------------------
# Partial-exit (D18, docs/DECISIONS.md): sell `exit_fraction` at
# `trigger_multiple`, let the remainder ride to `final_multiple`.
# ---------------------------------------------------------------------------
#
# `round_trip_pct` above prices a TWO-leg trade (one entry, one exit). This
# strategy executes THREE: entry, the partial exit, and the exit of whatever
# is left. Each leg pays `fixed_cost_sol` independently -- that third fixed
# fee is not free, and at small position sizes it can eat the entire point of
# taking profit early. These two functions exist so that claim is a computed
# number, not an assumption, exactly like `cost_floor_ok` did for the
# two-leg case.

def partial_exit_round_trip_pct(position_sol: float, trigger_multiple: float,
                                exit_fraction: float, cost: CostModel,
                                final_multiple: float = 1.0) -> float:
    """Total cost of the three-leg trade, as a fraction of the entry position.

    `final_multiple` is what the REMAINDER is assumed to exit at -- default
    1.0 (breakeven), matching D18's `remainder_floor: entry_price` rule. Pass
    a different value to price a specific scenario (e.g. the remainder also
    reaching 2x, or going to zero).
    """
    if position_sol <= 0:
        return float("inf")
    if not 0.0 < exit_fraction < 1.0:
        raise ValueError("exit_fraction must be strictly between 0 and 1")

    entry_cost = cost.fee_pct * position_sol + cost.fixed_cost_sol
    partial_notional = exit_fraction * position_sol * trigger_multiple
    partial_cost = cost.fee_pct * partial_notional + cost.fixed_cost_sol
    remainder_notional = (1.0 - exit_fraction) * position_sol * final_multiple
    remainder_cost = cost.fee_pct * remainder_notional + cost.fixed_cost_sol

    return (entry_cost + partial_cost + remainder_cost) / position_sol


def partial_exit_capital_recovered_pct(trigger_multiple: float,
                                       exit_fraction: float) -> float:
    """Fraction of the ORIGINAL position the partial leg alone returns,
    before any fees. `trigger_multiple * exit_fraction == 1.0` is the exact
    point at which the trim recovers the entire entry cost by itself --
    which is what makes trigger=2.0, fraction=0.5 ("sell half at +100%")
    a clean number and trigger=1.8 (v3's fitted `cost_recovery`) not quite:
    at 0.5 fraction, 1.8x recovers 90% of capital before fees, not 100%.
    """
    return trigger_multiple * exit_fraction


def partial_exit_cost_floor_ok(position_sol: float, trigger_multiple: float,
                               exit_fraction: float, cost: CostModel,
                               final_multiple: float = 1.0,
                               min_multiple: float = 3.0) -> bool:
    """Same discipline as `cost_floor_ok`, extended to the three-leg trade.

    Rejects the whole partial-exit plan (not just the entry) unless the
    expected combined gain clears the three-leg round trip by `min_multiple`.
    """
    rt = partial_exit_round_trip_pct(position_sol, trigger_multiple,
                                     exit_fraction, cost, final_multiple)
    expected_gain = (exit_fraction * trigger_multiple
                     + (1.0 - exit_fraction) * final_multiple - 1.0)
    return expected_gain >= rt * min_multiple
