"""Paper positions, many configs at once, on the live curve state.

Same economics as tape.outcomes.simulate_anchored: our SOL sits on top of the
observed market path; buy pays fee_b on top; a sale receives gross/(1+fee_s);
a fixed tip per transaction. The reaction delay is counted in SLOTS: an order
decided at slot s fills at the state after the last trade with slot <= s + L.
Live, that state is known when the first trade with slot > s + L arrives, or
when the wall clock passes (L * 0.4 s + grace) without one."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SLOT_S = 0.4


@dataclass
class Config:
    cid: str
    entry: str              # entry rule name
    tp: float
    sl: float
    horizon_ms: int
    size: float = 0.5
    latency_slots: int = 2
    tip_sol: float = 0.001


@dataclass
class Position:
    cfg: Config
    mint: str
    decided_slot: int
    decided_ms: int
    fee_b: float
    fee_s: float
    state: str = "pending_entry"       # pending_entry | open | pending_exit | closed
    cand_vsol: float = 0.0
    cand_k: float = 0.0
    entry_vsol: float = 0.0
    k: float = 0.0
    sol_in: float = 0.0
    N: float = 0.0
    entry_ms: int = 0
    exit_decided_slot: int = 0
    exit_decided_ms: int = 0
    exit_reason: str = ""
    net: Optional[float] = None
    exit_ms: int = 0
    last_vsol: float = 0.0
    extra: dict = field(default_factory=dict)

    def net_at(self, vsol: float) -> float:
        c = self.cfg
        qq = vsol + self.sol_in
        gross = qq - self.k / (self.k / qq + self.N)
        return (gross / (1.0 + self.fee_s) - c.tip_sol) / (c.size + c.tip_sol) - 1.0

    def _fill_entry(self, vsol, k, now_ms):
        c = self.cfg
        self.entry_vsol, self.k = vsol, k
        self.sol_in = c.size / (1.0 + self.fee_b)
        self.N = k / vsol - k / (vsol + self.sol_in)
        self.entry_ms, self.state, self.last_vsol = now_ms, "open", vsol

    def _fill_exit(self, vsol, now_ms):
        self.net, self.exit_ms, self.state = self.net_at(vsol), now_ms, "closed"

    def on_trade(self, t, now_ms: int, graduated: bool = False) -> None:
        """t: the new trade (state AFTER it). Called for every trade after the decision."""
        L = self.cfg.latency_slots
        if self.state == "pending_entry" and graduated:
            self.state, self.exit_reason, self.exit_ms = "closed", "NOENTRY", now_ms   # no trade possible
            return
        if self.state == "pending_entry":
            if t.slot <= self.decided_slot + L:
                self.cand_vsol, self.cand_k = t.vsol, t.k
                return
            self._fill_entry(self.cand_vsol, self.cand_k, now_ms)
        if self.state == "pending_exit":
            if t.slot <= self.exit_decided_slot + L and not graduated:
                self.last_vsol = t.vsol
                return
            self._fill_exit(self.last_vsol, now_ms)
            return
        if self.state != "open":
            return
        if graduated:
            self.last_vsol = t.vsol
            self.exit_reason = "GRAD"
            self._fill_exit(t.vsol, now_ms)
            return
        if now_ms - self.entry_ms > self.cfg.horizon_ms:
            self.exit_reason = "TIME"
            self._fill_exit(self.last_vsol, now_ms)          # last state before the deadline
            return
        self.last_vsol = t.vsol
        v = self.net_at(t.vsol)
        if v >= self.cfg.tp or v <= self.cfg.sl:
            self.exit_reason = "TP" if v >= self.cfg.tp else "SL"
            self.state, self.exit_decided_slot, self.exit_decided_ms = "pending_exit", t.slot, now_ms

    def on_clock(self, now_ms: int) -> None:
        grace = int((self.cfg.latency_slots * SLOT_S + 0.6) * 1000)
        if self.state == "pending_entry" and now_ms - self.decided_ms > grace:
            self._fill_entry(self.cand_vsol, self.cand_k, now_ms)
        elif self.state == "pending_exit" and now_ms - self.exit_decided_ms > grace:
            self._fill_exit(self.last_vsol, now_ms)
        elif self.state == "open" and now_ms - self.entry_ms > self.cfg.horizon_ms:
            self.exit_reason = "TIME"
            self._fill_exit(self.last_vsol, now_ms)


def open_position(cfg: Config, tok, decided_slot: int, now_ms: int, fee_b=0.0125, fee_s=0.0125) -> Position:
    p = Position(cfg, tok.mint, decided_slot, now_ms, fee_b, fee_s)
    p.cand_vsol, p.cand_k = tok.vsol, tok.k            # state after the deciding trade
    return p
