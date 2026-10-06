"""The engine: ONE loop, driven identically by the backtester and the live
bot. This is the "live bot IS the backtester" rule from docs/PLAN.md made
concrete -- the only difference between the two runs is which source hands
swaps to `on_swap`.

The loop (per bar, per mint):

    state.update(bar)
    if position open:  step_position -> exit action -> executor fill
    else:              risk/cooldown checks -> decide_entry() -> executor fill
    ledger:            EVERY decision (enter / abstain / reject) is a row

Abstentions are first-class rows with reason codes -- the tally of why the
bot did NOT act is the most informative output of any run, more than PnL.

Time exits are driven by `sweep(now_ms)`, separate from the tape: a token
that stops trading still gets exited (v3 lost 224 positions at -14.8% when
the tape went quiet and nothing could sell them).

The engine does no I/O except writing the ledgers on `save()` -- every
decision is a pure function of (bar, state, spec, model, now_ms).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Optional

from ..bars import BarBuilder, band_bar_threshold
from ..costs import CostModel
from ..features import TokenState
from ..model import ModelArtifact
from ..schema import Bar, CanonicalSwap, Decision
from .autocorrect import Autopilot
from .executors import PaperExecutor
from .exits import FULL, ExitAction, Position, apply_action, open_position, step_position
from .spec import BandSpec, BotSpec
from .strategy import build_signals, decide_entry

DECISION_COLUMNS = ["mint", "ts_ms", "action", "reason", "layer", "band",
                    "p_raw", "p_calibrated", "credibility", "size_sol",
                    "size_cap_reason", "drift"]
TRADE_COLUMNS = ["mint", "side", "ts_ms", "price", "quote_sol", "base_amount",
                 "fee_sol", "fixed_sol", "reason", "pnl_sol", "pnl_pct"]
EQUITY_COLUMNS = ["ts_ms", "total_realized", "n_entered", "n_closed"]


class Engine:
    def __init__(self, spec: BotSpec,
                 models: Optional[Dict[str, ModelArtifact]] = None,
                 executor: Optional[object] = None,
                 autocorrect: bool = False,
                 base_dir: str | Path = "data",
                 ledger_dir: Optional[str | Path] = None) -> None:
        self.spec = spec
        self.cost = CostModel(fee_pct=spec.fee_pct,
                              fixed_cost_sol=spec.fixed_cost_sol,
                              extra_slippage_pct=spec.extra_slippage_pct)
        self.models: Dict[str, ModelArtifact] = dict(models or {})
        self.executor = executor or PaperExecutor(self.cost)
        self.base_dir = Path(base_dir)
        self.ledger_dir = Path(ledger_dir) if ledger_dir else self.base_dir / "bot"

        # per-mint tape state
        self.states: Dict[str, TokenState] = {}
        self.bar_builders: Dict[str, BarBuilder] = {}
        self._threshold_set: Dict[str, bool] = {}
        self.positions: Dict[str, Position] = {}
        self.last_bar: Dict[str, Bar] = {}
        self.last_exit_ts: Dict[str, int] = {}

        # bookkeeping
        self.capital0 = spec.entry.capital_sol
        self.total_realized = 0.0
        self.n_entered = 0
        self.n_closed = 0
        self.equity_curve: List[Dict] = []
        self._decision_rows: List[Dict] = []
        self._trade_rows: List[Dict] = []

        self.autopilot = Autopilot(spec, self.models, self.base_dir)
        # The Autopilot's drift/tuner/recalibration obey the opt-in flag
        # internally; its RiskGuard (kill switch, daily loss cap, drawdown
        # halt) is ALWAYS on -- safety is not opt-in.

    # -- capital ------------------------------------------------------------

    def capital(self) -> float:
        return max(self.capital0 + self.total_realized, self.capital0 * 0.5)

    # -- tape feeding -------------------------------------------------------

    def begin_mint(self, mint: str, *, created_ts_ms: Optional[int] = None,
                   reputation=None, total_supply: Optional[float] = None,
                   creator: Optional[str] = None,
                   insiders: Optional[set] = None) -> None:
        if mint not in self.states:
            self.states[mint] = TokenState(mint, created_ts_ms=created_ts_ms,
                                           reputation=reputation,
                                           total_supply=total_supply)
        self.bar_builders[mint] = BarBuilder(
            "dollar", threshold=self.spec.bar_floor_quote,
            creator=creator, insiders=insiders or set())
        self._threshold_set[mint] = False

    def on_swap(self, swap: CanonicalSwap) -> None:
        if swap.mint not in self.bar_builders:
            self.begin_mint(swap.mint, created_ts_ms=swap.ts_ms)
        bb = self.bar_builders[swap.mint]
        # Once depth is known, bar size becomes a fraction of pool depth --
        # one bar is a comparable ECONOMIC event in a thin and a deep pool
        # (bars.py::band_bar_threshold). Rebuild only before pushing a swap.
        if not self._threshold_set[swap.mint] and swap.quote_reserve_after:
            depth = float(swap.quote_reserve_after)
            bb = BarBuilder(
                "dollar",
                threshold=band_bar_threshold(depth, self.spec.bar_fraction_of_liquidity,
                                             self.spec.bar_floor_quote))
            self.bar_builders[swap.mint] = bb
            self._threshold_set[swap.mint] = True
        bar = bb.push(swap)
        if bar is not None:
            self.on_bar(swap.mint, bar)

    def on_bar(self, mint: str, bar: Bar) -> None:
        st = self.states[mint]
        st.update(bar)
        self.last_bar[mint] = bar
        now_ms = bar.close_ts_ms
        feats = dict(st.features())

        if self.autopilot is not None:
            self.autopilot.observe_features(feats)

        pos = self.positions.get(mint)
        if pos is not None:
            self._step_position(mint, pos, bar, feats, now_ms)
            return
        self._decide(mint, st, bar, feats, now_ms)

    # -- the decision path --------------------------------------------------

    def _decide(self, mint: str, st: TokenState, bar: Bar,
                feats: Dict, now_ms: int) -> None:
        signals = build_signals(feats)
        liq = signals.get("liquidity_sol")
        band = self.spec.band_for_liquidity(liq) if liq is not None else None

        # engine-level abstentions (state the pure decide() cannot see)
        if self.autopilot is not None:
            blocked = self.autopilot.entry_block(now_ms)
            if blocked:
                self._decision(mint, now_ms, "abstain", blocked, "risk",
                               band.name if band else "", {})
                return
        if len(self.positions) >= self.spec.max_open_positions:
            self._decision(mint, now_ms, "abstain", "max_positions", "engine",
                           band.name if band else "", {})
            return
        if band is None:
            self._decision(mint, now_ms, "reject", "missing_liquidity", "rail", "", {})
            return
        last_exit = self.last_exit_ts.get(mint)
        if last_exit is not None and now_ms - last_exit < band.cooldown_ms:
            self._decision(mint, now_ms, "abstain", "cooldown", "engine",
                           band.name, {})
            return

        drift = self.autopilot.drift_action() if self.autopilot else "normal"
        if drift == "halt":
            self._decision(mint, now_ms, "abstain", "drift_halt", "risk",
                           band.name, {"drift": drift})
            return

        model = self.models.get(band.name)
        d: Decision = decide_entry(feats, signals, model, self.spec, band,
                                   self.cost, self.capital(), now_ms, mint=mint)
        if drift == "halve" and d.action == "enter" and d.size_sol is not None:
            d.size_sol *= 0.5
            d.size_cap_reason = "drift_halved"

        self._decision(mint, now_ms, d.action, d.reason, d.layer or "",
                       d.band or band.name, {
                           "p_raw": d.p_raw, "p_calibrated": d.p_calibrated,
                           "credibility": d.credibility, "size_sol": d.size_sol,
                           "size_cap_reason": d.size_cap_reason, "drift": drift,
                       })
        if d.action == "enter" and d.size_sol:
            self._enter(mint, band, bar, d)

    def _enter(self, mint: str, band: BandSpec, bar: Bar, d: Decision) -> None:
        fill = self.executor.fill_enter(mint, d.size_sol, bar.close, bar)
        if fill.base_amount <= 0 or fill.price <= 0:
            return  # failed fill: nothing bought; the decision row stays honest
        pos = open_position(mint, band, bar.close_ts_ms, fill.price,
                            fill.quote_sol, fill.base_amount)
        self.positions[mint] = pos
        self.n_entered += 1
        if self.autopilot is not None:
            self.autopilot.register_entry(mint, d.p_raw, d.p_calibrated, band.name)
        self._trade(mint, "enter", fill, reason=None, pnl=None)

    # -- the exit path ------------------------------------------------------

    def _step_position(self, mint: str, pos: Position, bar: Bar,
                       feats: Dict, now_ms: int) -> None:
        band = self.spec.band_by_name(pos.band)
        action: Optional[ExitAction] = step_position(pos, bar, feats, now_ms,
                                                     self.spec.exits, band)
        if action is None:
            return
        fill = self.executor.fill_exit(pos, action, bar)
        if action.fraction >= FULL:
            self._close_position(mint, pos, action, fill)
        else:
            apply_action(pos, action, fill.quote_sol)
            self._trade(mint, "exit_partial", fill, reason=action.reason, pnl=None)

    def _close_position(self, mint: str, pos: Position, action: ExitAction,
                        fill) -> None:
        apply_action(pos, action, fill.quote_sol)
        pnl_sol = pos.realized_sol if pos.realized_sol is not None else \
            fill.quote_sol - pos.size_sol
        self.total_realized += pnl_sol
        self.n_closed += 1
        self.last_exit_ts[mint] = action.ts_ms
        self.positions.pop(mint, None)
        self.equity_curve.append({"ts_ms": action.ts_ms,
                                  "total_realized": self.total_realized,
                                  "n_entered": self.n_entered,
                                  "n_closed": self.n_closed})
        self._trade(mint, "exit", fill, reason=action.reason,
                    pnl={"pnl_sol": pnl_sol,
                         "pnl_pct": pnl_sol / pos.size_sol if pos.size_sol else None})
        if self.autopilot is not None:
            self.autopilot.on_closed_trade(mint, action.ts_ms, pnl_sol)

    # -- clock: exits independent of the tape -------------------------------

    def sweep(self, now_ms: int) -> None:
        for mint, pos in list(self.positions.items()):
            if now_ms < pos.horizon_ts_ms:
                continue
            bar = self.last_bar.get(mint)
            if bar is None:
                continue
            action = ExitAction("time_stop", bar.close, FULL, now_ms)
            fill = self.executor.fill_exit(pos, action, bar)
            self._close_position(mint, pos, action, fill)

    def end_tape(self, mint: str) -> None:
        """Flush the open bar, then force-close any open position at the
        last observed price. A position the tape abandoned is real value the
        backtest must not pretend to still hold."""
        bb = self.bar_builders.get(mint)
        if bb is not None:
            last = bb.flush()
            if last is not None:
                self.on_bar(mint, last)
        pos = self.positions.get(mint)
        bar = self.last_bar.get(mint)
        if pos is not None and bar is not None:
            action = ExitAction("tape_end", bar.close, FULL, bar.close_ts_ms)
            fill = self.executor.fill_exit(pos, action, bar)
            self._close_position(mint, pos, action, fill)

    # -- ledger ---------------------------------------------------------------

    def _decision(self, mint: str, ts_ms: int, action: str, reason: Optional[str],
                  layer: str, band: str, d: Dict) -> None:
        self._decision_rows.append({
            "mint": mint, "ts_ms": ts_ms, "action": action,
            "reason": reason or "", "layer": layer, "band": band,
            "p_raw": d.get("p_raw"), "p_calibrated": d.get("p_calibrated"),
            "credibility": d.get("credibility"), "size_sol": d.get("size_sol"),
            "size_cap_reason": d.get("size_cap_reason") or "",
            "drift": d.get("drift") or "",
        })

    def _trade(self, mint: str, side: str, fill, reason: Optional[str],
               pnl: Optional[Dict]) -> None:
        self._trade_rows.append({
            "mint": mint, "side": side, "ts_ms": fill.ts_ms, "price": fill.price,
            "quote_sol": fill.quote_sol, "base_amount": fill.base_amount,
            "fee_sol": fill.fee_sol, "fixed_sol": fill.fixed_sol,
            "reason": reason or "", "pnl_sol": pnl["pnl_sol"] if pnl else None,
            "pnl_pct": pnl["pnl_pct"] if pnl else None,
        })

    def save(self) -> Dict[str, Path]:
        """Write the three ledgers. Idempotent: appends to existing files."""
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        written = {}
        written["decisions"] = self._append_csv(
            self.ledger_dir / "decisions.csv", DECISION_COLUMNS, self._decision_rows)
        written["trades"] = self._append_csv(
            self.ledger_dir / "trades.csv", TRADE_COLUMNS, self._trade_rows)
        written["equity"] = self._append_csv(
            self.ledger_dir / "equity.csv", EQUITY_COLUMNS, self.equity_curve)
        self._decision_rows.clear()
        self._trade_rows.clear()
        self.equity_curve.clear()
        if self.autopilot is not None:
            self.autopilot.save_log()
        return written

    @staticmethod
    def _append_csv(path: Path, columns: List[str], rows: List[Dict]) -> Path:
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            if new:
                writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in columns})
        return path

    # -- introspection -------------------------------------------------------

    def summary(self) -> Dict:
        open_pos = {m: p for m, p in self.positions.items() if not p.closed}
        return {
            "n_entered": self.n_entered,
            "n_closed": self.n_closed,
            "n_open": len(open_pos),
            "total_realized_sol": round(self.total_realized, 6),
            "capital": round(self.capital(), 2),
            "decision_rows": len(self._decision_rows),
        }
