"""D140: reaction delay counted in slots (simulate_anchored(latency_slots=...))."""

import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tape import curve as cv  # noqa: E402
from tape.outcomes import TradeConfig, simulate_anchored  # noqa: E402

R = 0.0125


def path(n=400, seed=1):
    rng = np.random.default_rng(seed)
    ts = np.cumsum(rng.integers(0, 2, n)) * 1000 + 3_600_000_000      # several swaps per second
    q = 31.0 + np.cumsum(rng.normal(0.02, 0.4, n)).clip(-0.9, None)
    return ts.astype("int64"), q


class TestSlotLatency(unittest.TestCase):
    def test_equals_seconds_when_one_slot_per_second(self):
        ts, q = path()
        slots = (ts // 1000).astype("int64")
        for L in (0, 1, 3):
            for cfg in (TradeConfig(latency_s=L), TradeConfig(take_profit=0.2, stop_loss=-0.1, latency_s=L)):
                a = simulate_anchored(ts, q, 5, cv.K_STD, R, R, cfg, None, None)
                b = simulate_anchored(ts, q, 5, cv.K_STD, R, R, TradeConfig(take_profit=cfg.take_profit,
                                      stop_loss=cfg.stop_loss, latency_s=99), None, None,
                                      slots=slots, latency_slots=L)
                self.assertEqual((a.status, a.exit_ts), (b.status, b.exit_ts))
                self.assertAlmostEqual(a.net_ret, b.net_ret, places=12)
                self.assertEqual(a.entry_q, b.entry_q)

    def test_zero_slots_is_same_block_after_trigger(self):
        ts = np.array([0, 0, 0, 0, 0], dtype="int64") + 3_600_000_000
        slots = np.array([10, 10, 10, 11, 12])
        q = np.array([31.0, 32.0, 33.0, 40.0, 50.0])
        o = simulate_anchored(ts, q, 0, cv.K_STD, R, R, TradeConfig(), None, None, slots=slots, latency_slots=0)
        self.assertEqual(o.entry_q, 33.0)                       # rest of slot 10 executes before us
        o1 = simulate_anchored(ts, q, 0, cv.K_STD, R, R, TradeConfig(), None, None, slots=slots, latency_slots=1)
        self.assertEqual(o1.entry_q, 40.0)
        with self.assertRaises(ValueError):
            simulate_anchored(ts, q, 0, cv.K_STD, R, R, TradeConfig(), None, None, latency_slots=1)


if __name__ == "__main__":
    unittest.main()


class TestDiagScript(unittest.TestCase):
    def test_run_configs_and_summary(self):
        import importlib.util
        import pandas as pd
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        spec = importlib.util.spec_from_file_location("dl", os.path.join(ROOT, "scripts", "diag_latency.py"))
        dl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dl)
        ts, q = path()
        slots = (ts // 400).astype("int64")
        r = dl.run_configs(ts, q, slots, 5, cv.K_STD, R, R, dl.EXITS_E, None)
        self.assertEqual(len(r), len(dl.EXITS_E) * len(dl.DELAYS))
        ref = simulate_anchored(ts, q, 5, cv.K_STD, R, R, TradeConfig(size_sol=2.0), None, None)
        self.assertAlmostEqual(r["base|1s"], ref.net_ret, places=12)
        rng = np.random.default_rng(0)
        rows = [{"group": "E", "mint": f"m{i}", "day": i % 20, "in_E000": True, "in_FLOOR": False,
                 **{k: v + rng.normal(0, 0.01) for k, v in r.items()}} for i in range(200)]
        out = dl.summarize(pd.DataFrame(rows), lambda m: None)
        self.assertIn("E000|base|1slot", out)
        self.assertAlmostEqual(out["E000|base|1s"]["delta_vs_1s"], 0.0)
