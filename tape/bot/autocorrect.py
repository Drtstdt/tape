"""The optional self-correcting layer.

Four mechanisms, each bounded and each logged (docs/BOT.md):

  1. DriftMonitor -- per-feature PSI of the live stream against the training
     distribution (tape/model.py::psi). Above `psi_halve`, sizes are halved;
     above `psi_halt`, new entries stop and a retrain is flagged. v1 saw PSI
     0.80 within 48 hours; a model predicting a world that no longer exists
     must stop spending money in it.

  2. Recalibrator -- the isotonic mapping is refit on the union of the
     original calibration points and a rolling buffer of recent (p_raw,
     outcome) pairs, so "when it says 35%, it wins 35%" keeps meaning live.
     The conformal scores are left untouched: credibility is a property of
     the fitting distribution, not of the market's mood.

  3. AutoTuner -- adjusts probability_margin and kelly_fraction within
     PRE-REGISTERED bounds (spec.entry.margin_bounds / kelly_bounds) when
     the realized win rate over the last window is outside the band's
     breakeven by more than the hysteresis. A hysteresis + minimum sample
     are the anti-churn devices; the bounds are the anti-suicide device.

  4. RiskGuard -- a daily loss cap, a drawdown halt, and a kill-switch
     file. These can only STOP trading, never loosen anything. Deleting the
     files is the manual reset, which is exactly as manual as it should be.

Everything is OPT-IN (spec.autocorrect.enabled / --autocorrect). Every
change is appended to the autocorrect log with the old value, the new
value, and the reason -- a bot that changes its own mind must leave a
paper trail at least as good as a human's.
"""

from __future__ import annotations

import csv
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

from ..model import ModelArtifact, psi
from .spec import AutoCorrectSpec, BotSpec


@dataclass
class TuneChange:
    ts: int
    param: str
    old: float
    new: float
    reason: str
    spec_version: int


class DriftMonitor:
    """PSI per feature, streaming. Missing values are skipped (they are
    unmeasured, not observations of anything)."""

    def __init__(self, artifact: Optional[ModelArtifact], spec: AutoCorrectSpec) -> None:
        self.spec = spec
        self.baseline = artifact.train_quantiles if artifact is not None else {}
        self._windows: Dict[str, Deque[float]] = {}
        for name, qs in self.baseline.items():
            self._windows[name] = deque(maxlen=spec.psi_window_rows)

    def observe(self, features: Dict[str, Optional[float]]) -> None:
        for name, window in self._windows.items():
            v = features.get(name)
            if v is not None:
                window.append(float(v))

    def status(self) -> Dict:
        """max PSI across features + the resulting action.

        Values outside the training range are CLIPPED into its edge bins
        before the PSI is computed -- an observation beyond everything the
        model was fitted on is the strongest possible drift signal, and
        `tape/model.py::psi` (which histograms with the training quantiles
        as bin edges) silently discards out-of-range values instead. The
        clip is deliberately local to this module: `psi()` itself stays
        untouched for its other callers."""
        worst = 0.0
        for name, qs in self.baseline.items():
            window = self._windows.get(name)
            if window is None or len(window) < 50:
                continue
            cur = np.asarray(list(window), dtype=float)
            edges = np.asarray(qs, dtype=float)
            cur = np.clip(cur, edges.min(), edges.max())
            worst = max(worst, psi(edges, cur))
        if worst >= self.spec.psi_halt:
            action = "halt"
        elif worst >= self.spec.psi_halve:
            action = "halve"
        else:
            action = "normal"
        return {"max_psi": float(worst), "action": action}


class Recalibrator:
    """Rolling isotonic refit on (calibration points + recent outcomes).

    The original calibrator is an IsotonicRegression whose fitted step
    function is exposed as (X_, y_); we refit on the union of those points
    and the rolling buffer, with isotonic's own constraints. Returns a NEW
    artifact (the old one is never mutated), or None when there is nothing
    new enough to justify a refit."""

    def __init__(self, spec: AutoCorrectSpec) -> None:
        self.spec = spec
        self._buffer: Deque[Tuple[float, int]] = deque(maxlen=spec.recal_buffer_cap)
        self._since_refit = 0

    def add(self, p_raw: float, won: bool) -> None:
        self._buffer.append((float(p_raw), 1 if won else 0))
        self._since_refit += 1

    def maybe_refit(self, artifact: ModelArtifact, band: str) -> Optional[ModelArtifact]:
        if self._since_refit < self.spec.recal_min_new or len(self._buffer) < 100:
            return None
        from sklearn.isotonic import IsotonicRegression

        cal = artifact.calibrator
        x_old = np.asarray(getattr(cal, "X_", np.array([])), dtype=float)
        y_old = np.asarray(getattr(cal, "y_", np.array([])), dtype=float)
        new_raw = np.array([p for p, _ in self._buffer], dtype=float)
        new_y = np.array([y for _, y in self._buffer], dtype=float)

        x = np.concatenate([x_old, new_raw])
        y = np.concatenate([y_old, new_y.astype(float)])
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(x, y)

        import copy
        refreshed = copy.copy(artifact)
        refreshed.calibrator = iso
        refreshed.meta = dict(artifact.meta)
        refreshed.meta.setdefault("recalibrations", 0)
        refreshed.meta["recalibrations"] += 1
        refreshed.meta["last_recalibration_ts"] = int(time.time())
        self._since_refit = 0
        return refreshed


class RiskGuard:
    """Daily loss cap, drawdown halt, kill switch. Stop-only."""

    def __init__(self, spec: AutoCorrectSpec, base_dir: Path) -> None:
        self.spec = spec
        self.base_dir = Path(base_dir)
        self.daily_pnl: Dict[str, float] = {}
        self.peak_realized = 0.0
        self.total_realized = 0.0

    def _kill_file(self) -> Path:
        p = Path(self.spec.kill_switch_path)
        return p if p.is_absolute() else self.base_dir / p

    def block_reason(self, ts_ms: int) -> Optional[str]:
        if self._kill_file().exists():
            return "kill_switch"
        day = time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000.0))
        if self.daily_pnl.get(day, 0.0) <= -self.spec.daily_loss_cap_sol:
            return "daily_loss_cap"
        if self.peak_realized > 0 and \
                self.total_realized < self.peak_realized * (1.0 - self.spec.drawdown_halt_pct):
            return "drawdown_halt"
        return None

    def on_closed(self, ts_ms: int, pnl_sol: float) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000.0))
        self.daily_pnl[day] = self.daily_pnl.get(day, 0.0) + pnl_sol
        self.total_realized += pnl_sol
        self.peak_realized = max(self.peak_realized, self.total_realized)


class AutoTuner:
    """Bounded, hysteresis-guarded parameter adjustment. Never past the
    spec's bounds; never more often than once per `tune_window` trades."""

    def __init__(self, spec: AutoCorrectSpec) -> None:
        self.spec = spec
        self.recent: Deque[Tuple[int, float]] = deque(maxlen=spec.tune_window)
        self.n_closed = 0
        self.n_changes = 0
        self._spec_version = 0

    def on_closed(self, ts_ms: int, pnl_sol: float, band_breakeven: float,
                  spec: BotSpec) -> Tuple[Optional[BotSpec], List[TuneChange]]:
        self.recent.append((ts_ms, pnl_sol))
        self.n_closed += 1
        if self.n_closed < self.spec.tune_min_trades or self.n_closed % self.spec.tune_window != 0:
            return None, []
        pnls = [p for _, p in self.recent]
        win_rate = float(np.mean([p > 0 for p in pnls]))
        changes: List[TuneChange] = []
        new_spec = spec
        margin = spec.entry.probability_margin
        kelly = spec.entry.kelly_fraction

        if win_rate < band_breakeven - self.spec.tune_hysteresis:
            lo, hi = spec.entry.margin_bounds
            if margin + 0.01 <= hi:
                changes.append(self._change("probability_margin", margin,
                                            margin + 0.01,
                                            f"recent win rate {win_rate:.2f} below "
                                            f"breakeven {band_breakeven:.2f}"))
                margin += 0.01
            lo, hi = spec.entry.kelly_bounds
            if kelly * 0.8 >= lo:
                changes.append(self._change("kelly_fraction", kelly, kelly * 0.8,
                                            f"recent win rate {win_rate:.2f} below "
                                            f"breakeven {band_breakeven:.2f}"))
                kelly *= 0.8
        elif win_rate > band_breakeven + self.spec.tune_hysteresis and margin > 0.02:
            changes.append(self._change("probability_margin", margin, margin - 0.01,
                                        f"recent win rate {win_rate:.2f} above "
                                        f"breakeven {band_breakeven:.2f}"))
            margin -= 0.01
            lo, hi = spec.entry.kelly_bounds
            if kelly * 1.1 <= hi:
                changes.append(self._change("kelly_fraction", kelly, kelly * 1.1,
                                            f"recent win rate {win_rate:.2f} above "
                                            f"breakeven {band_breakeven:.2f}"))
                kelly *= 1.1

        for c in changes:
            new_spec = new_spec.apply_tune(c.param, c.new) or new_spec
        if changes:
            self.n_changes += 1
            self._spec_version += 1
            for c in changes:
                c.spec_version = self._spec_version
        return new_spec, changes

    def _change(self, param: str, old: float, new: float, reason: str) -> TuneChange:
        return TuneChange(ts=int(time.time()), param=param, old=old,
                          new=round(new, 6), reason=reason, spec_version=0)


class Autopilot:
    """The glue: one object the engine talks to for all four mechanisms.

    Split policy: the RiskGuard (daily loss cap, drawdown halt, kill
    switch) is ALWAYS on -- it is safety, not opinion. Drift monitoring,
    rolling recalibration and the auto-tuner are strictly opt-in via
    `spec.autocorrect.enabled`."""

    def __init__(self, spec: BotSpec, models: Dict[str, ModelArtifact],
                 base_dir: Path) -> None:
        self.spec = spec
        self.ac = spec.autocorrect
        self.models = models
        self.enabled = spec.autocorrect.enabled
        self.base_dir = Path(base_dir)
        self.drift = DriftMonitor(next(iter(models.values()), None), self.ac) \
            if models else DriftMonitor(None, self.ac)
        self.recal = Recalibrator(self.ac)
        self.tuner = AutoTuner(self.ac)
        self.guard = RiskGuard(self.ac, self.base_dir)
        self._pending: Dict[str, Dict] = {}
        self._log_rows: List[Dict] = []

    # -- engine hooks -------------------------------------------------------

    def observe_features(self, features: Dict[str, Optional[float]]) -> None:
        if not self.enabled:
            return
        self.drift.observe(features)

    def entry_block(self, ts_ms: int) -> Optional[str]:
        # Safety is always on; drift/tuner/recalibration are opt-in.
        return self.guard.block_reason(ts_ms)

    def drift_action(self) -> str:
        if not self.enabled:
            return "normal"
        return self.drift.status()["action"]

    def register_entry(self, mint: str, p_raw: Optional[float],
                       p_calibrated: Optional[float], band: str) -> None:
        self._pending[mint] = {"p_raw": p_raw, "p_calibrated": p_calibrated,
                               "band": band}

    def on_closed_trade(self, mint: str, ts_ms: int, pnl_sol: float
                        ) -> Tuple[Optional[BotSpec], List[TuneChange]]:
        self.guard.on_closed(ts_ms, pnl_sol)   # always: caps must track the book
        if not self.enabled:
            self._pending.pop(mint, None)
            return None, []
        meta = self._pending.pop(mint, {})
        p_raw = meta.get("p_raw")
        if p_raw is not None:
            self.recal.add(float(p_raw), pnl_sol > 0)
            band = meta.get("band", "")
            art = self.models.get(band)
            if art is not None:
                refreshed = self.recal.maybe_refit(art, band)
                if refreshed is not None:
                    self.models[band] = refreshed
                    self._log({
                        "kind": "recalibration", "band": band, "param": "calibrator",
                        "old": "-",
                        "new": f"refit#{refreshed.meta.get('recalibrations')}",
                        "reason": "rolling outcomes folded into the isotonic",
                    })
        new_spec, changes = self.tuner.on_closed(
            ts_ms, pnl_sol, self._breakeven_for(meta.get("band", "")), self.spec)
        if new_spec is not None and new_spec is not self.spec:
            self.spec = new_spec
            for c in changes:
                self._log({"kind": "autotune", "band": meta.get("band", ""),
                           "param": c.param, "old": c.old, "new": c.new,
                           "reason": c.reason})
        return new_spec, changes

    # -- internals ----------------------------------------------------------

    def _breakeven_for(self, band: str) -> float:
        try:
            return self.spec.band_by_name(band).breakeven_win_rate
        except KeyError:
            return 0.5

    def _log(self, row: Dict) -> None:
        row = dict(row)
        row["ts_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._log_rows.append(row)

    def save_log(self) -> None:
        if not self._log_rows:
            return
        path = Path(self.ac.log_path)
        p = path if path.is_absolute() else self.base_dir / path
        p.parent.mkdir(parents=True, exist_ok=True)
        fields = ["ts_iso", "kind", "band", "param", "old", "new", "reason"]
        new = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if new:
                writer.writeheader()
            for row in self._log_rows:
                writer.writerow({k: row.get(k, "") for k in fields})
        self._log_rows = []
