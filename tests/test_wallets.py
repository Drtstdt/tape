"""D136: family W -- wallet ledger, point-in-time weekly classes, triggers,
mirror exit, fee ratios, and the mirror exit in the anchored model."""

import importlib.util
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from tape import curve as cv  # noqa: E402
from tape import trials as tr  # noqa: E402
from tape import wallets as wl  # noqa: E402
from tape.outcomes import TradeConfig, simulate_anchored  # noqa: E402

DAY = wl.DAY_MS
MON = wl.MONDAY0 + 2900 * wl.WEEK_MS          # some Monday in 2025


def swaps(rows):
    cols = ["mint", "wallet", "side", "ts_ms", "quote_amount", "fee_sol", "base_amount", "price"]
    return pd.DataFrame(rows, columns=cols)


class TestLedger(unittest.TestCase):
    def test_flows_window_and_mark(self):
        c = MON + 1000
        df = swaps([
            ("A", "w1", "buy", c + 10, 1.0, 0.0125, 100.0, 0.01),
            ("A", "w1", "sell", c + 20, 0.5, 0.00625, 40.0, 0.0125),
            ("A", None, "buy", c + 30, 0.2, 0.0025, 10.0, 0.02),          # walletless: price only
            ("A", "w1", "sell", c + DAY + 1, 9.0, 0.0, 60.0, 0.15),        # after the 24 h window
            ("B", "w2", "buy", c + 5, 2.0, 0.025, 50.0, 0.04),
        ])
        led = wl.ledger_frame(df, {"A": c, "B": c}).set_index(["wallet", "mint"])
        r = led.loc[("w1", "A")]
        self.assertAlmostEqual(r["sol_in"], 1.0125)
        self.assertAlmostEqual(r["sol_out"], 0.5)
        self.assertAlmostEqual(r["p_last"], 0.02)                          # last price inside the window
        self.assertAlmostEqual(r["pnl"], 0.5 - 1.0125 + 60.0 * 0.02)
        self.assertEqual(int(r["settle_ts"]), c + DAY)
        self.assertNotIn((None, "A"), led.index)
        self.assertAlmostEqual(led.loc[("w2", "B")]["pnl"], -2.025 + 50.0 * 0.04)

    def test_swaps_before_create_ignored(self):
        c = MON
        df = swaps([("A", "w1", "buy", c - 5, 1.0, 0.0, 10.0, 0.1), ("A", "w1", "buy", c + 5, 1.0, 0.0, 10.0, 0.1)])
        led = wl.ledger_frame(df, {"A": c})
        self.assertAlmostEqual(float(led["sol_in"].iloc[0]), 1.0)


class TestClasses(unittest.TestCase):
    def _records(self):
        w, st, pnl = [], [], []
        rng = np.random.default_rng(0)
        for i in range(200):                       # 200 wallets x 12 settled tokens before MON
            for _ in range(12):
                w.append(f"w{i}"), st.append(MON - DAY), pnl.append(i / 200.0 + rng.normal(0, 1e-4))
        for _ in range(50):                        # a superstar whose record settles only AFTER MON
            w.append("late"), st.append(MON + 1), pnl.append(100.0)
        for _ in range(9):                         # too few tokens
            w.append("few"), st.append(MON - DAY), pnl.append(100.0)
        return np.array(w, dtype=object), np.array(st, dtype="int64"), np.array(pnl)

    def test_point_in_time_and_min_n(self):
        w, st, pnl = self._records()
        mem, stats = wl.weekly_classes(w, st, pnl, [MON, MON + wl.WEEK_MS])
        self.assertEqual(stats[0]["n_eligible"], 200)
        self.assertFalse(wl.in_class(mem, "late", "top1", 0))             # not known yet at MON
        self.assertTrue(wl.in_class(mem, "late", "top1", 1))              # known a week later
        self.assertFalse(any(wl.in_class(mem, "few", c, k) for c in wl.CLASSES for k in (0, 1)))
        self.assertTrue(wl.in_class(mem, "w199", "top1", 0))
        self.assertTrue(wl.in_class(mem, "w190", "top5", 0))
        self.assertFalse(wl.in_class(mem, "w10", "top5", 0))
        self.assertTrue(wl.in_class(mem, "w100", "placebo", 0))
        self.assertEqual(stats[0]["n_top1"], 2)
        self.assertEqual(stats[0]["n_top5"], 10)
        self.assertEqual(stats[0]["n_placebo"], 40)

    def test_weeks(self):
        self.assertEqual(int(wl.week_start(MON + 3 * DAY)), MON)
        self.assertEqual(int(wl.week_start(MON)), MON)
        self.assertEqual(int(wl.first_snapshot_at_or_after(MON + 1)), MON + wl.WEEK_MS)


class TestTriggerAndMirror(unittest.TestCase):
    def setUp(self):
        self.ts = np.array([0, 1, 2, 3, 4, 5, 6], dtype="int64") * 60_000 + MON + 1000
        self.buy = np.array([True, True, True, True, False, False, True])
        self.qa = np.array([0.05, 0.5, 0.3, 0.4, 0.2, 0.3, 1.0])
        self.ba = np.array([10, 100, 50, 60, 40, 50, 100], dtype=float)
        self.wal = np.array(["L", "x", "L", "y", "L", "L", "L"], dtype=object)
        self.mem = {"L": {"top1": 1, "top5": 1}, "x": {"placebo": 1}}

    def test_trigger(self):
        pos = lambda t: 0                                   # noqa: E731
        c = MON + 1000
        self.assertEqual(wl.find_trigger(self.ts, self.buy, self.qa, self.wal, c, self.mem, "top1", pos), 2)
        self.assertEqual(wl.find_trigger(self.ts, self.buy, self.qa, self.wal, c, self.mem, "placebo", pos), 1)
        self.assertIsNone(wl.find_trigger(self.ts, self.buy, self.qa, self.wal, c, self.mem, "top1", lambda t: None))
        self.assertIsNone(wl.find_trigger(self.ts, self.buy, self.qa, self.wal, c, self.mem, "top1", pos,
                                          max_ms=60_000))   # trigger at 2 min is too late

    def test_trigger_blind_to_future(self):
        pos = lambda t: 0                                   # noqa: E731
        c = MON + 1000
        full = wl.find_trigger(self.ts, self.buy, self.qa, self.wal, c, self.mem, "top1", pos)
        cut = wl.find_trigger(self.ts[:3], self.buy[:3], self.qa[:3], self.wal[:3], c, self.mem, "top1", pos)
        self.assertEqual(full, cut)
        self.assertEqual(wl.pit_fee_ratios(self.buy, self.qa, self.qa * 0.0125, 2),
                         wl.pit_fee_ratios(self.buy[:3], self.qa[:3], self.qa[:3] * 0.0125, 2))

    def test_mirror(self):
        # holdings at trigger 2: L bought 10 (idx 0) + 50 (idx 2) = 60; sells 40 (idx 4) -> 40/60 >= 50%
        self.assertEqual(wl.mirror_exit_index(self.buy, self.ba, self.wal, 2), 4)
        self.assertIsNone(wl.mirror_exit_index(self.buy, self.ba, self.wal, 2, fraction=2.0))

    def test_fee_ratios(self):
        fee = np.array([0.001, 0.01, 0.003, 0.004, 0.003, 0.0045, 0.0])
        fb, fs = wl.pit_fee_ratios(self.buy, self.qa, fee, 5)
        self.assertAlmostEqual(fb, np.median([0.02, 0.02, 0.01, 0.01]))
        self.assertAlmostEqual(fs, np.median([0.015, 0.015]))
        fb2, fs2 = wl.pit_fee_ratios(self.buy, self.qa, fee, 3)
        self.assertAlmostEqual(fs2, fb2)                    # no sell yet -> buy ratio


class TestMirrorInModel(unittest.TestCase):
    def test_mirror_exit_and_default_unchanged(self):
        T0 = 1000 * 3_600_000
        ts = np.arange(12, dtype=np.int64) * 1000 + T0
        q = np.array([31.0, 31.0, 32.0, 33.0, 34.0, 35.0, 36.0, 37.0, 38.0, 39.0, 40.0, 41.0])
        big = TradeConfig(size_sol=0.5, take_profit=1e9, stop_loss=-1e9, latency_s=1)
        o = simulate_anchored(ts, q, 1, cv.K_STD, 0.0125, 0.0125, big, None, None, exit_idx=6)
        self.assertEqual(o.status, "MIRROR")
        self.assertEqual(o.exit_ts, int(ts[7]))             # the leader's sell at 6, we react 1 s later
        a = simulate_anchored(ts, q, 1, cv.K_STD, 0.0125, 0.0125, TradeConfig(latency_s=1), None, None)
        b = simulate_anchored(ts, q, 1, cv.K_STD, 0.0125, 0.0125, TradeConfig(latency_s=1), None, None, exit_idx=None)
        self.assertEqual((a.status, a.net_ret), (b.status, b.net_ret))


class TestTwoSample(unittest.TestCase):
    def test_detects_shift_and_null(self):
        rng = np.random.default_rng(3)
        da, db = rng.integers(0, 40, 2000), rng.integers(0, 40, 2000)
        r = tr.two_sample_day_bootstrap(rng.normal(0.1, 1, 2000), da, rng.normal(0, 1, 2000), db, 500, seed=1)
        self.assertGreater(r["ci"][0], 0)
        r0 = tr.two_sample_day_bootstrap(rng.normal(0, 1, 2000), da, rng.normal(0, 1, 2000), db, 500, seed=1)
        self.assertLess(r0["ci"][0], 0)
        self.assertGreater(r0["ci"][1], 0)


class TestRunnerW(unittest.TestCase):
    def test_grid_budget_and_evaluate(self):
        spec = importlib.util.spec_from_file_location("rfw", os.path.join(ROOT, "scripts", "run_family_W.py"))
        rfw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rfw)
        from tape.registry import FAMILY_BUDGET
        self.assertEqual(len(rfw.GRID), FAMILY_BUDGET["W"])
        self.assertEqual(len(set(rfw.GRID)), 8)
        rng = np.random.default_rng(7)
        rows = []
        for cls, mu in (("top1", 0.2), ("top5", 0.1), ("placebo", -0.05)):
            for i in range(600):
                for e in rfw.EXITS:
                    for s in rfw.SIZES:
                        for lat in rfw.LATENCIES:
                            rows.append({"mint": f"{cls}{i}", "cls": cls, "exit": e, "size": s, "lat": lat,
                                         "status": "TIME", "net": rng.normal(mu, 0.3),
                                         "trig_ts": MON + (i % 60) * DAY, "trig_after_create_s": 30.0,
                                         "leader": f"L{i % 50}", "leader_sol": 0.5, "entry_q": 30 + rng.uniform(0, 20),
                                         "hold_s": 60.0, "mirror_found": True, "fee_b": 0.0125, "fee_s": 0.0125})
        res = pd.DataFrame(rows)
        out = rfw.evaluate(res, lambda m: None, n_boot=200)
        self.assertEqual(len(out), 8)
        r0 = out[0]
        self.assertEqual((r0["cls"], r0["exit"], r0["size"]), ("top1", "mirror", 0.5))
        self.assertGreater(r0["ci"][0], 0)
        self.assertGreater(r0["diff_ci"][0], 0)
        self.assertEqual(r0["placebo_n"], 600)


if __name__ == "__main__":
    unittest.main()


class TestTokenRows(unittest.TestCase):
    def test_rows_for_one_token(self):
        spec = importlib.util.spec_from_file_location("rfw2", os.path.join(ROOT, "scripts", "run_family_W.py"))
        rfw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rfw)
        spec2 = importlib.util.spec_from_file_location("trs2", os.path.join(ROOT, "tests", "test_research_set_v2.py"))
        trs2 = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(trs2)
        g = trs2.real_tape(trs2.FLOWS)
        create = int(g["ts_ms"].iloc[0]) - 1000
        info = pd.Series({"create_ts": create, "bonding_ts": float("nan")}, name="M")
        snaps = [int(wl.week_start(create))]
        mem = {"w3": {"top1": 1, "top5": 1}, "w4": {"placebo": 1}}
        rows, st = rfw.token_rows(g, info, mem, {snaps[0]: 0}, trs2.COLLECTED)
        self.assertEqual(st, "ok")
        df = pd.DataFrame(rows)
        self.assertEqual(sorted(df["cls"].unique()), ["placebo", "top1", "top5"])
        self.assertEqual(len(df), 3 * 2 * 2 * 2)
        top = df[df["cls"] == "top1"]
        self.assertTrue((top["leader"] == "w3").all())
        first_w3 = g[(g["wallet"] == "w3") & (g["side"] == "buy") & (g["quote_amount"] >= 0.1)]["ts_ms"].min()
        self.assertTrue((top["trig_ts"] == first_w3).all())
        self.assertFalse(df["status"].isin(["CENS", "NOENTRY"]).any())
        self.assertTrue(set(df.loc[df["exit"] == "fixed", "status"]) <= {"TP", "SL", "TIME"})
        self.assertTrue(set(df.loc[df["exit"] == "mirror", "status"]) <= {"MIRROR", "TIME"})
        # no class member -> no rows
        rows2, _ = rfw.token_rows(g, info, {}, {snaps[0]: 0}, trs2.COLLECTED)
        self.assertEqual(rows2, [])
