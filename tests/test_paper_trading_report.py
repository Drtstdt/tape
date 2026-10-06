"""Tests for scripts/paper_trading_report.py's D90 (docs/DECISIONS.md)
addition -- the entered-vs-abstained belief-discrimination check.

Loaded by file path with importlib, same pattern as
tests/test_paper_trade_replay.py (scripts/ is not an importable package).
The rest of this script (`_pnl_stats`, the verdict logic) was previously
only smoke-tested by hand against a synthetic CSV (D84) -- that gap isn't
closed here, this file is scoped to the new D90 functions specifically,
since those are the ones making a quantitative claim that needs a
known-by-construction check.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "paper_trading_report", ROOT / "scripts" / "paper_trading_report.py")
ptrep = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ptrep
_spec.loader.exec_module(ptrep)

ptrep._FIXED_PAYOFF["upper_multiple"] = 1.6
ptrep._FIXED_PAYOFF["lower_pct"] = -0.30


class TestFixedPayoff(unittest.TestCase):
    def test_up_down_timeout(self):
        self.assertAlmostEqual(ptrep._fixed_payoff("UP"), 0.6)
        self.assertAlmostEqual(ptrep._fixed_payoff("DOWN"), -0.30)
        self.assertAlmostEqual(ptrep._fixed_payoff("TIMEOUT"), 0.0)


def _rows(mint_outcomes_actions):
    """mint_outcomes_actions: list of (mint, outcome, action) tuples."""
    return pd.DataFrame([{"mint": m, "outcome": o, "action": a}
                         for m, o, a in mint_outcomes_actions])


class TestDiscriminationGap(unittest.TestCase):
    def test_perfect_discrimination_recovers_the_full_barrier_spread(self):
        """Every ENTERED decision is a future UP, every ABSTAINED decision is
        a future DOWN -- by construction the gap must be exactly
        (upper-1) - lower = 0.6 - (-0.3) = 0.9, and a generous CI around it
        should still exclude 0 with enough distinct mints."""
        rows = []
        for i in range(30):
            rows.append((f"up{i}", "UP", "enter"))
            rows.append((f"down{i}", "DOWN", "abstain"))
        df = _rows(rows)
        out = ptrep._discrimination_gap(df, "test")
        self.assertAlmostEqual(out["gap"], 0.9, places=6)
        self.assertEqual(out["n_entered"], 30)
        self.assertEqual(out["n_abstained"], 30)
        self.assertEqual(out["n_mints"], 60)
        self.assertGreater(out["ci_lo"], 0)

    def test_no_discrimination_gives_a_gap_near_zero(self):
        """Action is assigned independently of outcome (alternating UP/DOWN
        regardless of enter/abstain) -- the gap should sit near 0 and the CI
        should include 0, the same placebo behavior `random`'s own rows show
        in the real report run (D90)."""
        rows = []
        for i in range(60):
            outcome = "UP" if i % 2 == 0 else "DOWN"
            action = "enter" if i % 3 == 0 else "abstain"
            rows.append((f"m{i}", outcome, action))
        df = _rows(rows)
        out = ptrep._discrimination_gap(df, "test", seed=1)
        self.assertLess(abs(out["gap"]), 0.3)
        self.assertLessEqual(out["ci_lo"], 0)
        self.assertGreaterEqual(out["ci_hi"], 0)

    def test_missing_entered_or_abstained_group_returns_nan_not_a_crash(self):
        df = _rows([("m1", "UP", "enter"), ("m2", "UP", "enter")])
        out = ptrep._discrimination_gap(df, "test")
        self.assertEqual(out["n_abstained"], 0)
        self.assertNotEqual(out["gap"], out["gap"])  # NaN != NaN


if __name__ == "__main__":
    unittest.main()
