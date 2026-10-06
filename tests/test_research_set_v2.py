"""D126: research set v2 -- in-slot order recovered from real reserves, vendor
virtual fields ignored, anchored outcome exact on known cases, features still
blind to the future."""

import importlib.util
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from tape import curve as cv  # noqa: E402
from tape.outcomes import TradeConfig, simulate_anchored  # noqa: E402

spec = importlib.util.spec_from_file_location("brs2", os.path.join(ROOT, "scripts", "build_research_set_v2.py"))
brs2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brs2)

H = 3_600_000
T0 = 1000 * H
R = 0.0125
COLLECTED = {T0 // H, T0 // H + 1, T0 // H + 2}


def real_tape(flows, V=cv.V_STD, k=cv.K_STD, per_slot=1, step_ms=1000, start=T0 + 60_000, garbage_virtual=False):
    """flows: ('buy', sol_into_curve) | ('sell', tokens_into_curve). Vendor semantics
    (D126): buy amount = curve flow, fee on top; sell amount = what the user gets,
    curve pays amount + fee."""
    r = 0.0
    rows = []
    for i, (side, x) in enumerate(flows):
        q = V + r
        t = k / q
        if side == "buy":
            tok = t - k / (q + x)
            amt, fee, r = x, x * R, r + x
        else:
            out = q - k / (t + x)
            tok = x
            amt = out / (1 + R)
            fee = amt * R
            r = r - out
        slot = 100 + i // per_slot
        rows.append({"mint": "M", "ts_ms": start + (i // per_slot) * step_ms, "slot": slot, "sig": f"s{i:05d}",
                     "side": side, "base_amount": tok, "quote_amount": amt, "fee_sol": fee,
                     "wallet": f"w{i % 11}", "real_quote_reserve_after": r,
                     "quote_reserve_after": (12.3 + i if garbage_virtual else V + r),
                     "base_reserve_after": (2e9 if garbage_virtual else k / (V + r)),
                     "price": amt / tok if tok else 0.0, "source": "pumpfundata", "venue": "v", "pool": "p",
                     "quote_mint": "So11111111111111111111111111111111111111112"})
    return pd.DataFrame(rows)


def info(create_ts):
    return pd.Series({"create_ts": create_ts, "bonding_ts": float("nan"), "creator": "w0", "is_mayhem_mode": False,
                      "creator_prior_launches": 0.0, "creator_prior_grads": 0.0, "creator_prior_grad_rate": float("nan"),
                      "creator_launches_24h": 0.0, "regime_creates_prev_hour": float("nan")})


def shuffle_within_slots(df, seed=0):
    rng = np.random.default_rng(seed)
    df = df.assign(_r=rng.random(len(df)), sig=[f"z{x:.6f}" for x in rng.random(len(df))])
    return df.sort_values(["ts_ms", "slot", "sig"]).drop(columns="_r").reset_index(drop=True)


FLOWS = [("buy", float(x)) if i % 4 else ("sell", float(x) * 2e7)
         for i, x in enumerate(np.random.default_rng(5).uniform(0.05, 0.8, 240))]


class TestPrepare(unittest.TestCase):
    def test_order_recovered_and_virtual_fields_ignored(self):
        true = real_tape(FLOWS, per_slot=4)
        g, q_mkt, params, share = brs2.prepare_token(shuffle_within_slots(real_tape(FLOWS, per_slot=4,
                                                                                    garbage_virtual=True)))
        self.assertEqual(share, 1.0)
        np.testing.assert_allclose(g["real_quote_reserve_after"].to_numpy(), true["real_quote_reserve_after"].to_numpy())
        np.testing.assert_allclose(g["quote_reserve_after"].to_numpy(), 30.0 + true["real_quote_reserve_after"].to_numpy())
        self.assertFalse(params["fitted"])
        self.assertTrue(params["usable"])

    def test_nonstandard_curve_is_fitted(self):
        g, q_mkt, params, share = brs2.prepare_token(real_tape(FLOWS, V=55.0, k=4.1e10))
        self.assertTrue(params["fitted"])
        self.assertTrue(params["usable"])
        self.assertAlmostEqual(params["V"], 55.0, delta=0.05)

    def test_rows_identical_with_garbage_virtual_fields(self):
        a = real_tape(FLOWS)
        b = real_tape(FLOWS, garbage_virtual=True)
        ra = brs2.process_token_rows(a, info(int(a["ts_ms"].iloc[0]) - 1000), "M", [5, 10, 20], H, COLLECTED)
        rb = brs2.process_token_rows(b, info(int(a["ts_ms"].iloc[0]) - 1000), "M", [5, 10, 20], H, COLLECTED)
        self.assertEqual(len(ra), 3)
        for x, y in zip(ra, rb):
            for k in x:
                if isinstance(x[k], float) and math.isnan(x[k]):
                    self.assertTrue(math.isnan(y[k]), k)
                else:
                    self.assertEqual(x[k], y[k], k)

    def test_features_blind_to_future(self):
        a = real_tape(FLOWS)
        inf = info(int(a["ts_ms"].iloc[0]) - 1000)
        base = brs2.process_token_rows(a, inf, "M", [5, 10, 20], H, COLLECTED)
        dmax = max(r["dec_idx"] for r in base)
        fut = real_tape(FLOWS[:dmax + 1] + [("sell", 5e7)] * 60)
        pert = brs2.process_token_rows(fut, inf, "M", [5, 10, 20], H, COLLECTED)
        for x, y in zip(base, pert):
            for k in x:
                if k.startswith("o_") or k.startswith("chk_") or k == "outcome_usable":
                    continue
                if isinstance(x[k], float) and math.isnan(x[k]):
                    self.assertTrue(math.isnan(y[k]), k)
                else:
                    self.assertEqual(x[k], y[k], k)
        self.assertNotEqual(base[0]["o_base_net"], pert[0]["o_base_net"])


class TestAnchored(unittest.TestCase):
    def test_no_trades_costs_only_fees(self):
        ts = np.array([T0 + 60_000, T0 + 61_000], dtype=np.int64)
        q = np.array([31.0, 31.0])
        o = simulate_anchored(ts, q, 1, cv.K_STD, R, R, TradeConfig(latency_s=0), COLLECTED)
        self.assertAlmostEqual(o.net_ret, 1 / ((1 + R) * (1 + R)) - 1, places=9)

    def test_tp_closed_form(self):
        ts = np.arange(10, dtype=np.int64) * 1000 + T0 + 60_000
        q = np.array([31.0] * 2 + [31.0 + 2.5 * i for i in range(1, 9)])
        o = simulate_anchored(ts, q, 1, cv.K_STD, R, R, TradeConfig(latency_s=0), COLLECTED)
        self.assertEqual(o.status, "TP")
        sol_in = 2.0 / (1 + R)
        N = cv.K_STD / 31.0 - cv.K_STD / (31.0 + sol_in)
        qq = q[1 + o.n_after] + sol_in
        gross = qq - cv.K_STD / (cv.K_STD / qq + N)
        self.assertAlmostEqual(o.net_ret, gross / (1 + R) / 2.0 - 1, places=9)

    def test_sl(self):
        ts = np.arange(10, dtype=np.int64) * 1000 + T0 + 60_000
        q = np.array([40.0] * 2 + [40.0 - 1.5 * i for i in range(1, 9)])
        o = simulate_anchored(ts, q, 1, cv.K_STD, R, R, TradeConfig(latency_s=0), COLLECTED)
        self.assertEqual(o.status, "SL")


if __name__ == "__main__":
    unittest.main()


class TestLongHold(unittest.TestCase):
    """D132: simulate_long on gapped tapes."""

    def _path(self, qs, step_ms=600_000):
        from tape.curve import V_STD
        ts = np.arange(len(qs), dtype=np.int64) * step_ms + T0
        q = np.array(qs, dtype=float)
        flow = np.concatenate([[q[0] - V_STD], np.diff(q)])
        return ts, q, flow

    def test_time_exit_uses_pre_state_of_first_swap_after_deadline(self):
        from tape.outcomes import simulate_long
        ts, q, flow = self._path([31.0, 31.0, 32.0, 33.0])
        ts[3] = ts[2] + 5 * 3_600_000                      # a long gap, deadline (2 h) inside it
        o = simulate_long(ts, q, flow, 1, cv.K_STD, cv.V_STD, R, R, 2.0, 0, 2 * 3_600_000, None, None)
        self.assertEqual(o.status, "TIME")
        ref = simulate_long(ts[:3], q[:3], flow[:3], 1, cv.K_STD, cv.V_STD, R, R, 2.0, 0, 10 ** 9, None, None)
        self.assertAlmostEqual(o.net_ret, ref.net_ret, places=12)          # pre-state of swap 3 == post of swap 2
        self.assertGreater(o.exit_delay_s, 0)

    def test_tp_graduation_and_no_later_swap(self):
        from tape.outcomes import simulate_long
        ts, q, flow = self._path([31.0, 31.0, 40.0, 60.0, 90.0])
        o = simulate_long(ts, q, flow, 1, cv.K_STD, cv.V_STD, R, R, 2.0, 0, 24 * 3_600_000, 1.0, None)
        self.assertEqual(o.status, "TP")
        self.assertGreaterEqual(o.net_ret, 1.0)
        g = simulate_long(ts, q, flow, 1, cv.K_STD, cv.V_STD, R, R, 2.0, 0, 24 * 3_600_000, None, int(ts[3]))
        self.assertEqual(g.status, "GRAD")
        self.assertEqual(g.exit_ts, int(ts[3]))                           # completing swap included
        d = simulate_long(ts[:2], q[:2], flow[:2], 1, cv.K_STD, cv.V_STD, R, R, 2.0, 0, 3_600_000, None, None)
        self.assertEqual(d.status, "NO_LATER_SWAP")
        self.assertLess(d.net_pessimistic, d.net_ret + 1e-12)
        self.assertAlmostEqual(d.net_ret, 1 / ((1 + R) * (1 + R)) - 1, places=9)


class TestFamilyHGrid(unittest.TestCase):
    def test_twelve_trials(self):
        spec = importlib.util.spec_from_file_location("rfh", os.path.join(ROOT, "scripts", "run_family_H.py"))
        rfh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rfh)
        from tape.registry import FAMILY_BUDGET
        self.assertEqual(len(rfh.CONFIGS) * len(rfh.SETS), 16)
        self.assertEqual(FAMILY_BUDGET["H"], 16)
        self.assertEqual(sum(1 for c in rfh.CONFIGS if c[2]), 2)


class TestPartial(unittest.TestCase):
    """D134: sell a fraction at the TP, the rest follows a rule."""

    def _path(self, qs, step_ms=600_000):
        from tape.curve import V_STD
        ts = np.arange(len(qs), dtype=np.int64) * step_ms + T0
        q = np.array(qs, dtype=float)
        flow = np.concatenate([[q[0] - V_STD], np.diff(q)])
        return ts, q, flow

    def _run(self, qs, rest, frac=0.5, h=6, bt=None, **kw):
        from tape.outcomes import simulate_partial
        ts, q, flow = self._path(qs, **kw)
        return simulate_partial(ts, q, flow, 1, cv.K_STD, cv.V_STD, R, R, 2.0, 0, h * 3_600_000, 1.0, frac, rest, bt)

    def test_no_tp_equals_simulate_long(self):
        from tape.outcomes import simulate_long
        qs = [31.0, 31.0, 32.0, 31.5, 33.0, 32.0] + [32.5] * 40
        ts, q, flow = self._path(qs)
        a = simulate_long(ts, q, flow, 1, cv.K_STD, cv.V_STD, R, R, 2.0, 0, 6 * 3_600_000, 1.0, None)
        for rest in ("time", "breakeven"):
            b = self._run(qs, rest)
            self.assertEqual((a.status, a.exit_ts), (b.status, b.exit_ts))
            self.assertAlmostEqual(a.net_ret, b.net_ret, places=12)

    def test_full_fraction_equals_tp_exit(self):
        from tape.outcomes import simulate_long
        qs = [31.0, 31.0, 40.0, 60.0, 30.5] + [30.5] * 40
        ts, q, flow = self._path(qs)
        a = simulate_long(ts, q, flow, 1, cv.K_STD, cv.V_STD, R, R, 2.0, 0, 6 * 3_600_000, 1.0, None)
        b = self._run(qs, "time", frac=1.0)
        self.assertEqual(a.status, "TP")
        self.assertAlmostEqual(a.net_ret, b.net_ret, places=9)     # the rest is zero tokens

    def test_two_sales_at_one_state_equal_one_sale(self):
        # TP hit, then the deadline passes with the market unchanged -> same as selling all at the TP state
        from tape.outcomes import simulate_long
        qs = [31.0, 31.0, 60.0] + [60.0] * 40
        ts, q, flow = self._path(qs)
        a = simulate_long(ts, q, flow, 1, cv.K_STD, cv.V_STD, R, R, 2.0, 0, 6 * 3_600_000, 1.0, None)
        b = self._run(qs, "time")
        self.assertEqual(b.status, "PART_TIME")
        self.assertAlmostEqual(a.net_ret, b.net_ret, places=9)

    def test_breakeven_stop_and_linearity(self):
        qs = [31.0, 31.0, 60.0, 45.0, 30.9, 30.5] + [80.0] * 40
        stop = self._run(qs, "breakeven")
        held = self._run(qs, "time")
        self.assertEqual(stop.status, "PART_STOP")
        self.assertEqual(stop.exit_ts, T0 + 4 * 600_000)          # first state at/below entry (30.9 <= 31)
        self.assertEqual(held.status, "PART_TIME")
        self.assertGreater(held.net_ret, stop.net_ret)             # the rest rode the later rally
        self.assertGreater(stop.net_ret, 0.0)                      # half sold at ~+100% covers the rest

    def test_partial_graduation(self):
        qs = [31.0, 31.0, 60.0, 70.0, 90.0, 95.0] + [95.0] * 10
        ts, _, _ = self._path(qs)
        o = self._run(qs, "time", bt=int(ts[4]))
        self.assertEqual(o.status, "PART_GRAD")
        self.assertEqual(o.exit_ts, int(ts[4]))
