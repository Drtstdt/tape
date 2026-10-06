import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from tape.costs import (CostModel, constant_product_buy, constant_product_sell,
                        cost_floor_ok, partial_exit_round_trip_pct,
                        partial_exit_capital_recovered_pct, partial_exit_cost_floor_ok)
from tape.policy import Rails, ConfidencePolicy, decide, evaluate_rails, kelly_size


class TestCosts(unittest.TestCase):
    def test_fixed_cost_dominates_a_small_position(self):
        """The arithmetic that makes the venue change worth more than any
        parameter: at 0.006 SOL a 0.0001 SOL priority fee doubles the round
        trip; at 0.5 SOL it is a rounding error."""
        c = CostModel(fee_pct=0.01, fixed_cost_sol=0.0001)
        self.assertAlmostEqual(c.round_trip_pct(0.006), 0.02 + 2 * 0.0001 / 0.006, places=9)
        self.assertGreater(c.round_trip_pct(0.006), 0.05)
        self.assertLess(c.round_trip_pct(0.5), 0.021)

    def test_v3_cost_model_is_optimistic(self):
        no_fixed = CostModel(fee_pct=0.01, fixed_cost_sol=0.0)
        real = CostModel(fee_pct=0.01, fixed_cost_sol=0.0001)
        self.assertAlmostEqual(no_fixed.round_trip_pct(0.006), 0.02)
        self.assertGreater(real.round_trip_pct(0.006) - no_fixed.round_trip_pct(0.006), 0.03)

    def test_breakeven_win_rate_matches_the_measured_book(self):
        c = CostModel()
        self.assertAlmostEqual(c.breakeven_win_rate(52.4, -22.0), 0.2957, places=3)

    def test_amm_round_trip_loses_exactly_the_fees_plus_impact(self):
        q, b = 200.0, 2000.0
        base_out, _, _ = constant_product_buy(q, b, 1.0, 0.01)
        quote_back, _, _ = constant_product_sell(q + 1.0 * 0.99, b - base_out, base_out, 0.01)
        self.assertLess(quote_back, 1.0)
        self.assertGreater(quote_back, 0.95)

    def test_cost_floor_rejects_a_move_that_cannot_pay_for_itself(self):
        c = CostModel(fee_pct=0.01, fixed_cost_sol=0.0001)
        self.assertFalse(cost_floor_ok(0.006, 1.05, c))   # +5% on a 5%+ round trip
        self.assertTrue(cost_floor_ok(0.5, 1.8, c))


class TestPartialExit(unittest.TestCase):
    """D18: sell `exit_fraction` at `trigger_multiple`, ride the remainder to
    `final_multiple`. These pin down the two claims made in docs/DECISIONS.md
    D18: (1) 2.0x/0.5 recovers capital exactly and 1.8x/0.5 does not, and
    (2) the third fixed-cost leg is real and must be checked, not assumed
    away."""

    def test_2x_half_exit_recovers_capital_exactly_before_fees(self):
        self.assertAlmostEqual(partial_exit_capital_recovered_pct(2.0, 0.5), 1.0)

    def test_the_fitted_1_8_trigger_does_not_quite_recover_capital(self):
        """v3's ablation-fitted cost_recovery trigger, at an even split, is
        NOT the clean break-even point -- 1.8 * 0.5 = 0.9, ninety percent of
        capital, before fees eat further into it. Worth knowing before
        assuming the fitted value already behaves like the user's rule."""
        self.assertAlmostEqual(partial_exit_capital_recovered_pct(1.8, 0.5), 0.9)

    def test_third_leg_adds_real_cost_beyond_the_two_leg_round_trip(self):
        """The three-leg trade must cost strictly more than a plain two-leg
        round trip to the same trigger price -- there is a whole extra
        transaction's fixed cost in there. If this ever failed it would mean
        the accounting silently dropped a leg."""
        c = CostModel(fee_pct=0.01, fixed_cost_sol=0.0001)
        two_leg = c.round_trip_pct(0.006)          # entry + one exit at any price
        three_leg = partial_exit_round_trip_pct(0.006, 2.0, 0.5, c)
        self.assertGreater(three_leg, two_leg)

    def test_third_leg_fixed_cost_matters_more_at_small_size(self):
        """The whole reason this function exists: at v3's actual position
        size the extra fixed-cost leg is not a rounding error."""
        c = CostModel(fee_pct=0.01, fixed_cost_sol=0.0001)
        small = partial_exit_round_trip_pct(0.006, 2.0, 0.5, c)
        large = partial_exit_round_trip_pct(0.5, 2.0, 0.5, c)
        self.assertGreater(small, large)
        # at v3's size, three fixed legs alone are already a meaningful drag
        self.assertGreater(small, 3 * 0.0001 / 0.006)

    def test_partial_exit_matches_plain_round_trip_when_price_hasnt_moved(self):
        """Degenerate case: split an exit into two legs at the SAME price
        (trigger = final = 1.0), with no fixed cost. Proportional fees do not
        care how a sale is split, so this must come out identical to a plain
        two-leg round trip -- confirming the three-leg formula only diverges
        because of (a) a per-leg fixed cost and (b) the achieved price
        multiple, not because of an accounting error."""
        c = CostModel(fee_pct=0.01, fixed_cost_sol=0.0)
        three_leg = partial_exit_round_trip_pct(0.5, 1.0, 0.5, c, final_multiple=1.0)
        two_leg = c.round_trip_pct(0.5)
        self.assertAlmostEqual(three_leg, two_leg)

    def test_partial_exit_can_fail_its_own_floor_at_small_size(self):
        """At v3's actual position sizes (0.002-0.006 SOL), three fixed-cost
        legs can outright fail the same cost-floor discipline the rest of the
        system already enforces -- this is the number D18 was written to
        produce, not assume."""
        c = CostModel(fee_pct=0.01, fixed_cost_sol=0.0001)
        self.assertFalse(partial_exit_cost_floor_ok(0.002, 2.0, 0.5, c))
        self.assertTrue(partial_exit_cost_floor_ok(0.5, 2.0, 0.5, c))


class TestRails(unittest.TestCase):
    def base_signals(self, **over):
        s = dict(liquidity_usd=50_000.0, liquidity_sol=500.0, age_ms=300_000,
                 top_holder_pct=12.0, mint_authority_renounced=True,
                 freeze_authority_renounced=True, creator_blacklisted=False,
                 creator_rug_rate=0.1, attributed_trades=20, unique_buyers=9,
                 largest_buyer_share=0.2, expected_multiple=1.8)
        s.update(over)
        return s

    def test_clean_token_passes(self):
        self.assertIsNone(evaluate_rails(self.base_signals(), Rails(), CostModel(), 0.5))

    def test_unknown_is_not_safe(self):
        """Every null on a safety-critical field rejects. Trading on an
        unmeasured safety field is how you find out what it was."""
        for field in ("liquidity_usd", "age_ms", "top_holder_pct",
                      "mint_authority_renounced", "freeze_authority_renounced",
                      "unique_buyers", "largest_buyer_share"):
            r = evaluate_rails(self.base_signals(**{field: None}), Rails(), CostModel(), 0.5)
            self.assertIsNotNone(r, f"{field}=None must reject")

    def test_thin_pool_rejected_by_the_depth_rail_not_the_floor(self):
        s = self.base_signals(liquidity_usd=20_000.0, liquidity_sol=190.0)
        self.assertEqual(evaluate_rails(s, Rails(), CostModel(), 10.0)[0],
                         "position_too_large_vs_liquidity")

    def test_young_token_rejected(self):
        s = self.base_signals(age_ms=5_000)
        self.assertEqual(evaluate_rails(s, Rails(), CostModel(), 0.5)[0], "token_too_young")

    def test_single_whale_rejected(self):
        s = self.base_signals(largest_buyer_share=0.85)
        self.assertEqual(evaluate_rails(s, Rails(), CostModel(), 0.5)[0],
                         "single_wallet_dominates_buying")


class FakeModel:
    feature_names = ["x"]
    def __init__(self, p, cred): self.p, self.cred = p, cred
    def predict(self, features, band="_all"):
        return {"p_raw": self.p, "p_calibrated": self.p, "credibility": self.cred}


class TestDecide(unittest.TestCase):
    def sig(self):
        return dict(liquidity_usd=50_000.0, liquidity_sol=500.0, age_ms=300_000,
                    top_holder_pct=12.0, mint_authority_renounced=True,
                    freeze_authority_renounced=True, creator_blacklisted=False,
                    creator_rug_rate=0.1, attributed_trades=20, unique_buyers=9,
                    largest_buyer_share=0.2, expected_multiple=1.8)

    def test_abstains_when_out_of_distribution_even_with_a_high_score(self):
        """The whole point. An out-of-distribution prediction is a guess
        wearing a probability's clothes."""
        d = decide({}, self.sig(), FakeModel(0.90, 0.02), Rails(), ConfidencePolicy(),
                   CostModel(), capital_sol=10.0, now_ms=0)
        self.assertEqual(d.action, "abstain")
        self.assertEqual(d.reason, "out_of_distribution")

    def test_abstains_below_the_probability_threshold(self):
        d = decide({}, self.sig(), FakeModel(0.30, 0.8), Rails(), ConfidencePolicy(),
                   CostModel(), capital_sol=10.0, now_ms=0)
        self.assertEqual(d.action, "abstain")
        self.assertEqual(d.reason, "below_probability_threshold")

    def test_enters_and_sizes_by_conviction(self):
        conf = ConfidencePolicy()
        weak = decide({}, self.sig(), FakeModel(0.34, 0.8), Rails(), conf,
                      CostModel(), capital_sol=10.0, now_ms=0)
        strong = decide({}, self.sig(), FakeModel(0.42, 0.8), Rails(), conf,
                        CostModel(), capital_sol=10.0, now_ms=0)
        self.assertEqual(weak.action, "enter")
        self.assertEqual(strong.action, "enter")
        self.assertGreater(strong.size_sol, weak.size_sol)

    def test_the_cap_binds_for_strong_signals_and_that_is_deliberate(self):
        """The cap should bind only for genuinely strong signals. If it bound
        for EVERY p the model emits you would have flat sizing with extra
        steps -- which the default quarter-Kelly/1% pair actually did, and
        which this check caught."""
        from tape.policy import sizing_is_actually_varying
        conf = ConfidencePolicy()
        a = decide({}, self.sig(), FakeModel(0.50, 0.8), Rails(), conf,
                   CostModel(), capital_sol=10.0, now_ms=0)
        b = decide({}, self.sig(), FakeModel(0.75, 0.8), Rails(), conf,
                   CostModel(), capital_sol=10.0, now_ms=0)
        self.assertEqual(a.size_sol, b.size_sol)                    # both capped
        self.assertTrue(sizing_is_actually_varying(conf, 0.34, 0.42))
        self.assertFalse(sizing_is_actually_varying(conf, 0.50, 0.75))

    def test_rails_cannot_be_overruled_by_a_confident_model(self):
        s = self.sig(); s["mint_authority_renounced"] = False
        d = decide({}, s, FakeModel(0.99, 0.99), Rails(), ConfidencePolicy(),
                   CostModel(), capital_sol=10.0, now_ms=0)
        self.assertEqual(d.action, "reject")
        self.assertEqual(d.layer, "rail")

    def test_no_model_falls_back_flat_and_says_so(self):
        d = decide({}, self.sig(), None, Rails(), ConfidencePolicy(),
                   CostModel(), capital_sol=10.0, now_ms=0)
        self.assertEqual(d.action, "enter")
        self.assertEqual(d.reason, "no_model_flat_size")

    def test_kelly_is_zero_below_breakeven(self):
        self.assertEqual(kelly_size(0.20, 2.38, 0.25, 10.0, 0.01), 0.0)

    def test_decide_is_pure(self):
        s, m, conf = self.sig(), FakeModel(0.55, 0.8), ConfidencePolicy()
        a = decide({}, s, m, Rails(), conf, CostModel(), 10.0, 0)
        b = decide({}, s, m, Rails(), conf, CostModel(), 10.0, 0)
        self.assertEqual(a.to_row(), b.to_row())


if __name__ == "__main__":
    unittest.main()
