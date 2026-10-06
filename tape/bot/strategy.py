"""decide_entry() -- the ONE entry decision, called identically by the
backtester and the live bot.

The strategy, in one paragraph (full writeup in docs/BOT.md):

    ACCUMULATE-AND-CONFIRM. Buy only where a tape exists and accumulation
    is OBSERVED in it -- positive net flow, buyer breadth, liquidity not
    draining, price moving WITH flow, nothing overextended, no creator
    dump -- then ask a calibrated model whether THIS kind of tape has won
    before (conformal credibility: "have I seen this before"), and size by
    conviction. The rails can only reject; the gate can only veto; the
    model's probability is the only number that sizes.

Layer order, exactly:
    1 rail      fail closed on any missing safety-critical field
    2 gate      the accumulation thesis, on measured quantities only
    3 model     calibrated probability + conformal credibility
    4 sizing    Kelly, conviction-weighted, capped by depth and band

PURE. No I/O, no clock, no RNG. Same inputs -> same Decision, forever.
The caller supplies now_ms, capital and the band; the engine supplies the
model. Abstentions are first-class rows with reason codes -- the tally of
why the bot did NOT act is the most informative output of any run.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..costs import CostModel, cost_floor_ok
from ..model import ModelArtifact
from ..schema import Decision
from .spec import BandSpec, BotSpec, EntrySpec

# Feature-name constants -- the contract between TokenState.features() and
# this module. Named to match tape/features.py exactly; a rename there is a
# silent rail failure here, so tests pin these.
F_LIQ = "liquidity"
F_AGE = "age_ms"
F_NBARS = "n_bars"
F_UB10 = "unique_buyers_10"
F_LBS10 = "largest_buyer_share_10"
F_NETFLOW_K = "netflow_{k}_over_liq"
F_BREADTH_K = "buyer_seller_breadth_10"
F_DIVERGENCE = "flow_price_divergence"
F_LIQ_SLOPE = "liq_slope_10"
F_RSI = "rsi"
F_CREATOR_SOLD = "creator_sold_over_liq"


def build_signals(features: Dict[str, Optional[float]],
                  extras: Optional[Dict] = None) -> Dict:
    """Enrich the feature row with the safety fields the rails read that are
    not themselves features (holder concentration, attribution). Extras come
    from TokenMeta / reputation sources; absent means unmeasured."""
    sig = {
        "liquidity_sol": features.get(F_LIQ),
        "age_ms": features.get(F_AGE),
        "n_bars": features.get(F_NBARS),
        "unique_buyers_10": features.get(F_UB10),
        "largest_buyer_share_10": features.get(F_LBS10),
    }
    for key in ("top_holder_pct", "mint_authority_renounced",
                "freeze_authority_renounced", "creator_blacklisted",
                "creator_rug_rate", "attributed_trades"):
        sig[key] = (extras or {}).get(key)
    return sig


def _rail_rejections(sig: Dict, features: Dict[str, Optional[float]],
                     band: BandSpec, spec: BotSpec, entry: EntrySpec,
                     candidate_sol: float, cost: CostModel) -> Optional[str]:
    """Returns the reason string on the FIRST failing rail, else None.
    Every `is None` check is deliberate: unknown is not safe."""

    liq = sig.get("liquidity_sol")
    if liq is None:
        return "missing_liquidity"
    if liq < band.min_liquidity_sol:
        return "liquidity_below_floor"

    # On a thin pool your own exit is the rug. This rail, not the liquidity
    # floor, is usually the binding one (policy.py's own comment).
    if candidate_sol > 0 and candidate_sol / liq > entry.max_position_to_liquidity_pct / 100.0:
        return "position_too_large_vs_liquidity"

    age = sig.get("age_ms")
    if age is None:
        return "missing_age"
    if age < entry.min_age_ms:
        # Never buy the listing event: on the reference token price gapped
        # -32.4% within 8 seconds of migration and no stop could have saved it.
        return "token_too_young"

    n_bars = sig.get("n_bars")
    if n_bars is None or n_bars < entry.min_bars:
        return "not_enough_bars"

    # Breadth. Null = UNMEASURED, not zero (schema.py's load-bearing invariant).
    ub = sig.get("unique_buyers_10")
    if ub is None:
        return "missing_buyer_breadth"
    if ub < entry.min_unique_buyers_10:
        return "too_few_unique_buyers"
    lbs = sig.get("largest_buyer_share_10")
    if lbs is None:
        return "missing_buyer_concentration"
    if lbs > entry.max_largest_buyer_share_10:
        # Ten wallets buying beats one wallet buying ten times.
        return "single_wallet_dominates_buying"

    thp = sig.get("top_holder_pct")
    if thp is not None and entry.max_top_holder_pct is not None \
            and thp > entry.max_top_holder_pct:
        return "top_holder_too_concentrated"

    if not cost_floor_ok(candidate_sol, band.upper_multiple,
                         cost, entry.min_cost_floor_multiple):
        return "below_cost_floor"

    return None


def _gate_rejections(features: Dict[str, Optional[float]], entry: EntrySpec) -> Optional[str]:
    """The accumulation thesis, as vetoes on measured quantities.
    Missing core flows fail closed -- None is not 0."""

    if entry.gate_require_positive_netflow:
        nf = features.get(F_NETFLOW_K.format(k=entry.gate_netflow_k))
        if nf is None:
            return "gate_flow_unmeasured"
        if nf <= 0:
            return "gate_netflow_not_positive"

    if entry.gate_require_buyer_breadth:
        br = features.get(F_BREADTH_K)
        if br is None:
            return "gate_breadth_unmeasured"
        if br <= 0:
            return "gate_seller_breadth_dominates"

    div = features.get(F_DIVERGENCE)
    if div is not None and div > entry.gate_max_flow_price_divergence:
        # Price rising while flow is negative: the tape is not paying for the
        # move. Flow/price divergence substitutes a MEASURED quantity for an
        # inferred one -- this is the strong condition in the gate.
        return "gate_price_flow_divergence"

    slope = features.get(F_LIQ_SLOPE)
    if slope is None:
        return "gate_liquidity_unmeasured"
    if slope < entry.gate_min_liq_slope_10:
        # Liquidity draining is how a floor disappears while you hold.
        return "gate_liquidity_draining"

    rsi = features.get(F_RSI)
    if rsi is not None and rsi > entry.gate_max_rsi:
        # RSI is a veto, never a trigger (docs/ML.md: RSI<30->buy buys all
        # the way down; the symmetric error is buying the top of the spike).
        return "gate_overextended"

    sold = features.get(F_CREATOR_SOLD)
    if sold is not None and sold > entry.gate_max_creator_sold_over_liq:
        return "gate_creator_dumping"

    return None


def _kelly_sol(p: float, band: BandSpec, entry: EntrySpec) -> float:
    """f* = (p*b - (1-p))/b, scaled by kelly_fraction and clamped to the
    max position fraction. Returns the FRACTION of capital (in [0, 1])."""
    b = band.payoff_ratio
    if b <= 0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    if f <= 0:
        return 0.0
    return min(f * entry.kelly_fraction, entry.max_position_fraction)


def decide_entry(
    features: Dict[str, Optional[float]],
    signals: Dict,
    model: Optional[ModelArtifact],
    spec: BotSpec,
    band: BandSpec,
    cost: CostModel,
    capital_sol: float,
    now_ms: int,
    mint: str = "",
) -> Decision:
    """The whole entry decision. Pure. See module docstring for the order."""

    entry = spec.entry

    candidate = min(entry.max_position_fraction * capital_sol,
                    (signals.get("liquidity_sol") or 0.0)
                    * entry.max_position_to_liquidity_pct / 100.0)

    rail_fail = _rail_rejections(signals, features, band, spec, entry,
                                 max(candidate, 1e-9), cost)
    if rail_fail is not None:
        return Decision(mint=mint, ts_ms=now_ms, action="reject",
                        reason=rail_fail, layer="rail", band=band.name)

    gate_fail = _gate_rejections(features, entry)
    if gate_fail is not None:
        return Decision(mint=mint, ts_ms=now_ms, action="reject",
                        reason=gate_fail, layer="gate", band=band.name)

    if model is None:
        if entry.require_model:
            return Decision(mint=mint, ts_ms=now_ms, action="abstain",
                            reason="no_model_available", layer="model",
                            band=band.name)
        # Rule-only mode: enter flat and SAY SO. A fallback that looks like a
        # model decision is how an unvalidated number acquires authority.
        if candidate < 1e-9:
            return Decision(mint=mint, ts_ms=now_ms, action="abstain",
                            reason="size_floor", layer="sizing", band=band.name)
        return Decision(mint=mint, ts_ms=now_ms, action="enter",
                        reason="no_model_flat_size", layer="sizing",
                        band=band.name, size_sol=candidate,
                        size_cap_reason="flat_fallback")

    pred = model.predict(features, band=band.name)
    p, cred = pred["p_calibrated"], pred["credibility"]

    if cred < entry.min_credibility:
        # The model has not seen anything like this. An out-of-distribution
        # prediction is a guess wearing a probability's clothes.
        return Decision(mint=mint, ts_ms=now_ms, action="abstain",
                        reason="out_of_distribution", layer="conformal",
                        band=band.name, p_raw=pred["p_raw"], p_calibrated=p,
                        credibility=cred)

    threshold = band.breakeven_win_rate + entry.probability_margin
    if p < threshold:
        return Decision(mint=mint, ts_ms=now_ms, action="abstain",
                        reason="below_probability_threshold", layer="model",
                        band=band.name, p_raw=pred["p_raw"], p_calibrated=p,
                        credibility=cred)

    frac = _kelly_sol(p, band, entry)
    size = frac * capital_sol
    cap_reason = None
    if size > candidate:
        size, cap_reason = candidate, "pool_depth"
    if size < 1e-9:
        return Decision(mint=mint, ts_ms=now_ms, action="abstain",
                        reason="size_floor", layer="sizing", band=band.name,
                        p_raw=pred["p_raw"], p_calibrated=p, credibility=cred)

    if not cost_floor_ok(size, band.upper_multiple, cost,
                         entry.min_cost_floor_multiple):
        return Decision(mint=mint, ts_ms=now_ms, action="abstain",
                        reason="size_below_cost_floor", layer="sizing",
                        band=band.name, p_raw=pred["p_raw"], p_calibrated=p,
                        credibility=cred, size_sol=size)

    return Decision(mint=mint, ts_ms=now_ms, action="enter", reason=None,
                    layer=None, band=band.name, p_raw=pred["p_raw"],
                    p_calibrated=p, credibility=cred, size_sol=size,
                    size_cap_reason=cap_reason)
