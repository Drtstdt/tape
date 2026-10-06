"""decide() -- the one decision function.

Called by the replay engine with bars from disk, and by the live engine with
bars from a websocket. Same function, same inputs, same answer. There is no
second code path, so there is no class of bug where backtest and live disagree
-- which is what every expensive failure in v3's history actually was.

PURE. No I/O, no clock reads, no network. The caller supplies `now_ms`.

Order:
    1 hard rails      fail closed on any missing safety-critical field
    2 cost floor      can this position clear its own round trip at all
    3 model           calibrated probability
    4 conformal       has the model seen anything like this
    5 sizing          conviction-weighted, capped by depth

Rails are rails, not preferences. They can only ever REJECT. Nothing the model
says may overrule one, because a model fitted on a corpus where a rail was
always true has no opinion about what happens when it is false.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .costs import CostModel, cost_floor_ok, price_impact_pct
from .schema import Decision


@dataclass(slots=True)
class Rails:
    """Hard, non-negotiable, never fitted. Missing means reject."""

    min_liquidity_usd: float = 13_000.0
    max_position_to_liquidity_pct: float = 1.0
    min_age_ms: int = 150_000
    max_top_holder_pct: float = 80.0
    require_mint_authority_renounced: bool = True
    require_freeze_authority_renounced: bool = True
    min_lp_locked_pct: Optional[float] = None
    max_creator_rug_rate: float = 0.6
    reject_blacklisted_creator: bool = True
    max_ceiling_ratio: Optional[float] = None      # reject if R >= ceiling: no upside left
    min_attributed_trades: int = 3                 # breadth needs real wallets
    max_largest_buyer_share: float = 0.4
    min_unique_buyers: int = 3


@dataclass(slots=True)
class ConfidencePolicy:
    """The abstention rule. This is what 'confident, not in the fog' means in
    code: two independent numbers must both clear a bar before any money moves.
    """

    breakeven_win_rate: float = 0.296      # measured: avg win +52.4 / avg loss -22.0
    probability_margin: float = 0.04       # demand a real edge, not a coin flip
    min_credibility: float = 0.15          # conformal floor
    payoff_ratio: float = 2.38

    # TENTH-Kelly, not the usual quarter. Kelly assumes you KNOW p; here p is
    # estimated from a model fitted on a few months of a market that changes
    # regime in days, and overbetting is punished superlinearly while
    # underbetting costs only linearly. The asymmetry is the whole argument.
    kelly_fraction: float = 0.10

    # Chosen so the cap does NOT bind across the p range the model actually
    # emits (roughly 0.33-0.45 after the threshold). At quarter-Kelly and a
    # 1% cap, every entering trade sizes identically -- flat sizing with extra
    # steps, and none of the conviction benefit. `sizing_is_actually_varying`
    # is the check; run it whenever these two numbers change.
    max_position_fraction: float = 0.02

    @property
    def min_probability(self) -> float:
        return self.breakeven_win_rate + self.probability_margin


def evaluate_rails(sig: Dict, rails: Rails, cost: CostModel,
                   position_sol: float) -> Optional[tuple]:
    """Returns (reason, layer) on rejection, or None if every rail passes.

    Every `is None` check below is deliberate. Unknown is not safe: a null on a
    safety-critical field means nobody measured it, and trading on an unmeasured
    safety field is how you find out what it was.
    """
    liq_usd = sig.get("liquidity_usd")
    if liq_usd is None:
        return ("missing_liquidity", "rail")
    if liq_usd < rails.min_liquidity_usd:
        return ("liquidity_below_floor", "rail")

    liq_sol = sig.get("liquidity_sol")
    if liq_sol:
        impact = price_impact_pct(liq_sol, position_sol)
        if impact is not None and impact * 100.0 > rails.max_position_to_liquidity_pct:
            # On a thin pool your own exit is the rug. This rail, not the
            # liquidity floor, is usually the binding one.
            return ("position_too_large_vs_liquidity", "rail")

    age = sig.get("age_ms")
    if age is None:
        return ("missing_age", "rail")
    if age < rails.min_age_ms:
        # Never buy the event. On the measured reference token price gapped to
        # -32.4% within 8 seconds of migration and EVERY stop tighter than that
        # produced an identical fill -- on a fresh listing the stop percentage
        # is not a decision variable, and the only defence that works lives at
        # the entry.
        return ("token_too_young", "rail")

    thp = sig.get("top_holder_pct")
    if thp is None:
        return ("missing_top_holder_pct", "rail")
    if thp > rails.max_top_holder_pct:
        return ("top_holder_too_concentrated", "rail")

    if rails.require_mint_authority_renounced:
        v = sig.get("mint_authority_renounced")
        if v is None:
            return ("missing_mint_authority_status", "rail")
        if not v:
            return ("mint_authority_not_renounced", "rail")

    if rails.require_freeze_authority_renounced:
        v = sig.get("freeze_authority_renounced")
        if v is None:
            return ("missing_freeze_authority_status", "rail")
        if not v:
            return ("freeze_authority_not_renounced", "rail")

    if rails.min_lp_locked_pct is not None:
        v = sig.get("lp_locked_pct")
        if v is None:
            return ("missing_lp_lock_status", "rail")
        if v < rails.min_lp_locked_pct:
            return ("lp_not_locked", "rail")

    if rails.reject_blacklisted_creator and sig.get("creator_blacklisted"):
        return ("creator_blacklisted", "rail")

    rug = sig.get("creator_rug_rate")
    if rug is not None and rug > rails.max_creator_rug_rate:
        return ("creator_rug_rate_too_high", "rail")

    if rails.max_ceiling_ratio is not None:
        R = sig.get("ceiling_ratio_R")
        if R is not None and R >= rails.max_ceiling_ratio:
            # R is the inverse of the fraction of supply in the pool. At or
            # above the ceiling there is no upside left by this model, with
            # 1 - 1/R of supply outside a pool that cannot absorb it. Free screen.
            return ("already_at_ceiling", "rail")

    # Breadth. Nulls here are UNMEASURED, not zero -- see bars._close().
    n_attr = sig.get("attributed_trades")
    if n_attr is None or n_attr < rails.min_attributed_trades:
        return ("insufficient_attribution", "rail")
    ub = sig.get("unique_buyers")
    if ub is None:
        return ("missing_buyer_breadth", "rail")
    if ub < rails.min_unique_buyers:
        return ("too_few_unique_buyers", "rail")
    lbs = sig.get("largest_buyer_share")
    if lbs is None:
        return ("missing_buyer_breadth", "rail")
    if lbs > rails.max_largest_buyer_share:
        # Ten wallets buying beats one wallet buying ten times.
        return ("single_wallet_dominates_buying", "rail")

    if not cost_floor_ok(position_sol, sig.get("expected_multiple", 1.8), cost):
        return ("below_cost_floor", "rail")

    return None


def kelly_size(p: float, payoff: float, fraction: float,
               capital_sol: float, max_fraction: float) -> float:
    """f* = (p*b - (1-p)) / b, scaled and clamped.

    This is where two points of hit rate become money rather than a statistic.
    At a 2.38 payoff, sizing 3% of capital on a 45% signal and 0.5% on a 31%
    one is worth more than another point of average accuracy.
    """
    if payoff <= 0:
        return 0.0
    f = (p * payoff - (1.0 - p)) / payoff
    if f <= 0:
        return 0.0
    return min(f * fraction, max_fraction) * capital_sol


def sizing_is_actually_varying(conf, p_lo: float, p_hi: float) -> bool:
    """Sanity check you should run whenever the sizing params change.

    At a 2.38 payoff, Kelly is large, so a tight `max_position_fraction` can
    bind for EVERY probability the model ever emits -- at which point you have
    flat sizing with extra steps and none of the conviction benefit. Check that
    the cap does not bind across the range of p you actually expect, and widen
    the cap or lower `kelly_fraction` until it does not.
    """
    lo = kelly_size(p_lo, conf.payoff_ratio, conf.kelly_fraction, 1.0,
                    conf.max_position_fraction)
    hi = kelly_size(p_hi, conf.payoff_ratio, conf.kelly_fraction, 1.0,
                    conf.max_position_fraction)
    return hi > lo


def decide(
    features: Dict[str, Optional[float]],
    signals: Dict,
    model,                                   # ModelArtifact | None
    rails: Rails,
    conf: ConfidencePolicy,
    cost: CostModel,
    capital_sol: float,
    now_ms: int,
    band: str = "_all",
    mint: str = "",
) -> Decision:
    candidate = min(conf.max_position_fraction * capital_sol,
                    (signals.get("liquidity_sol") or 0.0)
                    * rails.max_position_to_liquidity_pct / 100.0)

    rail_fail = evaluate_rails(signals, rails, cost, max(candidate, 1e-9))
    if rail_fail is not None:
        return Decision(mint=mint, ts_ms=now_ms, action="reject",
                        reason=rail_fail[0], layer=rail_fail[1], band=band)

    if model is None:
        # No validated model: size flat and SAY SO. A fallback that looks like
        # a decision is how an unvalidated number acquires authority.
        return Decision(mint=mint, ts_ms=now_ms, action="enter",
                        reason="no_model_flat_size", layer="sizing", band=band,
                        size_sol=candidate, size_cap_reason="flat_fallback")

    pred = model.predict(features, band=band)
    p, cred = pred["p_calibrated"], pred["credibility"]

    if cred < conf.min_credibility:
        # The model has not seen anything like this. Abstaining here is the
        # whole point: an out-of-distribution prediction is a guess wearing a
        # probability's clothes.
        return Decision(mint=mint, ts_ms=now_ms, action="abstain",
                        reason="out_of_distribution", layer="conformal", band=band,
                        p_raw=pred["p_raw"], p_calibrated=p, credibility=cred)

    if p < conf.min_probability:
        return Decision(mint=mint, ts_ms=now_ms, action="abstain",
                        reason="below_probability_threshold", layer="model", band=band,
                        p_raw=pred["p_raw"], p_calibrated=p, credibility=cred)

    size = kelly_size(p, conf.payoff_ratio, conf.kelly_fraction,
                      capital_sol, conf.max_position_fraction)
    cap_reason = None
    if size > candidate:
        size, cap_reason = candidate, "pool_depth"

    if not cost_floor_ok(size, signals.get("expected_multiple", 1.8), cost):
        return Decision(mint=mint, ts_ms=now_ms, action="abstain",
                        reason="size_below_cost_floor", layer="sizing", band=band,
                        p_raw=pred["p_raw"], p_calibrated=p, credibility=cred,
                        size_sol=size)

    return Decision(mint=mint, ts_ms=now_ms, action="enter", reason=None,
                    layer=None, band=band, p_raw=pred["p_raw"], p_calibrated=p,
                    credibility=cred, size_sol=size, size_cap_reason=cap_reason)
