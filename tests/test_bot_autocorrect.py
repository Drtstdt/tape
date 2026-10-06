"""Auto-correct: drift PSI actions, bounded tuning with hysteresis, rolling
recalibration, and the stop-only risk guard (daily cap, drawdown, kill
switch)."""

import sys, os, tempfile, unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from tape.bot.autocorrect import (AutoTuner, Autopilot, DriftMonitor,
                                  Recalibrator, RiskGuard)
from tape.bot.spec import AutoCorrectSpec, BandSpec, BotSpec, EntrySpec
from tape.model import ModelArtifact


class FakeArtifact:
    train_quantiles = {"x": np.linspace(0.0, 1.0, 11)}


def spec(**auto_over):
    band = BandSpec("B_small", 15.0, 2.0, -0.40, 1000, 0.5, 0.3)
    ac = AutoCorrectSpec(daily_loss_cap_sol=5.0)
    for k, v in auto_over.items():
        setattr(ac, k, v)
    return BotSpec(entry=EntrySpec(), bands=[band], autocorrect=ac)


class TestDriftMonitor(unittest.TestCase):
    def test_psi_actions(self):
        dm = DriftMonitor(FakeArtifact(), AutoCorrectSpec())
        rng = np.random.default_rng(0)
        for _ in range(100):
            dm.observe({"x": float(rng.uniform(0, 1)), "unmeasured": None})
        self.assertEqual(dm.status()["action"], "normal")
        for _ in range(100):
            dm.observe({"x": 15.0})     # a world the model never saw
        self.assertEqual(dm.status()["action"], "halt")

    def test_no_model_no_drift(self):
        dm = DriftMonitor(None, AutoCorrectSpec())
        dm.observe({"x": 1.0})
        self.assertEqual(dm.status(), {"max_psi": 0.0, "action": "normal"})


class TestAutoTuner(unittest.TestCase):
    def test_tightens_on_losing_streak_within_bounds(self):
        s = spec()
        tuner = AutoTuner(s.autocorrect)
        new_spec, changes = None, []
        for i in range(s.autocorrect.tune_min_trades):
            new_spec, changes = tuner.on_closed(
                i * 1000, -0.5, s.bands[0].breakeven_win_rate, s)
            s = new_spec or s
        self.assertTrue(changes)
        names = {c.param for c in changes}
        self.assertIn("probability_margin", names)
        self.assertIn("kelly_fraction", names)
        self.assertGreater(s.entry.probability_margin, 0.04)
        self.assertLess(s.entry.kelly_fraction, 0.10)
        # bounds respected, ever
        lo, hi = s.entry.margin_bounds
        self.assertTrue(lo <= s.entry.probability_margin <= hi)

    def test_never_crosses_bounds(self):
        s = spec()
        tuner = AutoTuner(s.autocorrect)
        for i in range(s.autocorrect.tune_min_trades * 30):
            new_spec, _ = tuner.on_closed(
                i * 1000, -1.0, s.bands[0].breakeven_win_rate, s)
            s = new_spec or s
            lo_m, hi_m = s.entry.margin_bounds
            lo_k, hi_k = s.entry.kelly_bounds
            self.assertTrue(lo_m <= s.entry.probability_margin <= hi_m)
            self.assertTrue(lo_k <= s.entry.kelly_fraction <= hi_k)

    def test_hysteresis_no_flipflop(self):
        s = spec()
        tuner = AutoTuner(s.autocorrect)
        breakeven = s.bands[0].breakeven_win_rate
        for i in range(s.autocorrect.tune_min_trades):
            # win rate 0.30 sits inside breakeven +- hysteresis: no change
            won = (i % 10) < 3
            new_spec, changes = tuner.on_closed(
                i * 1000, 0.5 if won else -0.3, breakeven, s)
            s = new_spec or s
            self.assertFalse(changes)


class TestRecalibrator(unittest.TestCase):
    def _artifact(self):
        from sklearn.isotonic import IsotonicRegression
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(np.linspace(0, 1, 50), np.linspace(0, 1, 50))
        return ModelArtifact(booster=None, feature_names=[], calibrator=iso,
                             conformal_scores={}, train_medians={},
                             train_quantiles={}, meta={})

    def test_refit_only_after_min_new(self):
        rec = Recalibrator(AutoCorrectSpec(recal_min_new=20))
        art = self._artifact()
        for i in range(99):
            rec.add(0.5, True)
            self.assertIsNone(rec.maybe_refit(art, "B_small"))
        rec.add(0.5, False)
        out = rec.maybe_refit(art, "B_small")
        self.assertIsNotNone(out)
        self.assertEqual(out.meta["recalibrations"], 1)
        self.assertIsNot(out.calibrator, art.calibrator)

    def test_refit_stays_monotone_and_in_unit(self):
        rec = Recalibrator(AutoCorrectSpec(recal_min_new=5))
        art = self._artifact()
        for i in range(120):
            rec.add(float(i % 10) / 10.0, i % 2 == 0)
        out = rec.maybe_refit(art, "B_small")
        preds = out.calibrator.predict(np.linspace(0, 1, 21))
        self.assertTrue((np.diff(preds) >= -1e-9).all())
        self.assertTrue((preds >= 0).all() and (preds <= 1).all())


class TestRiskGuard(unittest.TestCase):
    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as td:
            ac = AutoCorrectSpec(kill_switch_path="kill_switch")
            g = RiskGuard(ac, Path(td))
            self.assertIsNone(g.block_reason(0))
            Path(td, "kill_switch").write_text("stop")
            self.assertEqual(g.block_reason(0), "kill_switch")

    def test_daily_loss_cap(self):
        with tempfile.TemporaryDirectory() as td:
            g = RiskGuard(AutoCorrectSpec(daily_loss_cap_sol=5.0), Path(td))
            g.on_closed(1_700_000_000_000, -6.0)     # same UTC day
            self.assertEqual(g.block_reason(1_700_000_000_100), "daily_loss_cap")
            g.on_closed(1_700_000_000_000 + 86_400_000, 0.0)  # next day unaffected
            self.assertIsNone(g.block_reason(1_700_000_000_000 + 86_400_100))

    def test_drawdown_halt(self):
        with tempfile.TemporaryDirectory() as td:
            g = RiskGuard(AutoCorrectSpec(drawdown_halt_pct=0.25), Path(td))
            g.on_closed(1_700_000_000_000, 100.0)
            g.on_closed(1_700_000_100_000, -80.0)     # -80% from peak
            self.assertEqual(g.block_reason(1_700_000_200_000), "drawdown_halt")


class TestAutopilot(unittest.TestCase):
    def test_safety_always_on_tuning_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            s = spec(enabled=False)
            ap = Autopilot(s, {}, Path(td))
            self.assertIsNone(ap.entry_block(0))           # nothing triggered yet
            self.assertEqual(ap.drift_action(), "normal")
            ap.on_closed_trade("M", 1_700_000_000_000, -999.0)
            self.assertEqual(ap.entry_block(1_700_000_000_100), "daily_loss_cap")


if __name__ == "__main__":
    unittest.main()
