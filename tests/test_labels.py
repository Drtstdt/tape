import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from tape.labels import (BarrierConfig, triple_barrier, average_uniqueness,
                         sample_weights, UP, DOWN, TIMEOUT)


CFG = BarrierConfig(upper_multiple=1.5, lower_pct=-0.30, horizon_ms=10_000)


def series(prices, step=1000):
    ts = [i * step for i in range(len(prices))]
    return list(prices), list(prices), list(prices), ts


class TestTripleBarrier(unittest.TestCase):
    def test_up(self):
        h, l, c, t = series([1.0, 1.1, 1.6, 1.2])
        lab = triple_barrier(h, l, c, t, 0, CFG)
        self.assertEqual(lab.outcome, UP)
        self.assertEqual(lab.y, 1)

    def test_down(self):
        h, l, c, t = series([1.0, 0.9, 0.6, 2.0])
        lab = triple_barrier(h, l, c, t, 0, CFG)
        self.assertEqual(lab.outcome, DOWN)

    def test_ambiguity_inside_one_bar_resolves_DOWN(self):
        """Bars carry no intra-bar path. A labeller that breaks ties in its own
        favour produces a backtest nobody can reproduce live."""
        highs = [1.0, 1.6]
        lows = [1.0, 0.6]
        closes = [1.0, 1.0]
        ts = [0, 1000]
        lab = triple_barrier(highs, lows, closes, ts, 0, CFG)
        self.assertEqual(lab.outcome, DOWN)

    def test_timeout_and_truncation_are_distinguishable(self):
        # ran past the deadline without hitting a barrier -> not truncated
        h, l, c, t = series([1.0] * 15)
        self.assertFalse(triple_barrier(h, l, c, t, 0, CFG).truncated)
        # tape ended inside the horizon -> truncated, must be excluded from stats
        h, l, c, t = series([1.0] * 4)
        self.assertTrue(triple_barrier(h, l, c, t, 0, CFG).truncated)

    def test_entry_is_the_close_not_the_open(self):
        """You cannot trade a bar you have not seen close."""
        h, l, c, t = series([1.0, 2.0, 2.05])
        lab = triple_barrier(h, l, c, t, 1, CFG)   # entry = 2.0, target 3.0
        self.assertNotEqual(lab.outcome, UP)

    def test_never_reads_before_i(self):
        h, l, c, t = series([9.0, 1.0, 1.6])
        self.assertEqual(triple_barrier(h, l, c, t, 1, CFG).outcome, UP)


class TestWeights(unittest.TestCase):
    def test_disjoint_labels_are_fully_unique(self):
        from tape.labels import Label
        labs = [Label("M", 0, 0, 100, UP, 1, 0.5, 0.0, 0.5, False),
                Label("M", 1, 200, 300, UP, 1, 0.5, 0.0, 0.5, False)]
        self.assertEqual(average_uniqueness(labs), [1.0, 1.0])

    def test_fully_overlapping_labels_are_halved(self):
        from tape.labels import Label
        labs = [Label("M", 0, 0, 100, UP, 1, 0.5, 0.0, 0.5, False),
                Label("M", 1, 0, 100, UP, 1, 0.5, 0.0, 0.5, False)]
        u = average_uniqueness(labs)
        self.assertAlmostEqual(u[0], 0.5, places=6)
        self.assertAlmostEqual(u[1], 0.5, places=6)

    def test_sample_weights_normalise_to_mean_one(self):
        from tape.labels import Label
        labs = [Label("M", i, i * 50, i * 50 + 100, UP, 1, 0.5, 0.0, 0.2 * (i + 1), False)
                for i in range(6)]
        w = sample_weights(labs)
        self.assertAlmostEqual(sum(w) / len(w), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
