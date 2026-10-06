"""Exit state machine: the three v3 invariants (trail arms ONLY on the
profit-taking partial; Category A cannot double-sell; same-bar ambiguity
resolves DOWN), the sustained-observation rule, and the clock sweep."""

import sys, os, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape.bot.exits import ExitAction, Position, open_position, step_position
from tape.bot.spec import BandSpec, ExitSpec
from tape.schema import Bar

BAND = BandSpec("B_small", 15.0, 2.0, -0.40, 45 * 60_000, 0.5, 0.3)
EXITS = ExitSpec()


def bar(open_p, high_p, low_p, close_p, ts, feats=None):
    return Bar(mint="M", venue="v", kind="dollar", open_ts_ms=ts,
               close_ts_ms=ts + 1000, open=open_p, high=high_p, low=low_p,
               close=close_p, buy_quote=1.0, sell_quote=1.0, buy_count=1,
               sell_count=1, trade_count=2, attributed_count=2,
               unique_buyers=2, unique_sellers=2, largest_buyer_share=0.5,
               buy_hhi=0.5)


def feats(**over):
    base = {"liq_div_consecutive": 0.0, "liq_drawdown": -0.05}
    base.update(over)
    return base


def pos(entry=1.0):
    return open_position("M", BAND, 0, entry, 1.0, 1.0)


class TestHardStop(unittest.TestCase):
    def test_stop_before_tp_in_same_bar_resolves_down(self):
        p = pos()
        a = step_position(p, bar(1.0, 2.2, 0.55, 0.6, 1000), feats(), 2000,
                          EXITS, BAND)
        self.assertIsNotNone(a)
        self.assertEqual(a.reason, "hard_stop")
        self.assertEqual(a.fraction, 1.0)
        self.assertTrue(p.closed)

    def test_no_second_action_after_close(self):
        p = pos()
        step_position(p, bar(1.0, 1.1, 0.55, 0.6, 1000), feats(), 2000, EXITS, BAND)
        self.assertIsNone(step_position(p, bar(0.6, 0.6, 0.1, 0.1, 2000),
                                        feats(), 3000, EXITS, BAND))


class TestPartialAndTrail(unittest.TestCase):
    def test_partial_arms_trail_and_floors_at_breakeven(self):
        p = pos()
        a = step_position(p, bar(1.0, 2.1, 0.9, 2.0, 1000), feats(), 2000,
                          EXITS, BAND)
        self.assertEqual(a.reason, "take_profit_partial")
        self.assertAlmostEqual(a.fraction, 0.5)
        self.assertTrue(p.partial_done)
        # trail armed at tp*(1-trail) = 2.0*0.7 = 1.4; stop floor = entry
        self.assertAlmostEqual(p.trail_stop, 1.4)
        self.assertAlmostEqual(p.stop_price, 1.0)
        self.assertFalse(p.closed)

    def test_trail_exits_remainder(self):
        p = pos()
        step_position(p, bar(1.0, 2.1, 0.9, 2.0, 1000), feats(), 2000, EXITS, BAND)
        a = step_position(p, bar(2.0, 1.5, 1.3, 1.35, 2000), feats(), 3000,
                          EXITS, BAND)
        self.assertEqual(a.reason, "trail_stop")
        self.assertEqual(a.fraction, 1.0)

    def test_remainder_never_loses_after_partial(self):
        p = pos()
        step_position(p, bar(1.0, 2.1, 0.9, 2.0, 1000), feats(), 2000, EXITS, BAND)
        a = step_position(p, bar(1.6, 1.7, 0.8, 0.85, 2000), feats(), 3000,
                          EXITS, BAND)
        # hard stop is now at entry (1.0), not 0.6 -- a winner never loses
        self.assertEqual(a.reason, "hard_stop")
        self.assertAlmostEqual(a.price, 1.0)


class TestCategoryA(unittest.TestCase):
    def test_single_observation_does_not_exit(self):
        p = pos()
        self.assertIsNone(step_position(p, bar(1.0, 1.1, 0.9, 1.05, 1000),
                                        feats(liq_div_consecutive=1.0), 2000,
                                        EXITS, BAND))
        self.assertFalse(p.closed)

    def test_sustained_divergence_exits(self):
        p = pos()
        step_position(p, bar(1.0, 1.1, 0.9, 1.05, 1000),
                      feats(liq_div_consecutive=1.0), 2000, EXITS, BAND)
        a = step_position(p, bar(1.05, 1.1, 1.0, 1.08, 2000),
                          feats(liq_div_consecutive=2.0), 3000, EXITS, BAND)
        self.assertEqual(a.reason, "category_a_divergence")
        self.assertTrue(p.closed)

    def test_liquidity_collapse_exits(self):
        p = pos()
        a = step_position(p, bar(1.0, 1.05, 0.95, 1.0, 1000),
                          feats(liq_drawdown=-0.4), 2000, EXITS, BAND)
        self.assertEqual(a.reason, "category_a_liquidity_collapse")


class TestTimeStop(unittest.TestCase):
    def test_clock_not_tape(self):
        p = pos()
        p.horizon_ts_ms = 5000
        self.assertIsNone(step_position(p, bar(1.0, 1.1, 0.9, 1.0, 1000),
                                        feats(), 4000, EXITS, BAND))
        a = step_position(p, bar(1.0, 1.0, 1.0, 1.0, 6000), feats(), 6000,
                          EXITS, BAND)
        self.assertEqual(a.reason, "time_stop")


if __name__ == "__main__":
    unittest.main()
