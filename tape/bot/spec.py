"""The bot specification -- every threshold in one place.

`config/bot.yaml` is the single source of truth; this module loads and
validates it. Two disciplines carried over from the rest of `tape/`:

  1. A parameter's STATUS is part of its identity. `unvalidated` means "my
     starting value, not yet fitted against a measurement". A parameter
     nobody can trace to a measurement must not quietly acquire authority
     by being old.
  2. The tunable knobs (probability margin, Kelly fraction) carry hard
     bounds that the auto-correct layer may never cross. Auto-correction
     is a bounded local search, not a free hand.

This module does no I/O beyond reading the YAML file in `load_spec()` --
everything else is pure dataclass validation, so tests can construct specs
directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

VALID_STATUS = ("unvalidated", "fitted", "frozen")


@dataclass(slots=True)
class BandSpec:
    """One liquidity band. The band chooses the exit economics: deeper pools
    get longer horizons and smaller barriers, per docs/ML.md's table (my
    values; status unvalidated)."""

    name: str
    min_liquidity_sol: float          # hard rail floor, quote-denominated (SOL)
    upper_multiple: float             # take-profit barrier
    lower_pct: float                  # hard stop (negative)
    horizon_ms: int                   # time stop
    tp_fraction: float                # fraction sold at the take-profit partial
    trail_pct: float                  # trail distance, active only AFTER the partial
    cooldown_ms: int = 60_000         # no re-entry on the same mint for this long
    status: str = "unvalidated"

    def __post_init__(self) -> None:
        if self.min_liquidity_sol < 0:
            raise ValueError("min_liquidity_sol must be >= 0")
        if self.upper_multiple <= 1.0:
            raise ValueError("upper_multiple must exceed 1.0")
        if not -1.0 < self.lower_pct < 0.0:
            raise ValueError("lower_pct must be negative and above -1.0")
        if self.horizon_ms <= 0:
            raise ValueError("horizon_ms must be positive")
        if not 0.0 < self.tp_fraction < 1.0:
            raise ValueError("tp_fraction must be strictly between 0 and 1")
        if not 0.0 < self.trail_pct < 1.0:
            raise ValueError("trail_pct must be strictly between 0 and 1")
        if self.cooldown_ms < 0:
            raise ValueError("cooldown_ms must be >= 0")
        if self.status not in VALID_STATUS:
            raise ValueError(f"status must be one of {VALID_STATUS}")

    @property
    def payoff_ratio(self) -> float:
        """Kelly b = (upper - 1) / |lower|: how many units you win per unit
        risked, in barrier terms."""
        return (self.upper_multiple - 1.0) / abs(self.lower_pct)

    @property
    def breakeven_win_rate(self) -> float:
        """p such that p*(upper-1) + (1-p)*lower == 0."""
        gain = self.upper_multiple - 1.0
        loss = abs(self.lower_pct)
        return loss / (gain + loss) if (gain + loss) > 0 else 0.5


@dataclass(slots=True)
class EntrySpec:
    """Rails, accumulation gate, model gate, sizing."""

    # -- hard rails (fail closed; missing safety field = reject) -----------
    min_age_ms: int = 150_000          # never buy the listing event (docs/PLAN.md)
    min_bars: int = 5                  # enough tape for features to mean anything
    min_unique_buyers_10: float = 3.0
    max_largest_buyer_share_10: float = 0.6
    max_top_holder_pct: Optional[float] = 80.0   # optional field; enforced if measured
    max_position_to_liquidity_pct: float = 1.0   # our own impact cap (thin pool = our exit is the rug)
    min_cost_floor_multiple: float = 3.0         # expected gain must clear round trip x this

    # -- accumulation gate (the strategy thesis, docs/BOT.md) -------------
    # Buy only while accumulation is OBSERVED, never on the news event.
    # Each check is a veto on a MEASURED quantity; unmeasured core flows
    # fail closed (None is not 0, schema.py).
    gate_netflow_k: int = 10
    gate_breadth_k: int = 10
    gate_require_positive_netflow: bool = True    # netflow_k_over_liq > 0
    gate_require_buyer_breadth: bool = True       # buyer_seller_breadth_k > 0
    gate_max_flow_price_divergence: float = 0.0   # divergence <= this (no price-up-on-sell-flow)
    gate_min_liq_slope_10: float = -0.05          # liquidity not draining fast
    gate_max_rsi: float = 85.0                    # not overextended (veto only; None skips)
    gate_max_creator_sold_over_liq: float = 0.10  # creator not dumping (None skips)

    # -- model gate --------------------------------------------------------
    probability_margin: float = 0.04    # demand a real edge over the band breakeven
    min_credibility: float = 0.15       # conformal floor: "have I seen this before"
    require_model: bool = False         # False: rule-only mode enters flat and LOGS it

    # -- sizing ------------------------------------------------------------
    kelly_fraction: float = 0.10        # tenth-Kelly: p is estimated, not known
    max_position_fraction: float = 0.02  # of capital, per position
    capital_sol: float = 100.0

    # -- self-correct bounds: the auto-tuner may move these, never past these.
    margin_bounds: tuple = (0.02, 0.12)
    kelly_bounds: tuple = (0.03, 0.25)
    status: str = "unvalidated"

    def __post_init__(self) -> None:
        if self.min_age_ms < 0:
            raise ValueError("min_age_ms must be >= 0")
        if self.min_bars < 1:
            raise ValueError("min_bars must be >= 1")
        if self.min_cost_floor_multiple <= 0:
            raise ValueError("min_cost_floor_multiple must be positive")
        if not 0.0 < self.max_position_to_liquidity_pct < 100.0:
            raise ValueError("max_position_to_liquidity_pct out of range")
        if not 0.0 < self.max_largest_buyer_share_10 <= 1.0:
            raise ValueError("max_largest_buyer_share_10 must be in (0, 1]")
        if not 0.0 <= self.probability_margin <= 0.5:
            raise ValueError("probability_margin out of range")
        if not 0.0 < self.kelly_fraction <= 1.0:
            raise ValueError("kelly_fraction out of range")
        if not 0.0 < self.max_position_fraction <= 1.0:
            raise ValueError("max_position_fraction out of range")
        if self.status not in VALID_STATUS:
            raise ValueError(f"status must be one of {VALID_STATUS}")


@dataclass(slots=True)
class ExitSpec:
    """Exit state machine parameters. The ORDER and the invariants live in
    exits.py and are load-bearing (v3 paid for them, see docs/MIGRATION.md)."""

    # Category A -- structural, overrides everything. Sustained only:
    # v3's single-observation exits fired 428 and 1,174 times at -14.8% and
    # -8.5%; a rule that acts on n=1 has maximum variance by construction.
    cat_a_divergence_bars: int = 2      # liquidity/price divergence bars in a row
    cat_a_liq_drawdown: float = -0.35   # OR liquidity down this far from its own peak
    # After the partial: the remainder rides a trail but is floored at
    # breakeven -- a winner never becomes a loser.
    remainder_floor_multiple: float = 1.0
    status: str = "unvalidated"

    def __post_init__(self) -> None:
        if self.cat_a_divergence_bars < 1:
            raise ValueError("cat_a_divergence_bars must be >= 1")
        if not -1.0 < self.cat_a_liq_drawdown < 0.0:
            raise ValueError("cat_a_liq_drawdown must be negative")
        if self.status not in VALID_STATUS:
            raise ValueError(f"status must be one of {VALID_STATUS}")


@dataclass(slots=True)
class AutoCorrectSpec:
    """The optional self-correcting layer. Every change it makes is bounded,
    logged with a reason, and reversible by editing the spec back."""

    enabled: bool = False               # opt-in: --autocorrect
    psi_halve: float = 0.20             # per-feature PSI -> halve sizes
    psi_halt: float = 0.30              # per-feature PSI -> stop new entries
    psi_window_rows: int = 2000         # rolling window for the live distribution
    tune_min_trades: int = 30           # closed trades before the tuner speaks
    tune_window: int = 30               # rolling trades the tuner evaluates
    tune_hysteresis: float = 0.02       # win-rate slack to stop oscillation
    recal_buffer_cap: int = 4000        # rolling (p_raw, outcome) buffer
    recal_min_new: int = 250            # new outcomes before a refit
    daily_loss_cap_sol: float = 10.0    # stop new entries for the day
    drawdown_halt_pct: float = 0.25     # halt until manual reset
    kill_switch_path: str = "data/bot/kill_switch"
    log_path: str = "data/bot/autocorrect_log.csv"
    status: str = "unvalidated"

    def __post_init__(self) -> None:
        if not 0.0 < self.psi_halve < self.psi_halt:
            raise ValueError("need 0 < psi_halve < psi_halt")
        if self.tune_min_trades < self.tune_window:
            raise ValueError("tune_min_trades must be >= tune_window")
        if self.daily_loss_cap_sol <= 0:
            raise ValueError("daily_loss_cap_sol must be positive")
        if self.status not in VALID_STATUS:
            raise ValueError(f"status must be one of {VALID_STATUS}")


@dataclass(slots=True)
class BotSpec:
    """Everything the engine needs, resolved base -> band."""

    name: str = "tape-one"
    version: int = 1
    entry: EntrySpec = field(default_factory=EntrySpec)
    exits: ExitSpec = field(default_factory=ExitSpec)
    autocorrect: AutoCorrectSpec = field(default_factory=AutoCorrectSpec)
    bands: list = field(default_factory=lambda: [
        BandSpec(name="B_small", min_liquidity_sol=15.0, upper_multiple=2.0,
                 lower_pct=-0.40, horizon_ms=45 * 60_000, tp_fraction=0.5,
                 trail_pct=0.30, cooldown_ms=60_000),
        BandSpec(name="B_mid", min_liquidity_sol=75.0, upper_multiple=1.6,
                 lower_pct=-0.30, horizon_ms=3 * 3600_000, tp_fraction=0.5,
                 trail_pct=0.25, cooldown_ms=120_000),
        BandSpec(name="B_deep", min_liquidity_sol=300.0, upper_multiple=1.4,
                 lower_pct=-0.25, horizon_ms=12 * 3600_000, tp_fraction=0.5,
                 trail_pct=0.20, cooldown_ms=300_000),
    ])
    # costs -- measured values only; fixed cost is the one v3 never modelled.
    fee_pct: float = 0.01
    fixed_cost_sol: float = 0.0005
    extra_slippage_pct: float = 0.0
    # bar construction
    bar_fraction_of_liquidity: float = 0.01
    bar_floor_quote: float = 0.05
    # engine limits
    max_open_positions: int = 6
    status: str = "unvalidated"

    def __post_init__(self) -> None:
        names = [b.name for b in self.bands]
        if len(set(names)) != len(names):
            raise ValueError("band names must be unique")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if not 0.0 < self.fee_pct < 1.0:
            raise ValueError("fee_pct out of range")
        if self.fixed_cost_sol < 0:
            raise ValueError("fixed_cost_sol must be >= 0")
        self.bands.sort(key=lambda b: b.min_liquidity_sol)

    # -- band selection ----------------------------------------------------

    def band_for_liquidity(self, liquidity_sol: float) -> Optional[BandSpec]:
        """The highest-floor band the pool satisfies, or None (below all
        floors -> rails reject on liquidity)."""
        chosen = None
        for b in self.bands:
            if liquidity_sol >= b.min_liquidity_sol:
                chosen = b
        return chosen

    def band_by_name(self, name: str) -> BandSpec:
        for b in self.bands:
            if b.name == name:
                return b
        raise KeyError(f"no band named {name!r}")

    # -- auto-correct: apply a bounded change ------------------------------

    def apply_tune(self, param: str, new_value: float) -> Optional["BotSpec"]:
        """Return a NEW spec with the tuned value, or None if out of bounds.
        Never mutates in place -- the ledger references the version it ran on."""
        lo, hi = (self.entry.margin_bounds if param == "probability_margin"
                  else self.entry.kelly_bounds)
        if not lo <= new_value <= hi:
            return None
        return replace(self, entry=replace(self.entry, **{param: new_value}))


def _band_from_dict(d: dict) -> BandSpec:
    return BandSpec(**d)


def spec_to_dict(spec: BotSpec) -> dict:
    """YAML-shaped dict for saving / diffing. Tuples become lists."""
    out = {
        "name": spec.name, "version": spec.version, "status": spec.status,
        "fee_pct": spec.fee_pct, "fixed_cost_sol": spec.fixed_cost_sol,
        "extra_slippage_pct": spec.extra_slippage_pct,
        "bar_fraction_of_liquidity": spec.bar_fraction_of_liquidity,
        "bar_floor_quote": spec.bar_floor_quote,
        "max_open_positions": spec.max_open_positions,
        "entry": {
            k: (list(v) if isinstance(v, tuple) else v)
            for k, v in spec.entry.__dict__.items()
        },
        "exits": spec.exits.__dict__,
        "autocorrect": spec.autocorrect.__dict__,
        "bands": [b.__dict__ for b in spec.bands],
    }
    return out


def load_spec(path: str | Path = "config/bot.yaml") -> BotSpec:
    """Load and validate. Every key in the YAML must exist on the spec --
    a typo'd override is an error, never a silent no-op (ARCHITECTURE.md)."""
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    known_top = {"name", "version", "status", "fee_pct", "fixed_cost_sol",
                 "extra_slippage_pct", "bar_fraction_of_liquidity",
                 "bar_floor_quote", "max_open_positions", "entry", "exits",
                 "autocorrect", "bands"}
    unknown = set(raw) - known_top
    if unknown:
        raise ValueError(f"unknown top-level spec keys: {sorted(unknown)}")

    entry_raw = dict(raw.get("entry") or {})
    entry_fields = {f for f in EntrySpec.__dataclass_fields__}
    _check_keys("entry", entry_raw, entry_fields)

    exit_raw = dict(raw.get("exits") or {})
    _check_keys("exits", exit_raw, set(ExitSpec.__dataclass_fields__))

    auto_raw = dict(raw.get("autocorrect") or {})
    _check_keys("autocorrect", auto_raw, set(AutoCorrectSpec.__dataclass_fields__))

    band_fields = set(BandSpec.__dataclass_fields__)
    bands = []
    for b in raw.get("bands") or []:
        _check_keys(f"band {b.get('name')}", b, band_fields)
        bands.append(BandSpec(**b))

    core = {k: v for k, v in raw.items() if k in known_top - {"entry", "exits", "autocorrect", "bands"}}
    spec = BotSpec(**core, entry=EntrySpec(**entry_raw), exits=ExitSpec(**exit_raw),
                   autocorrect=AutoCorrectSpec(**auto_raw), bands=bands)
    return spec


def _check_keys(scope: str, d: dict, allowed: set) -> None:
    unknown = set(d) - allowed
    if unknown:
        raise ValueError(f"unknown keys in {scope}: {sorted(unknown)}")
