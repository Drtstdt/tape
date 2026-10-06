"""Tests for scripts/paper_trade_replay.py (D84, docs/DECISIONS.md) --
Phase A of docs/PAPER_TRADING_PLAN.md.

Loaded by file path with importlib, same pattern as
tests/test_information_audit.py (scripts/ is not an importable package).
`scripts/` is also added to sys.path so paper_trade_replay.py's own
`from information_audit import (...)` (the same cross-script reuse
convention scenario_backtest.py/token_dna_report.py already use) resolves
exactly as it does under a real `python scripts/paper_trade_replay.py`
invocation, where the running script's own directory is sys.path[0]
automatically.
"""

import csv
import importlib.util
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tape.labels import BarrierConfig, UP, DOWN  # noqa: E402
from tape.online_policy import OnlinePolicy, PaperRails  # noqa: E402
from tape.costs import CostModel  # noqa: E402
from tape.schema import CanonicalSwap  # noqa: E402
from tape.features import TokenState  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "paper_trade_replay", ROOT / "scripts" / "paper_trade_replay.py")
ptr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ptr)

# replay_one() re-checks MIN_SWAPS_PER_TOKEN itself (belt-and-suspenders,
# same pattern as information_audit.py::build_one()), imported from
# information_audit.py where it's a real, large corpus threshold (50). Real
# callers pre-filter the universe to that same bar via chronological_universe,
# so it's never actually binding there -- but these tests build tiny, hand
# constructed tapes (a handful of swaps) specifically to control bar
# boundaries precisely, so the real threshold is lowered here to the size of
# these fixtures. This changes no logic under test, only the volume floor.
ptr.MIN_SWAPS_PER_TOKEN = 4


def swap(ts, price, qty, side="buy", wallet="w1", res=100.0):
    return CanonicalSwap(mint="M", venue="v", pool="p", ts_ms=ts, slot=ts,
                         sig=f"s{ts}{side}{wallet}", side=side,
                         base_amount=qty / price, quote_amount=qty, quote_mint="SOL",
                         price=price, wallet=wallet, quote_reserve_after=res,
                         base_reserve_after=res / price)


# Three bars, each closing on 10.0 quote of dollar-bar threshold, each with
# 2 distinct buyers -- bar0/bar1 are too YOUNG (age_ms < 5000) for
# PaperRails even though breadth already clears it; bar2 (close_ts=6000,
# age_ms=6000) is the first bar where every PaperRails default clears.
_EARLY_BARS = [
    swap(0, 1.0, 5.0, wallet="a"), swap(1000, 1.0, 5.0, wallet="b"),      # bar0 closes @1000
    swap(2000, 1.0, 5.0, wallet="a"), swap(3000, 1.0, 5.0, wallet="c"),   # bar1 closes @3000
    swap(4000, 1.0, 5.0, wallet="d"), swap(6000, 1.0, 5.0, wallet="e"),   # bar2 closes @6000 -- DECISION BAR
]

CFG = BarrierConfig(upper_multiple=1.6, lower_pct=-0.30, horizon_ms=10_000)


def make_policies(**overrides):
    feature_names = list(TokenState("probe").features().keys())
    kwargs = dict(upper_multiple=1.6, lower_pct=-0.30, seed=0)
    kwargs.update(overrides)
    online = OnlinePolicy(feature_names, **kwargs)
    random_baseline = OnlinePolicy(feature_names, upper_multiple=1.6, lower_pct=-0.30,
                                   epsilon_start=1.0, epsilon_floor=1.0, seed=1)
    return online, random_baseline


class TestFirstDecisionBarIndex(unittest.TestCase):
    """Directly tests the bar-selection logic, decoupled from everything
    that happens once a decision bar is found -- resolves the ambiguity a
    bare `replay_one(...) == []` can't: "rails never passed" and "decision
    bar found, but the tape truncated after it" both return `[]` from
    `replay_one`, for different reasons that matter to distinguish here."""

    def test_finds_the_third_bar_in_the_fixture_tape(self):
        # Rebuild the same feats-by-bar sequence _EARLY_BARS produces, using
        # the exact same BarBuilder/TokenState path replay_one uses.
        from tape.bars import BarBuilder, band_bar_threshold
        depth = next((s.quote_reserve_after for s in _EARLY_BARS if s.quote_reserve_after), None) or 1.0
        bb = BarBuilder("dollar", threshold=band_bar_threshold(depth, 0.1))
        st = TokenState("M", created_ts_ms=_EARLY_BARS[0].ts_ms)
        feats_by_bar = []
        for s in _EARLY_BARS:
            bar = bb.push(s)
            if bar is None:
                continue
            st.update(bar)
            feats_by_bar.append(dict(st.features()))
        self.assertEqual(len(feats_by_bar), 3)  # bar0, bar1, bar2
        idx = ptr.first_decision_bar_index(feats_by_bar, PaperRails())
        self.assertEqual(idx, 2)  # bar0/bar1 are too young; bar2 is the first to clear every rail

    def test_returns_none_when_no_bar_ever_clears_rails(self):
        idx = ptr.first_decision_bar_index(
            [{"n_bars": 1.0, "age_ms": 100.0, "liquidity": 10.0,
              "unique_buyers_10": 5.0, "largest_buyer_share_10": 0.1}],
            PaperRails())
        self.assertIsNone(idx)


class TestDecisionBarSelection(unittest.TestCase):
    def test_no_decision_when_tape_never_clears_rails(self):
        """Only the two too-young bars -- rails never pass, no decision made,
        no PnL row, and (full-feedback learning notwithstanding) no update
        either, since there is no resolved decision point at all."""
        online, random_baseline = make_policies()
        rows = ptr.replay_one("M", _EARLY_BARS[:4], CFG, bar_fraction=0.1,
                              rails=PaperRails(), online=online,
                              random_baseline=random_baseline, cost=CostModel(),
                              nominal_size=1.0, mode="replay")
        self.assertEqual(rows, [])
        self.assertEqual(online.n_decisions, 0)
        self.assertEqual(online.n_updates, 0)

    def test_truncated_tape_after_decision_bar_is_excluded(self):
        """The decision bar (bar2) is reached, but the tape ends there --
        triple_barrier can't resolve within the horizon, so (matching
        information_audit.py's own truncated-label handling) this contributes
        nothing, not a fabricated TIMEOUT."""
        online, random_baseline = make_policies()
        rows = ptr.replay_one("M", _EARLY_BARS, CFG, bar_fraction=0.1,
                              rails=PaperRails(), online=online,
                              random_baseline=random_baseline, cost=CostModel(),
                              nominal_size=1.0, mode="replay")
        self.assertEqual(rows, [])
        # No resolved label -> no update, even though a decision bar existed.
        self.assertEqual(online.n_updates, 0)


class TestMintsAlreadyDecided(unittest.TestCase):
    """D89 (docs/DECISIONS.md): re-running replay against a Store that grew
    since the last run must not re-decide on the same mint twice -- would
    duplicate ledger rows and break the one-decision-per-token design."""

    def _write_ledger(self, tmp_path, rows):
        path = tmp_path / "ledger.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["mode", "policy", "mint"])
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return path

    def test_returns_empty_set_when_ledger_does_not_exist(self):
        missing = Path(tempfile.mkdtemp()) / "nope.csv"
        self.assertEqual(ptr.mints_already_decided(missing), set())

    def test_collects_mints_only_for_the_matching_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_ledger(Path(d), [
                {"mode": "replay", "policy": "online", "mint": "A"},
                {"mode": "replay", "policy": "random", "mint": "A"},
                {"mode": "replay", "policy": "online", "mint": "B"},
                {"mode": "live", "policy": "online", "mint": "C"},
            ])
            self.assertEqual(ptr.mints_already_decided(path, mode="replay"), {"A", "B"})
            self.assertEqual(ptr.mints_already_decided(path, mode="live"), {"C"})


class TestRejectionReasonTally(unittest.TestCase):
    """The `reasons` Counter added after the real 0/65-decisions replay run
    (docs/DECISIONS.md D85) -- confirms each early-return path in
    `replay_one` tallies exactly the reason it claims to, so the breakdown
    `scripts/paper_trade_replay.py::main()` prints is trustworthy."""

    def test_too_few_swaps_is_tallied(self):
        online, random_baseline = make_policies()
        reasons = Counter()
        rows = ptr.replay_one("M", _EARLY_BARS[:2], CFG, bar_fraction=0.1,
                              rails=PaperRails(), online=online,
                              random_baseline=random_baseline, cost=CostModel(),
                              nominal_size=1.0, mode="replay", reasons=reasons)
        self.assertEqual(rows, [])
        self.assertEqual(reasons, Counter({"too_few_swaps_after_sanity_filter": 1}))

    def test_rail_rejection_is_tallied_by_reason_code(self):
        """Only the two too-young bars -- PaperRails' min_age_ms check fires
        on the LAST bar (its best shot), so that's the reason tallied, not
        whatever failed on bar0."""
        online, random_baseline = make_policies()
        reasons = Counter()
        rows = ptr.replay_one("M", _EARLY_BARS[:4], CFG, bar_fraction=0.1,
                              rails=PaperRails(), online=online,
                              random_baseline=random_baseline, cost=CostModel(),
                              nominal_size=1.0, mode="replay", reasons=reasons)
        self.assertEqual(rows, [])
        self.assertEqual(len(reasons), 1)
        (reason,) = reasons.keys()
        self.assertTrue(reason.startswith("rail:"))
        self.assertEqual(reasons[reason], 1)

    def test_truncated_after_decision_bar_is_tallied_separately_from_rail_rejection(self):
        online, random_baseline = make_policies()
        reasons = Counter()
        rows = ptr.replay_one("M", _EARLY_BARS, CFG, bar_fraction=0.1,
                              rails=PaperRails(), online=online,
                              random_baseline=random_baseline, cost=CostModel(),
                              nominal_size=1.0, mode="replay", reasons=reasons)
        self.assertEqual(rows, [])
        self.assertEqual(reasons, Counter({"decision_found_but_tape_truncated": 1}))

    def test_resolved_decision_tallies_nothing(self):
        online, random_baseline = make_policies(epsilon_start=1.0, epsilon_floor=1.0)
        reasons = Counter()
        rows = ptr.replay_one("M", _EARLY_BARS + [swap(7000, 1.6, 5.0, wallet="f"),
                                                    swap(8000, 1.6, 5.0, wallet="g")],
                              CFG, bar_fraction=0.1, rails=PaperRails(),
                              online=online, random_baseline=random_baseline,
                              cost=CostModel(), nominal_size=1.0, mode="replay",
                              reasons=reasons)
        self.assertEqual(len(rows), 2)
        self.assertEqual(reasons, Counter())

    def test_reasons_defaults_to_none_and_is_a_silent_no_op(self):
        """Every existing call site (and every test above this class) calls
        replay_one() without `reasons` -- confirms that stays a true no-op,
        not just untested."""
        online, random_baseline = make_policies()
        rows = ptr.replay_one("M", _EARLY_BARS[:2], CFG, bar_fraction=0.1,
                              rails=PaperRails(), online=online,
                              random_baseline=random_baseline, cost=CostModel(),
                              nominal_size=1.0, mode="replay")
        self.assertEqual(rows, [])


class TestResolvedOutcomes(unittest.TestCase):
    def _bars_with_tail(self, tail_price):
        tail = [swap(7000, tail_price, 5.0, wallet="f"),
                swap(8000, tail_price, 5.0, wallet="g")]
        return _EARLY_BARS + tail

    def test_up_outcome_produces_positive_pnl_for_an_entering_policy(self):
        online, random_baseline = make_policies(epsilon_start=1.0, epsilon_floor=1.0)  # force enter/abstain 50/50, deterministic via seed
        bars = self._bars_with_tail(1.6)  # hits the +60% upper barrier exactly
        rows = ptr.replay_one("M", bars, CFG, bar_fraction=0.1, rails=PaperRails(),
                              online=online, random_baseline=random_baseline,
                              cost=CostModel(), nominal_size=1.0, mode="replay")
        self.assertEqual(len(rows), 2)  # one row per policy
        for row in rows:
            self.assertEqual(row["outcome"], "UP")
            self.assertFalse(row["truncated"])
            if row["action"] == "enter":
                self.assertAlmostEqual(row["raw_pnl_pct"], 0.6)
                self.assertLess(row["cost_adjusted_pnl_pct"], row["raw_pnl_pct"])
        # Full feedback: BOTH policies learn from this resolved outcome,
        # whether or not they personally entered (the random baseline's
        # learned weights are simply never consulted, since its epsilon is
        # pinned at 1.0 -- see tape/online_policy.py's module docstring).
        self.assertEqual(online.n_updates, 1)
        self.assertEqual(random_baseline.n_updates, 1)

    def test_down_outcome_produces_the_fixed_lower_barrier_pnl(self):
        online, random_baseline = make_policies(epsilon_start=1.0, epsilon_floor=1.0)
        bars = self._bars_with_tail(0.65)  # below the -30% lower barrier
        rows = ptr.replay_one("M", bars, CFG, bar_fraction=0.1, rails=PaperRails(),
                              online=online, random_baseline=random_baseline,
                              cost=CostModel(), nominal_size=1.0, mode="replay")
        for row in rows:
            self.assertEqual(row["outcome"], "DOWN")
            if row["action"] == "enter":
                self.assertAlmostEqual(row["raw_pnl_pct"], -0.30)

    def test_abstained_rows_carry_no_pnl(self):
        """An abstained decision is still logged (for an honest sample-size
        count in the report), but with no PnL -- abstaining risks nothing,
        on paper or otherwise."""
        online, random_baseline = make_policies(epsilon_start=0.0, epsilon_floor=0.0)
        online.bias = -50.0  # force a confident abstain regardless of features
        bars = self._bars_with_tail(1.6)
        rows = ptr.replay_one("M", bars, CFG, bar_fraction=0.1, rails=PaperRails(),
                              online=online, random_baseline=random_baseline,
                              cost=CostModel(), nominal_size=1.0, mode="replay")
        online_row = next(r for r in rows if r["policy"] == "online")
        self.assertEqual(online_row["action"], "abstain")
        self.assertIsNone(online_row["raw_pnl_pct"])
        self.assertIsNone(online_row["cost_adjusted_pnl_pct"])
        # Still learned from the true outcome despite abstaining.
        self.assertEqual(online.n_updates, 1)


if __name__ == "__main__":
    unittest.main()
