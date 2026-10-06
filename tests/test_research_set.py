"""D124: research set v1 -- bars match BarBuilder, features never see the
future, the counterfactual curve replay is exact on known cases."""

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

from tape.outcomes import TradeConfig, simulate, curve_checks  # noqa: E402
from tape import pit_features as pf  # noqa: E402
from tape.bars import BarBuilder, band_bar_threshold  # noqa: E402
from tape.schema import CanonicalSwap  # noqa: E402

spec = importlib.util.spec_from_file_location("brs", os.path.join(ROOT, "scripts", "build_research_set_v1.py"))
brs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brs)

K_INV = 30.0 * 1.073e9
H = 3_600_000
T0 = 1000 * H                       # an hour-aligned epoch ms
FEE = 0.0125


def curve_tape(flows, start_ms=T0 + 60_000, step_ms=2000, wallets=None, slots=None):
    """flows: list of ('buy', sol_into_curve) / ('sell', tokens_into_curve).
    Returns a DataFrame shaped like the store, reserves exactly consistent."""
    q = 30.0
    rows = []
    for i, (side, amt) in enumerate(flows):
        t = K_INV / q
        if side == "buy":
            q2 = q + amt
            tok = t - K_INV / q2
            sol = amt
        else:
            t2 = t + amt
            q2 = K_INV / t2
            tok = amt
            sol = q - q2
        q = q2
        rows.append({"mint": "M", "ts_ms": start_ms + i * step_ms, "slot": (slots[i] if slots else 100 + i),
                     "sig": f"s{i:05d}", "side": side, "base_amount": tok, "quote_amount": sol,
                     "wallet": (wallets[i] if wallets else f"w{i % 13}"),
                     "base_reserve_after": K_INV / q, "quote_reserve_after": q,
                     "real_quote_reserve_after": q - 30.0, "fee_sol": sol * FEE, "price": sol / tok,
                     "source": "pumpfundata", "venue": "v", "pool": "p",
                     "quote_mint": "So11111111111111111111111111111111111111112"})
    return pd.DataFrame(rows)


def arrays(df):
    return (df["ts_ms"].to_numpy("int64"), (df["side"] == "buy").to_numpy(), df["quote_amount"].to_numpy(float),
            df["base_amount"].to_numpy(float), df["quote_reserve_after"].to_numpy(float),
            df["base_reserve_after"].to_numpy(float))


COLLECTED = {T0 // H, T0 // H + 1, T0 // H + 2}


class TestSimulate(unittest.TestCase):
    def test_round_trip_with_no_other_trades_costs_only_fees(self):
        df = curve_tape([("buy", 0.5)] * 3)
        ts, b, qa, ba, qr, tr = arrays(df)
        o = simulate(ts, b, qa, ba, qr, tr, 2, FEE, TradeConfig(latency_s=0), COLLECTED)
        self.assertEqual(o.status, "TIME")
        self.assertAlmostEqual(o.net_ret, (1 - FEE) / (1 + FEE) - 1, places=9)
        self.assertEqual(o.n_after, 0)

    def test_take_profit_and_matches_analytic(self):
        df = curve_tape([("buy", 0.5)] * 3 + [("buy", 3.0)] * 10)
        ts, b, qa, ba, qr, tr = arrays(df)
        o = simulate(ts, b, qa, ba, qr, tr, 2, FEE, TradeConfig(latency_s=0), COLLECTED)
        self.assertEqual(o.status, "TP")
        self.assertGreaterEqual(o.net_ret, 0.60)
        # analytic: our tokens and the curve after the trigger
        q0 = qr[2]
        sol_in = 2.0 / (1 + FEE)
        tokens = K_INV / q0 - K_INV / (q0 + sol_in)
        n_after = o.n_after
        q_end = q0 + sol_in + 3.0 * n_after
        gross = q_end - K_INV / (K_INV / q_end + tokens)
        self.assertAlmostEqual(o.net_ret, gross * (1 - FEE) / 2.0 - 1, places=9)

    def test_stop_loss(self):
        df = curve_tape([("buy", 0.5)] * 3 + [("sell", 2e7)] * 30)
        ts, b, qa, ba, qr, tr = arrays(df)
        o = simulate(ts, b, qa, ba, qr, tr, 2, FEE, TradeConfig(latency_s=0), COLLECTED)
        self.assertEqual(o.status, "SL")
        self.assertLessEqual(o.net_ret, -0.30)

    def test_latency_moves_entry(self):
        df = curve_tape([("buy", 0.5)] * 3 + [("buy", 1.0)] * 5, step_ms=500)
        ts, b, qa, ba, qr, tr = arrays(df)
        o0 = simulate(ts, b, qa, ba, qr, tr, 2, FEE, TradeConfig(latency_s=0), COLLECTED)
        o1 = simulate(ts, b, qa, ba, qr, tr, 2, FEE, TradeConfig(latency_s=1), COLLECTED)
        self.assertGreater(o1.entry_q, o0.entry_q)       # entered after 2 more buys

    def test_cens_when_window_leaves_collected_hours(self):
        df = curve_tape([("buy", 0.5)] * 3, start_ms=T0 + 50 * 60_000)
        ts, b, qa, ba, qr, tr = arrays(df)
        o = simulate(ts, b, qa, ba, qr, tr, 2, FEE, TradeConfig(), {T0 // H})
        self.assertEqual(o.status, "CENS")
        self.assertTrue(math.isnan(o.net_ret))

    def test_graduation_exits_before(self):
        df = curve_tape([("buy", 0.5)] * 3 + [("buy", 1.0)] * 10)
        ts, b, qa, ba, qr, tr = arrays(df)
        o = simulate(ts, b, qa, ba, qr, tr, 2, FEE, TradeConfig(latency_s=0, take_profit=9.0),
                     COLLECTED, bonding_ts=int(ts[6]))
        self.assertTrue(o.graduated)
        self.assertEqual(o.exit_ts, int(ts[5]))

    def test_curve_checks(self):
        df = curve_tape([("buy", 0.5), ("sell", 1e6), ("buy", 2.0)])
        c = curve_checks(*[arrays(df)[i] for i in (1, 2, 4, 5)])
        self.assertEqual(c["curve_amount_match"], 1.0)
        self.assertLess(c["k_rel_spread"], 1e-9)


class TestBarsAndNoLookahead(unittest.TestCase):
    def test_bar_close_indices_match_barbuilder(self):
        rng = np.random.default_rng(3)
        df = curve_tape([("buy", float(x)) if rng.random() < 0.6 else ("sell", float(x) * 3e6)
                         for x in rng.uniform(0.01, 0.6, 400)])
        from tape.swaps_cache import rows_to_swaps
        depth = float(df["quote_reserve_after"].iloc[0])
        bb = BarBuilder("dollar", threshold=band_bar_threshold(depth, 0.01))
        ref = [i for i, s in enumerate(rows_to_swaps(df)) if bb.push(s) is not None]
        self.assertEqual(brs.bar_close_indices(df["quote_amount"].to_numpy(float), depth), ref)

    def _info(self, create_ts):
        return pd.Series({"create_ts": create_ts, "bonding_ts": float("nan"), "creator": "w0",
                          "is_mayhem_mode": False, "creator_prior_launches": 3.0, "creator_prior_grads": 1.0,
                          "creator_prior_grad_rate": 1 / 3, "creator_launches_24h": 1.0,
                          "regime_creates_prev_hour": 500.0})

    def test_features_ignore_everything_after_the_decision(self):
        rng = np.random.default_rng(7)
        flows = [("buy", float(x)) for x in rng.uniform(0.1, 0.5, 300)]
        df = curve_tape(flows)
        info = self._info(int(df["ts_ms"].iloc[0]) - 1000)
        base = brs.process_token_rows(df, info, "M", [5, 10, 20], 3_600_000, COLLECTED)
        self.assertEqual([r["K"] for r in base], [5, 10, 20])
        d_max = max(r["dec_idx"] for r in base)
        fut = df.copy()
        later = fut.index > d_max
        fut.loc[later, "quote_amount"] *= 7.0               # rewrite the whole future
        fut.loc[later, "price"] *= 0.01
        fut.loc[later, "wallet"] = "whale"
        fut.loc[later, "side"] = "sell"
        pert = brs.process_token_rows(fut, info, "M", [5, 10, 20], 3_600_000, COLLECTED)
        for r0, r1 in zip(base, pert):
            for k in r0:
                if k.startswith("o_") or k.startswith("chk_"):
                    continue
                a, b = r0[k], r1[k]
                if isinstance(a, float) and math.isnan(a):
                    self.assertTrue(isinstance(b, float) and math.isnan(b), k)
                else:
                    self.assertEqual(a, b, k)
        self.assertNotEqual(base[0]["o_base_net"], pert[0]["o_base_net"])   # outcomes do look ahead

    def test_no_row_before_k_bars_or_after_max_decision(self):
        df = curve_tape([("buy", 0.05)] * 40)              # tiny volume: few bars
        info = self._info(int(df["ts_ms"].iloc[0]))
        rows = brs.process_token_rows(df, info, "M", [5, 10, 20], 3_600_000, COLLECTED)
        nb = len(brs.bar_close_indices(df["quote_amount"].to_numpy(float),
                                       float(df["quote_reserve_after"].iloc[0])))
        self.assertEqual([r["K"] for r in rows], [k for k in (5, 10, 20) if k <= nb])
        rows2 = brs.process_token_rows(df, info, "M", [5], 1, COLLECTED)
        self.assertEqual(rows2, [])


class TestContext(unittest.TestCase):
    def test_creator_history_point_in_time(self):
        ct = np.array([100, 200, 300, 400, 150], dtype=np.int64)
        cr = np.array(["A", "A", "A", "A", "B"], dtype=object)
        bt = np.array([250, np.nan, 1000, np.nan, 160], dtype=float)
        h = pf.creator_history(ct, cr, bt)
        self.assertEqual(list(h["creator_prior_launches"]), [0, 1, 2, 3, 0])
        self.assertEqual(list(h["creator_prior_grads"]), [0, 0, 1, 1, 0])     # 250 < 300; 1000 not < 400
        self.assertTrue(math.isnan(h["creator_prior_grad_rate"][0]))
        self.assertAlmostEqual(h["creator_prior_grad_rate"][3], 1 / 3)

    def test_creates_prev_hour_needs_collected_window(self):
        allc = np.sort(np.array([T0 + 10, T0 + 20, T0 + H + 5]))
        out = pf.creates_prev_hour(np.array([T0 + H + 30, T0 + 3 * H]), allc, {T0 // H, T0 // H + 1})
        self.assertEqual(out[0], 1.0)          # trailing window [t-1h, t): only T0+H+5
        self.assertTrue(math.isnan(out[1]))


if __name__ == "__main__":
    unittest.main()
