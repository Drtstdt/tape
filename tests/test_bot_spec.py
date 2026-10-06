"""Spec: bands, payoff math, bounded tuning, YAML loading with unknown-key
errors (a typo'd override is an error, never a silent no-op)."""

import sys, os, tempfile, unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape.bot.spec import (AutoCorrectSpec, BandSpec, BotSpec, EntrySpec,
                           ExitSpec, load_spec)


class TestBandSpec(unittest.TestCase):
    def test_payoff_and_breakeven(self):
        b = BandSpec("B", 10, 2.0, -0.40, 1000, 0.5, 0.3)
        self.assertAlmostEqual(b.payoff_ratio, 1.0 / 0.4)
        self.assertAlmostEqual(b.breakeven_win_rate, 0.4 / 1.4)

    def test_validation(self):
        with self.assertRaises(ValueError):
            BandSpec("B", 10, 0.5, -0.4, 1000, 0.5, 0.3)
        with self.assertRaises(ValueError):
            BandSpec("B", 10, 2.0, -1.4, 1000, 0.5, 0.3)
        with self.assertRaises(ValueError):
            BandSpec("B", 10, 2.0, -0.4, 1000, 1.5, 0.3)


class TestBotSpec(unittest.TestCase):
    def test_band_selection_highest_floor(self):
        spec = BotSpec()
        self.assertIsNone(spec.band_for_liquidity(10.0))
        self.assertEqual(spec.band_for_liquidity(20.0).name, "B_small")
        self.assertEqual(spec.band_for_liquidity(100.0).name, "B_mid")
        self.assertEqual(spec.band_for_liquidity(1000.0).name, "B_deep")

    def test_apply_tune_bounded_and_immutable(self):
        spec = BotSpec()
        orig = spec.entry.probability_margin
        out = spec.apply_tune("probability_margin", 0.08)
        self.assertIsNotNone(out)
        self.assertEqual(out.entry.probability_margin, 0.08)
        self.assertEqual(spec.entry.probability_margin, orig)  # untouched
        self.assertIsNone(spec.apply_tune("probability_margin", 0.99))
        self.assertIsNone(spec.apply_tune("kelly_fraction", 0.9))


class TestLoadSpec(unittest.TestCase):
    def test_roundtrip_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "spec.yaml"
            p.write_text("""
name: x
version: 2
entry:
  min_age_ms: 1000
  min_bars: 2
  gate_netflow_k: 5
bands:
  - name: B_small
    min_liquidity_sol: 15.0
    upper_multiple: 2.0
    lower_pct: -0.40
    horizon_ms: 2700000
    tp_fraction: 0.5
    trail_pct: 0.30
    cooldown_ms: 60000
typo_key: 1
""")
            with self.assertRaises(ValueError):
                load_spec(p)
            p.write_text(p.read_text().replace("typo_key: 1", ""))
            spec = load_spec(p)
            self.assertEqual(spec.version, 2)
            self.assertEqual(spec.entry.min_age_ms, 1000)
            self.assertEqual(spec.bands[0].name, "B_small")

    def test_default_spec_loads_from_repo(self):
        cfg = Path(__file__).resolve().parents[1] / "config" / "bot.yaml"
        spec = load_spec(cfg)
        self.assertEqual(len(spec.bands), 3)
        self.assertEqual(spec.entry.kelly_fraction, 0.10)


if __name__ == "__main__":
    unittest.main()
