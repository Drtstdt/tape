"""The exit state machine. Ported from v3's positionStateMachine.js with its
three load-bearing invariants (docs/MIGRATION.md):

  1. The trail arms ONLY on a profit-taking partial. Arming on any partial
     cost -51.22% over 23 positions vs +3.71% over the other 316.
  2. A Category-A full exit zeroes the remaining position before returning
     -- the phantom double-sale bug is the class of failure this prevents.
  3. Same-bar ambiguity resolves DOWN: if both the stop and the take-profit
     fall inside one bar, the stop fires. Bars carry no intra-bar path.

And two v3 lessons applied directly (docs/PLAN.md Sec 7):
  * exits fire on SUSTAINED observations (divergence over N bars, liquidity
    drawdown), never on a single observation -- v3's single-observation
    exits fired 428 and 1,174 times at -14.8% / -8.5%;
  * time exits are driven by the clock sweep (`now_ms`), independent of the
    tape -- a token that stops trading still gets exited (v3's 224 positions
    died at -14.8% when the tape went quiet and nothing could sell them).

PURE: same (position, bar, features, now_ms) -> same action, on any machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from ..schema import Bar
from .spec import BandSpec, ExitSpec

FULL = 1.0


@dataclass
class ExitAction:
    reason: str                    # see REASONS below
    price: float                   # fill price (bar-derived, see engine)
    fraction: float                # of REMAINING tokens to sell (1.0 = close)
    ts_ms: int


# reason -> (full or partial, description)
REASONS = {
    "category_a_divergence": (FULL, "liquidity/price divergence sustained"),
    "category_a_liquidity_collapse": (FULL, "liquidity down vs its own peak"),
    "hard_stop": (FULL, "adverse barrier"),
    "take_profit_partial": (None, "partial at the band multiple"),
    "trail_stop": (FULL, "trail (armed by the partial)"),
    "time_stop": (FULL, "band horizon, clock-driven"),
    "tape_end": (FULL, "tape ended; closed at last observed price"),
}


@dataclass
class Position:
    """One open position. Mutable fields below are only ever written by
    step_position(); the engine treats the rest as read-only facts."""

    mint: str
    band: str
    entry_ts_ms: int
    entry_price: float
    size_sol: float                    # quote spent at entry (gross)
    tokens_total: float                # base acquired (net of entry fee)
    tp_price: float
    stop_price: float
    horizon_ts_ms: int

    tokens_remaining: float = field(init=False)
    partial_done: bool = field(default=False)
    partial_ts_ms: Optional[int] = None
    partial_proceeds_sol: float = 0.0
    peak_price: float = field(init=False)
    trail_stop: Optional[float] = None
    closed: bool = field(default=False)
    exit_reason: Optional[str] = None
    exit_ts_ms: Optional[int] = None
    exit_price: Optional[float] = None
    realized_sol: Optional[float] = None   # net proceeds minus size (all legs)

    def __post_init__(self) -> None:
        self.tokens_remaining = self.tokens_total
        self.peak_price = self.entry_price

    @property
    def pnl_pct(self) -> Optional[float]:
        if self.realized_sol is None or self.size_sol <= 0:
            return None
        return self.realized_sol / self.size_sol


def open_position(mint: str, band: BandSpec, entry_ts_ms: int, entry_price: float,
                  size_sol: float, tokens_bought: float) -> Position:
    return Position(
        mint=mint, band=band.name, entry_ts_ms=entry_ts_ms,
        entry_price=entry_price, size_sol=size_sol, tokens_total=tokens_bought,
        tp_price=entry_price * band.upper_multiple,
        stop_price=entry_price * (1.0 + band.lower_pct),
        horizon_ts_ms=entry_ts_ms + band.horizon_ms,
    )


def step_position(pos: Position, bar: Bar, feats: Dict[str, Optional[float]],
                  now_ms: int, spec: ExitSpec, band: BandSpec) -> Optional[ExitAction]:
    """One bar against one open position. Returns the action to take, or
    None. Order is load-bearing (see module docstring)."""

    if pos.closed:
        return None

    # -- 1. Category A: structural, overrides everything -------------------
    div = feats.get("liq_div_consecutive")
    if div is not None and div >= spec.cat_a_divergence_bars:
        pos.closed = True               # invariant 2: no second action can fire
        return ExitAction("category_a_divergence", bar.close, FULL, bar.close_ts_ms)
    dd = feats.get("liq_drawdown")
    if dd is not None and dd <= spec.cat_a_liq_drawdown:
        pos.closed = True
        return ExitAction("category_a_liquidity_collapse", bar.close, FULL, bar.close_ts_ms)

    # -- 2. hard stop (same-bar ambiguity resolves DOWN: before any TP) ----
    if bar.low <= pos.stop_price:
        pos.closed = True
        return ExitAction("hard_stop", pos.stop_price, FULL, bar.close_ts_ms)

    # -- 3. take-profit partial: arms the trail ----------------------------
    if not pos.partial_done and bar.high >= pos.tp_price:
        pos.partial_done = True
        pos.partial_ts_ms = bar.close_ts_ms
        # invariant 1: trail arms ONLY here, and the remainder is floored at
        # breakeven -- a winner never becomes a loser.
        pos.trail_stop = max(pos.entry_price * spec.remainder_floor_multiple,
                             pos.tp_price * (1.0 - band.trail_pct))
        pos.stop_price = pos.entry_price * spec.remainder_floor_multiple
        pos.peak_price = max(pos.peak_price, bar.high)
        return ExitAction("take_profit_partial", pos.tp_price, band.tp_fraction,
                          bar.close_ts_ms)

    # -- 4. trail / peak bookkeeping (only meaningful after the partial) ---
    if pos.partial_done:
        pos.peak_price = max(pos.peak_price, bar.high)
        if pos.trail_stop is not None:
            if bar.low <= pos.trail_stop:
                pos.closed = True
                return ExitAction("trail_stop", pos.trail_stop, FULL, bar.close_ts_ms)
            pos.trail_stop = max(pos.trail_stop, pos.peak_price * (1.0 - band.trail_pct))

    # -- 5. time stop: the passage of time, not the tape -------------------
    if now_ms >= pos.horizon_ts_ms:
        pos.closed = True
        return ExitAction("time_stop", bar.close, FULL, bar.close_ts_ms)

    return None


def apply_action(pos: Position, action: ExitAction, proceeds_sol: float) -> None:
    """Book the fill: tokens sold, proceeds banked. Called by the engine
    (which computes proceeds_sol through the executor's AMM math)."""
    sold = pos.tokens_remaining * action.fraction
    pos.tokens_remaining -= sold
    pos.partial_proceeds_sol += proceeds_sol
    if action.fraction >= FULL:
        pos.exit_reason = action.reason
        pos.exit_ts_ms = action.ts_ms
        pos.exit_price = action.price
        pos.realized_sol = pos.partial_proceeds_sol - pos.size_sol
        pos.closed = True
