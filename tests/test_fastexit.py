"""D142: the vectorised exit grid equals simulate_anchored exactly."""

import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tape import curve as cv  # noqa: E402
from tape.fastexit import exit_grid, NO_TP, NO_SL  # noqa: E402
from tape.outcomes import TradeConfig, simulate_anchored  # noqa: E402

T0 = 3_600_000_000


def rand_path(rng, n):
    slot_rel = np.concatenate([[0], np.cumsum(rng.integers(0, 3, n - 1))]).astype(np.int64)
    rel_ts = (slot_rel * 400 + rng.integers(0, 50, n)).astype(np.int64)
    rel_ts = np.maximum.accumulate(rel_ts // 1000 * 1000)            # whole seconds, non-decreasing
    q = 31.0 + np.abs(np.cumsum(rng.normal(0.05, 0.8, n)))
    return rel_ts, slot_rel, q


class TestExactness(unittest.TestCase):
    def test_matches_simulate_anchored(self):
        rng = np.random.default_rng(11)
        tps = np.array([0.1, 0.3, 0.6, NO_TP])
        sls = np.array([-0.05, -0.2, NO_SL])
        hs = np.array([60_000, 300_000, 1_800_000])
        checked = 0
        for trial in range(60):
            n = int(rng.integers(5, 400))
            rel_ts, slot_rel, q = rand_path(rng, n)
            grad = int(rng.integers(1, n + 1)) if trial % 3 == 0 else n
            bt = None if grad >= n else int(T0 + rel_ts[grad])
            # bonding_ts semantics: first swap with ts >= bonding_ts is `grad`
            if bt is not None:
                grad = int(np.searchsorted(rel_ts + T0, bt, side="left"))
            for L in (0, 1, 2, 4):
                for size, tip in ((2.0, 0.0), (0.5, 0.001)):
                    g = exit_grid(rel_ts, slot_rel, q, grad, cv.K_STD, 0.0125, 0.011, size, L, tip, tps, sls, hs)
                    for a, tp in enumerate(tps):
                        for b, sl in enumerate(sls):
                            for c, h in enumerate(hs):
                                o = simulate_anchored(rel_ts + T0, q, 0, cv.K_STD, 0.0125, 0.011,
                                                      TradeConfig(size_sol=size, take_profit=tp, stop_loss=sl,
                                                                  horizon_ms=int(h), latency_s=9, fixed_cost_sol=tip),
                                                      None, bt, slots=slot_rel, latency_slots=L)
                                if o.status == "NOENTRY":
                                    self.assertTrue(np.isnan(g[a, b, c]))
                                else:
                                    self.assertAlmostEqual(g[a, b, c], o.net_ret, places=10,
                                                           msg=f"trial {trial} L{L} tp{tp} sl{sl} h{h}")
                                checked += 1
        self.assertGreater(checked, 10_000)


if __name__ == "__main__":
    unittest.main()
