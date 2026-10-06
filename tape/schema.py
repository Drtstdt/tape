"""Canonical records. Every adapter converts to these and nothing else.

Two invariants are load-bearing and were both learned the expensive way in v3:

1. ``wallet is None`` means UNATTRIBUTED, not "some wallet". An unattributed
   trade counts in full for every volume-shaped field and contributes NOTHING
   to unique-buyer counts or largest-buyer share. Giving anonymous trades
   synthetic ids reports perfectly broad buying and opens the breadth gate;
   giving them one shared id reports a single whale and rejects everything.
   Both are worse than the gap.

2. ``None`` is not ``0``. A zero net flow says "we watched and nothing
   happened". A null says "we were not watching". Collapsing the two is how a
   fail-closed gate silently becomes an open door -- this exact bug voided a
   live run on 2026-09-18.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional

BUY = "buy"
SELL = "sell"

WSOL = "So11111111111111111111111111111111111111112"


@dataclass(slots=True)
class CanonicalSwap:
    """One swap, from the base token's point of view.

    ``side`` is always relative to the BASE token: ``buy`` means base was
    acquired and quote was spent. Getting this backwards silently inverts every
    flow feature in the system, so adapters must assert it against a known
    transaction before being trusted (see tools/check_side_convention.py).
    """

    mint: str
    venue: str
    pool: str
    ts_ms: int
    slot: int
    sig: str
    side: str
    base_amount: float
    quote_amount: float
    quote_mint: str
    price: float
    wallet: Optional[str] = None
    base_reserve_after: Optional[float] = None
    quote_reserve_after: Optional[float] = None
    source: str = "unknown"

    def __post_init__(self) -> None:
        if self.side not in (BUY, SELL):
            raise ValueError(f"side must be {BUY!r} or {SELL!r}, got {self.side!r}")
        if self.base_amount < 0 or self.quote_amount < 0:
            raise ValueError("amounts are magnitudes; encode direction in `side`")

    @property
    def dedup_key(self) -> tuple:
        # Two sources feeding the same pool is the point (corroboration), so
        # duplicates are expected and ingestion must be idempotent.
        return (self.sig, self.mint, self.side, round(self.base_amount, 12))

    @property
    def signed_quote(self) -> float:
        return self.quote_amount if self.side == BUY else -self.quote_amount


@dataclass(slots=True)
class Bar:
    """A closed bar. ``kind`` is dollar | volume | time.

    ``*_at_open`` fields exist so a consumer never has to reach backwards into
    another bar to know what changed across this one -- reaching backwards is
    how windows silently become cumulative.
    """

    mint: str
    venue: str
    kind: str
    open_ts_ms: int
    close_ts_ms: int
    open: float
    high: float
    low: float
    close: float
    buy_quote: float
    sell_quote: float
    buy_count: int
    sell_count: int
    trade_count: int
    attributed_count: int
    unique_buyers: Optional[int]
    unique_sellers: Optional[int]
    largest_buyer_share: Optional[float]
    buy_hhi: Optional[float]
    quote_reserve_close: Optional[float] = None
    base_reserve_close: Optional[float] = None
    quote_reserve_open: Optional[float] = None
    creator_sold_quote: float = 0.0
    insider_sold_quote: float = 0.0

    @property
    def net_flow(self) -> float:
        return self.buy_quote - self.sell_quote

    @property
    def duration_ms(self) -> int:
        return self.close_ts_ms - self.open_ts_ms

    @property
    def ret(self) -> float:
        return (self.close / self.open - 1.0) if self.open > 0 else 0.0

    @property
    def attribution_complete(self) -> bool:
        return self.attributed_count == self.trade_count


@dataclass(slots=True)
class TokenMeta:
    mint: str
    launchpad: Optional[str] = None
    creator: Optional[str] = None
    created_ts_ms: Optional[int] = None
    decimals: Optional[int] = None
    total_supply: Optional[float] = None
    mint_authority_renounced: Optional[bool] = None
    freeze_authority_renounced: Optional[bool] = None
    lp_locked_pct: Optional[float] = None
    top_holder_pct: Optional[float] = None
    named_risks: tuple = field(default_factory=tuple)


@dataclass(slots=True)
class Decision:
    """The full record of one decision, including the ones not to act.

    The tally of WHY the bot did not act is the most informative output of any
    given run -- more than the PnL -- so abstentions are first-class rows with
    their own reason codes, not silent returns.
    """

    mint: str
    ts_ms: int
    action: str                  # "enter" | "abstain" | "reject"
    reason: Optional[str]
    layer: Optional[str]         # "rail" | "model" | "conformal" | "sizing"
    band: Optional[str] = None
    p_raw: Optional[float] = None
    p_calibrated: Optional[float] = None
    credibility: Optional[float] = None
    size_sol: Optional[float] = None
    size_cap_reason: Optional[str] = None
    top_features: tuple = field(default_factory=tuple)

    def to_row(self) -> dict:
        return asdict(self)


SWAP_COLUMNS = [f.name for f in CanonicalSwap.__dataclass_fields__.values()]  # type: ignore[attr-defined]
BAR_COLUMNS = [f.name for f in Bar.__dataclass_fields__.values()]  # type: ignore[attr-defined]
