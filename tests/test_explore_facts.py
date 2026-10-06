"""D119: explore_facts_v1 -- coverage-aware label classification.

The point of these tests: a label is censored by the COLLECTION schedule
(pumpfundata hourly files, ~every other UTC hour), never by its own outcome,
and everything else matches info_audit_v2.build_one exactly.
"""

import importlib.util
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tape.labels import BarrierConfig, UP, DOWN, TIMEOUT  # noqa: E402
from tape.schema import CanonicalSwap  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ef = _load("explore_facts_v1", "scripts/explore_facts_v1.py")

H = 3_600_000
T10 = 10 * H          # an arbitrary UTC hour index 10 (epoch-relative is fine)
CFG = BarrierConfig(1.6, -0.30, 30 * 60_000)


def tape(start_ms, end_ms, step_ms=10_000, price_fn=lambda i: 1.0, mint="M", seed=0):
    out = []
    i = 0
    t = start_ms
    res = 30.0
    while t < end_ms:
        p = price_fn(i)
        out.append(CanonicalSwap(mint=mint, venue="v", pool="p", ts_ms=t, slot=seed * 10**6 + i,
                                 sig=f"s{seed}_{i}", side="buy" if i % 2 == 0 else "sell",
                                 base_amount=0.5 / p, quote_amount=0.5, quote_mint="SOL", price=p,
                                 wallet=f"w{i % 7}", quote_reserve_after=res,
                                 base_reserve_after=res / p, source="pumpfundata"))
        i += 1
        t += step_ms
    return out


class TestCoverageHelpers(unittest.TestCase):
    def test_observed_hours_threshold(self):
        counts = {0: 100_000, 1: 150, 2: 120_000, 3: 90_000, 4: 210, 5: 0, 6: 110_000}
        obs, thr = ef.observed_hours_from_counts(counts)
        self.assertEqual(obs, {0, 2, 3, 6})
        self.assertGreater(thr, 210)

    def test_window_fully_observed(self):
        obs = {10, 11, 13}
        self.assertTrue(ef.window_fully_observed(T10 + 5, T10 + H + 10, obs))
        self.assertFalse(ef.window_fully_observed(T10 + H + 1, T10 + 2 * H + 1, obs))
        self.assertTrue(ef.window_fully_observed(T10, T10 + H - 1, {10}))
        self.assertFalse(ef.window_fully_observed(T10, T10 + H, {10}))

    def test_classify_is_outcome_independent_for_cens(self):
        obs = {10}
        t0 = T10 + 45 * 60_000      # window runs into hour 11 (not collected)
        for outcome in (UP, DOWN, TIMEOUT):
            for trunc in (False, True):
                self.assertEqual(ef.classify_label(outcome, trunc, t0, CFG.horizon_ms, obs), "CENS")

    def test_classify_dead_and_normal(self):
        obs = {10, 11}
        t0 = T10 + 45 * 60_000
        self.assertEqual(ef.classify_label(TIMEOUT, True, t0, CFG.horizon_ms, obs), "DEAD")
        self.assertEqual(ef.classify_label(UP, False, t0, CFG.horizon_ms, obs), "UP")
        self.assertEqual(ef.classify_label(DOWN, False, t0, CFG.horizon_ms, obs), "DOWN")
        self.assertEqual(ef.classify_label(TIMEOUT, False, t0, CFG.horizon_ms, obs), "TIMEOUT")

    def test_minutes_to_coverage_end(self):
        obs = {10, 11}
        self.assertAlmostEqual(ef.minutes_to_coverage_end(T10 + 30 * 60_000, obs), 90.0)
        self.assertEqual(ef.minutes_to_coverage_end(T10 + 3 * H, obs), 0.0)

    def test_auc(self):
        self.assertAlmostEqual(ef.auc([1, 2, 3, 4], [0, 0, 1, 1]), 1.0)
        self.assertAlmostEqual(ef.auc([4, 3, 2, 1], [0, 0, 1, 1]), 0.0)
        self.assertAlmostEqual(ef.auc([1, 1, 1, 1], [0, 1, 0, 1]), 0.5)


class TestProcessToken(unittest.TestCase):
    def test_gap_makes_fake_timeouts_today_and_cens_here(self):
        # flat price, trades 10:00-10:59 and 12:00-12:59; hour 11 not collected
        sw = tape(T10, T10 + H) + tape(T10 + 2 * H, T10 + 3 * H, seed=1)
        rec, rows = ef.process_token("M", sw, CFG, {10, 12})
        self.assertTrue(rec["passes_lookahead_filter"])
        # today's semantics: every early-hour-10 label is TIMEOUT and NOT truncated
        late = [r for r in rows if r[1] >= T10 + 31 * 60_000 and r[1] < T10 + H]
        self.assertTrue(late)
        for r in late:
            self.assertEqual(r[5], TIMEOUT)      # old outcome
            self.assertFalse(r[6])               # not truncated -> kept today as y=0
            self.assertEqual(r[7], "CENS")       # but its window was never observed
        early = [r for r in rows if r[1] < T10 + 29 * 60_000]
        self.assertTrue(early)
        for r in early:
            self.assertEqual(r[7], "TIMEOUT")

    def test_dead_token(self):
        sw = tape(T10, T10 + 20 * 60_000)      # stops trading at 10:20, hours 10-11 collected
        rec, rows = ef.process_token("M", sw, CFG, {10, 11})
        self.assertTrue(rows)
        self.assertTrue(all(r[7] == "DEAD" for r in rows))
        self.assertTrue(all(r[6] for r in rows))   # today: truncated -> silently dropped

    def test_up_and_k_decisions(self):
        sw = tape(T10, T10 + 50 * 60_000, price_fn=lambda i: 1.0 + 0.004 * i)
        rec, rows = ef.process_token("M", sw, CFG, {10})
        self.assertEqual(rec["k5_cls"], "UP")
        self.assertEqual(rec["k5_old"], "UP")
        self.assertIsNotNone(rec["k20_t0"])
        self.assertGreater(rec["k20_t0"], rec["k5_t0"])

    def test_k_decision_when_kth_bar_is_last(self):
        sw = tape(T10, T10 + 5 * 60_000)
        rec, _ = ef.process_token("M", sw, CFG, {10, 11})
        nb = rec["n_bars"]
        self.assertGreaterEqual(nb, 5)
        if nb < 20:
            self.assertIsNone(rec["k20_cls"])
        self.assertEqual(rec["k5_cls"], "DEAD")

    def test_parity_with_info_audit_v2_build_one(self):
        # stub duckdb so info_audit_v2 imports without it (store is not used: swaps passed in)
        sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))
        ia = _load("info_audit_v2", "scripts/info_audit_v2.py")
        sw = (tape(T10, T10 + H, price_fn=lambda i: 1.0 + 0.002 * (i % 50))
              + tape(T10 + 2 * H, T10 + 2 * H + 20 * 60_000, seed=1,
                     price_fn=lambda i: 1.1 - 0.003 * i))
        status, frows, ys = ia.build_one(None, "M", CFG, 0.01, 5, False, swaps=sw)
        self.assertEqual(status, "ok")
        _, rows = ef.process_token("M", sw, CFG, {10, 12})
        kept = [r for r in rows if not r[6]]
        self.assertEqual(len(kept), len(ys))
        self.assertEqual([1.0 if r[5] == UP else 0.0 for r in kept], ys)
        self.assertEqual([float(r[2]) for r in kept], [f["age_ms"] for f in frows])


if __name__ == "__main__":
    unittest.main()
