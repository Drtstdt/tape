"""Per-token live state. The curve state is EXACT from every event (virtual
reserves after the trade), so a missed trade never corrupts prices; holder,
bundle and bar statistics are computed from OBSERVED trades only (the first
moments after create can be missed while the subscription starts) -- recorded
as `first_observed_slot` so the gap is visible."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..bars import band_bar_threshold

Q_INIT = 30.0


@dataclass
class TokenState:
    mint: str
    created_ms: int
    creator: str
    name: str = ""
    symbol: str = ""
    uri: str = ""
    bonding_curve: str = ""
    create_slot: int = 0
    dev_initial_tokens: float = 0.0
    vsol: float = Q_INIT
    vtok: float = 1_073_000_000.0
    k: float = 0.0
    meta: Optional[dict] = None
    create_rails: List[str] = field(default_factory=list)
    holders: Dict[str, float] = field(default_factory=lambda: defaultdict(float))
    creator_sold_tokens: float = 0.0
    first_observed_slot: Optional[int] = None
    first_slot_other_tokens: float = 0.0
    n_trades: int = 0
    bar_threshold: Optional[float] = None
    bar_acc: float = 0.0
    bars: int = 0
    last_slot: int = 0
    last_ms: int = 0
    graduated: bool = False
    seen: set = field(default_factory=set)

    def __post_init__(self):
        if self.creator and self.dev_initial_tokens:
            self.holders[self.creator] += self.dev_initial_tokens
        if not self.k:
            self.k = self.vsol * self.vtok

    @property
    def q_gain(self) -> float:
        return self.vsol - Q_INIT

    def holding(self, wallet) -> float:
        return max(self.holders.get(wallet, 0.0), 0.0)

    def top_holders_share(self, n=10) -> float:
        vals = sorted((v for v in self.holders.values() if v > 0), reverse=True)[:n]
        return sum(vals) / 1_000_000_000.0

    def first_slot_other_share(self) -> float:
        return self.first_slot_other_tokens / 1_000_000_000.0

    def on_trade(self, t, recv_ms: int) -> List[int]:
        """Apply one trade; returns the list of bar counts closed by it (0 or 1 item)."""
        key = (t.sig, t.idx)
        if key in self.seen:
            return []
        self.seen.add(key)
        self.n_trades += 1
        if self.first_observed_slot is None:
            self.first_observed_slot = t.slot
        self.vsol, self.vtok, self.k = t.vsol, t.vtok, t.vsol * t.vtok
        self.last_slot, self.last_ms = t.slot, recv_ms
        if t.real_tok <= 0:
            self.graduated = True
        sgn = 1.0 if t.is_buy else -1.0
        self.holders[t.user] += sgn * t.tokens
        if t.user == self.creator and not t.is_buy:
            self.creator_sold_tokens += t.tokens
        if t.is_buy and t.user != self.creator and t.slot <= max(self.create_slot, self.first_observed_slot):
            self.first_slot_other_tokens += t.tokens
        if self.bar_threshold is None:
            self.bar_threshold = band_bar_threshold(t.vsol, 0.01)
        self.bar_acc += t.sol
        if self.bar_acc >= self.bar_threshold:
            self.bar_acc = 0.0
            self.bars += 1
            return [self.bars]
        return []
