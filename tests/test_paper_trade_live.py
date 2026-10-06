"""Tests for scripts/paper_trade_live.py (D84, docs/DECISIONS.md) --
Phase B of docs/PAPER_TRADING_PLAN.md.

Same importlib-by-path loading as tests/test_paper_trade_replay.py.
`HeliusSource` is never touched -- these tests pass a tiny fake object
exposing only the one method `poll_mint` actually calls
(`.historical(mint, since_ms, until_ms) -> Iterable[CanonicalSwap]`), so no
network, API key, or real adapter is needed to test the decision/resolution
logic itself.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tape.labels import BarrierConfig  # noqa: E402
from tape.online_policy import OnlinePolicy, PaperRails  # noqa: E402
from tape.costs import CostModel  # noqa: E402
from tape.schema import CanonicalSwap  # noqa: E402
from tape.features import TokenState  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "paper_trade_live", ROOT / "scripts" / "paper_trade_live.py")
ptl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ptl  # @dataclass needs cls.__module__ resolvable via sys.modules
_spec.loader.exec_module(ptl)
ptl.MIN_SWAPS_PER_TOKEN = 1  # tiny hand-built fixtures, not the real 50-swap corpus floor


def swap(ts, price, qty, side="buy", wallet="w1", res=100.0):
    return CanonicalSwap(mint="M", venue="v", pool="p", ts_ms=ts, slot=ts,
                         sig=f"s{ts}{side}{wallet}", side=side,
                         base_amount=qty / price, quote_amount=qty, quote_mint="SOL",
                         price=price, wallet=wallet, quote_reserve_after=res,
                         base_reserve_after=res / price)


class FakeSource:
    """Hands back whatever swaps fall in [since_ms, until_ms), exactly once
    each (mirrors HeliusSource.historical's `since_ms` being an exclusive,
    advancing watermark, per poll_mint's `last_fetched_ts_ms = max(...) + 1`)."""

    def __init__(self, swaps):
        self.swaps = swaps

    def historical(self, mint, since_ms, until_ms):
        return [s for s in self.swaps if since_ms <= s.ts_ms <= until_ms]


EARLY = [
    swap(0, 1.0, 5.0, wallet="a"), swap(1000, 1.0, 5.0, wallet="b"),
    swap(2000, 1.0, 5.0, wallet="a"), swap(3000, 1.0, 5.0, wallet="c"),
    swap(4000, 1.0, 5.0, wallet="d"), swap(6000, 1.0, 5.0, wallet="e"),
]
CFG = BarrierConfig(upper_multiple=1.6, lower_pct=-0.30, horizon_ms=10_000)


def make_policies():
    feature_names = list(TokenState("probe").features().keys())
    online = OnlinePolicy(feature_names, upper_multiple=1.6, lower_pct=-0.30,
                          epsilon_start=1.0, epsilon_floor=1.0, seed=0)
    random_baseline = OnlinePolicy(feature_names, upper_multiple=1.6, lower_pct=-0.30,
                                   epsilon_start=1.0, epsilon_floor=1.0, seed=1)
    return online, random_baseline


class TestPollMintDecisionTrigger(unittest.TestCase):
    def test_decision_fires_exactly_once_at_the_first_rails_passing_bar(self):
        online, random_baseline = make_policies()
        tm = ptl.TrackedMint(mint="M", created_ts_ms=0, last_fetched_ts_ms=0)
        source = FakeSource(EARLY)
        rails = PaperRails()

        ptl.poll_mint(source, tm, now_ms=6000, bar_fraction=0.1, rails=rails,
                      online=online, random_baseline=random_baseline)

        self.assertTrue(tm.decided)
        self.assertEqual(tm.decision_index, 2)  # bar0/bar1 too young, bar2 first to clear
        self.assertIsNotNone(tm.online_decision)
        self.assertIsNotNone(tm.random_decision)
        self.assertEqual(online.n_decisions, 1)

        # A second poll with no new swaps must NOT re-decide.
        ptl.poll_mint(source, tm, now_ms=7000, bar_fraction=0.1, rails=rails,
                      online=online, random_baseline=random_baseline)
        self.assertEqual(online.n_decisions, 1)

    def test_no_decision_while_rails_have_not_cleared_yet(self):
        online, random_baseline = make_policies()
        tm = ptl.TrackedMint(mint="M", created_ts_ms=0, last_fetched_ts_ms=0)
        source = FakeSource(EARLY[:2])  # only bar0's swaps -- too young
        ptl.poll_mint(source, tm, now_ms=1000, bar_fraction=0.1, rails=PaperRails(),
                      online=online, random_baseline=random_baseline)
        self.assertFalse(tm.decided)
        self.assertEqual(online.n_decisions, 0)

    def test_sets_error_and_stops_polling_on_a_real_runtime_error(self):
        online, random_baseline = make_policies()

        class BrokenSource:
            def historical(self, mint, since_ms, until_ms):
                raise RuntimeError("not a Token mint")

        tm = ptl.TrackedMint(mint="M", created_ts_ms=0, last_fetched_ts_ms=0)
        ptl.poll_mint(BrokenSource(), tm, now_ms=1000, bar_fraction=0.1, rails=PaperRails(),
                      online=online, random_baseline=random_baseline)
        self.assertEqual(tm.error, "not a Token mint")


class TestTryResolve(unittest.TestCase):
    def _decided_tm(self, tail_price=None):
        online, random_baseline = make_policies()
        tm = ptl.TrackedMint(mint="M", created_ts_ms=0, last_fetched_ts_ms=0)
        swaps = EARLY if tail_price is None else EARLY + [
            swap(7000, tail_price, 5.0, wallet="f"), swap(8000, tail_price, 5.0, wallet="g")]
        source = FakeSource(swaps)
        ptl.poll_mint(source, tm, now_ms=8000, bar_fraction=0.1, rails=PaperRails(),
                      online=online, random_baseline=random_baseline)
        return tm, online, random_baseline

    def test_returns_none_while_truncated(self):
        tm, online, random_baseline = self._decided_tm()  # no tail -- tape ends right at the decision bar
        rows = ptl.try_resolve(tm, CFG, online, random_baseline, CostModel(), 1.0, mode="live")
        self.assertIsNone(rows)
        self.assertFalse(tm.resolved)
        self.assertEqual(online.n_updates, 0)

    def test_resolves_up_and_updates_both_policies(self):
        tm, online, random_baseline = self._decided_tm(tail_price=1.6)
        rows = ptl.try_resolve(tm, CFG, online, random_baseline, CostModel(), 1.0, mode="live")
        self.assertIsNotNone(rows)
        self.assertTrue(tm.resolved)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["outcome"], "UP")
            self.assertEqual(row["mode"], "live")
            if row["action"] == "enter":
                self.assertAlmostEqual(row["raw_pnl_pct"], 0.6)
        self.assertEqual(online.n_updates, 1)
        self.assertEqual(random_baseline.n_updates, 1)

        # Calling try_resolve again on an already-resolved mint is a no-op.
        again = ptl.try_resolve(tm, CFG, online, random_baseline, CostModel(), 1.0, mode="live")
        self.assertIsNone(again)
        self.assertEqual(online.n_updates, 1)  # not double-counted

    def test_ledger_rows_match_the_shared_ledger_schema(self):
        from paper_trade_replay import LEDGER_COLUMNS
        tm, online, random_baseline = self._decided_tm(tail_price=0.65)
        rows = ptl.try_resolve(tm, CFG, online, random_baseline, CostModel(), 1.0, mode="live")
        for row in rows:
            self.assertEqual(set(row.keys()), set(LEDGER_COLUMNS))


if __name__ == "__main__":
    unittest.main()
