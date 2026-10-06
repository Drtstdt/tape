"""The entry strategy: rails fail closed, the gate vetoes on measured
quantities, the model gate respects calibration + credibility, sizing is
Kelly capped by depth, and decide_entry is PURE (no I/O, no clock)."""

import sys, os, unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape.bot.spec import BandSpec, BotSpec, EntrySpec
from tape.bot.strategy import build_signals, decide_entry
from tape.costs import CostModel

BAND = BandSpec("B_small", 15.0, 2.0, -0.40, 45 * 60_000, 0.5, 0.3)
COST = CostModel(fee_pct=0.01, fixed_cost_sol=0.0005)


def feats(**over):
    base = {
        "n_bars": 30.0, "age_ms": 300_000.0, "price": 1.0, "liquidity": 200.0,
        "unique_buyers_10": 8.0, "unique_sellers_10": 3.0,
        "largest_buyer_share_10": 0.2,
        "netflow_10_over_liq": 0.04, "buyer_seller_breadth_10": 0.45,
        "flow_price_divergence": 0.0, "liq_slope_10": 0.01,
        "rsi": 55.0, "creator_sold_over_liq": 0.0,
        "liq_div_consecutive": 0.0, "liq_drawdown": -0.05,
        "ret_10": 0.03,
    }
    base.update(over)
    return base


class StubModel:
    """A model stub: decide_entry only calls .predict(features, band=...)."""

    def __init__(self, p, cred):
        self.p, self.cred = p, cred

    def predict(self, features, band="_all"):
        return {"p_raw": self.p, "p_calibrated": self.p, "credibility": self.cred}


def spec(**entry_over):
    e = EntrySpec(min_age_ms=100_000, min_bars=3, min_unique_buyers_10=2.0,
                  max_largest_buyer_share_10=0.6)
    for k, v in entry_over.items():
        setattr(e, k, v)
    return BotSpec(entry=e, bands=[BAND])


class TestRails(unittest.TestCase):
    def _d(self, f, s=None, m=None):
        return decide_entry(f, s or build_signals(f), m, spec(), BAND, COST,
                            100.0, 0)

    def test_missing_liquidity_rejects(self):
        d = self._d(feats(liquidity=None))
        self.assertEqual(d.action, "reject")
        self.assertEqual(d.reason, "missing_liquidity")

    def test_liquidity_below_band_floor(self):
        d = self._d(feats(liquidity=5.0))
        self.assertEqual((d.action, d.reason), ("reject", "liquidity_below_floor"))

    def test_young_token_rejected(self):
        d = self._d(feats(age_ms=10_000))
        self.assertEqual((d.action, d.reason), ("reject", "token_too_young"))

    def test_breadth_null_is_unmeasured_not_zero(self):
        d = self._d(feats(unique_buyers_10=None))
        self.assertEqual((d.action, d.reason), ("reject", "missing_buyer_breadth"))

    def test_single_wallet_dominates(self):
        d = self._d(feats(largest_buyer_share_10=0.9))
        self.assertEqual((d.action, d.reason),
                         ("reject", "single_wallet_dominates_buying"))


class TestGate(unittest.TestCase):
    def _d(self, f, s=None):
        return decide_entry(f, s or build_signals(f), None, spec(), BAND, COST,
                            100.0, 0)

    def test_negative_netflow_vetoes(self):
        d = self._d(feats(netflow_10_over_liq=-0.05))
        self.assertEqual((d.action, d.reason), ("reject", "gate_netflow_not_positive"))

    def test_missing_flow_fails_closed(self):
        d = self._d(feats(netflow_10_over_liq=None))
        self.assertEqual((d.action, d.reason), ("reject", "gate_flow_unmeasured"))

    def test_seller_breadth_dominates(self):
        d = self._d(feats(buyer_seller_breadth_10=-0.3))
        self.assertEqual((d.action, d.reason),
                         ("reject", "gate_seller_breadth_dominates"))

    def test_price_flow_divergence(self):
        # divergence veto is reachable when the positive-netflow requirement
        # is relaxed: price up while flow is negative
        d = decide_entry(feats(flow_price_divergence=1.0, ret_10=0.1,
                               netflow_10_over_liq=-0.1),
                         build_signals(feats()), None,
                         spec(gate_require_positive_netflow=False), BAND, COST,
                         100.0, 0)
        self.assertEqual((d.action, d.reason), ("reject", "gate_price_flow_divergence"))

    def test_liquidity_draining(self):
        d = self._d(feats(liq_slope_10=-0.2))
        self.assertEqual((d.action, d.reason), ("reject", "gate_liquidity_draining"))

    def test_overextended(self):
        d = self._d(feats(rsi=95.0))
        self.assertEqual((d.action, d.reason), ("reject", "gate_overextended"))

    def test_creator_dumping(self):
        d = self._d(feats(creator_sold_over_liq=0.3))
        self.assertEqual((d.action, d.reason), ("reject", "gate_creator_dumping"))

    def test_rsi_none_skips_veto(self):
        d = self._d(feats(rsi=None))
        self.assertIn(d.action, ("enter", "abstain"))
        self.assertNotEqual(d.reason, "gate_overextended")


class TestModelGate(unittest.TestCase):
    def test_no_model_flat_size_logged(self):
        d = decide_entry(feats(), build_signals(feats()), None, spec(), BAND,
                         COST, 100.0, 0)
        self.assertEqual(d.action, "enter")
        self.assertEqual(d.reason, "no_model_flat_size")
        self.assertGreater(d.size_sol, 0)

    def test_require_model_abstains(self):
        d = decide_entry(feats(), build_signals(feats()), None,
                         spec(require_model=True), BAND, COST, 100.0, 0)
        self.assertEqual((d.action, d.reason), ("abstain", "no_model_available"))

    def test_below_threshold_abstains(self):
        m = StubModel(0.30, 0.9)   # breakeven 0.2857 + margin 0.04 = 0.3257
        d = decide_entry(feats(), build_signals(feats()), m, spec(), BAND, COST,
                         100.0, 0)
        self.assertEqual((d.action, d.reason), ("abstain", "below_probability_threshold"))

    def test_low_credibility_abstains(self):
        m = StubModel(0.60, 0.05)
        d = decide_entry(feats(), build_signals(feats()), m, spec(), BAND, COST,
                         100.0, 0)
        self.assertEqual((d.action, d.reason), ("abstain", "out_of_distribution"))

    def test_conviction_sizing_and_depth_cap(self):
        m = StubModel(0.52, 0.9)
        d = decide_entry(feats(liquidity=100.0), build_signals(feats(liquidity=100.0)),
                         m, spec(), BAND, COST, 100.0, 0)
        self.assertEqual(d.action, "enter")
        # Kelly at p=0.52 -> 0.10*0.328*100=3.28 SOL; the max-fraction cap
        # would allow 2.0 SOL, but the pool-depth cap (100 SOL * 1% = 1.0
        # SOL) binds first on a thin pool
        self.assertAlmostEqual(d.size_sol, 1.0, places=6)
        self.assertEqual(d.size_cap_reason, "pool_depth")


class TestPurity(unittest.TestCase):
    def test_no_io_or_clock(self):
        src = Path(__import__("tape.bot.strategy", fromlist=["x"]).__file__).read_text()
        for forbidden in ("open(", "requests.", "httpx", "time.time",
                          "datetime.now", "random.", "input("):
            self.assertNotIn(forbidden, src)

    def test_same_inputs_same_decision(self):
        f = feats()
        a = decide_entry(f, build_signals(f), StubModel(0.5, 0.9), spec(), BAND,
                         COST, 100.0, 123456)
        b = decide_entry(f, build_signals(f), StubModel(0.5, 0.9), spec(), BAND,
                         COST, 100.0, 123456)
        self.assertEqual(a.to_row(), b.to_row())


if __name__ == "__main__":
    unittest.main()
