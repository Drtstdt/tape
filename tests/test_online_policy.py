"""Tests for tape/online_policy.py (D84, docs/DECISIONS.md) -- the
online-learning entry policy for paper trading.

Style matches tests/test_features.py's D71/D82 additions: hand-built
synthetic fixtures where the right answer is known by construction, so a
test failure means the MECHANISM is wrong, not that noisy real data
disagreed with a hope.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import math
import tempfile
import unittest
from pathlib import Path

from tape.online_policy import OnlinePolicy, PaperRails, evaluate_paper_rails


def make_policy(**kwargs):
    defaults = dict(feature_names=["f1", "f2"], seed=0)
    defaults.update(kwargs)
    return OnlinePolicy(**defaults)


class TestBreakevenP(unittest.TestCase):
    def test_derived_from_barrier_config_directly(self):
        pol = make_policy(upper_multiple=1.6, lower_pct=-0.30, probability_margin=0.0)
        # p*(0.6) + (1-p)*(-0.3) == 0  ->  p == 0.3/0.9 == 1/3
        self.assertAlmostEqual(pol.breakeven_p, 1.0 / 3.0)
        self.assertAlmostEqual(pol.min_probability, 1.0 / 3.0)

    def test_probability_margin_is_added_on_top(self):
        pol = make_policy(upper_multiple=1.6, lower_pct=-0.30, probability_margin=0.04)
        self.assertAlmostEqual(pol.min_probability, 1.0 / 3.0 + 0.04)

    def test_symmetric_payoff_gives_50_50_breakeven(self):
        pol = make_policy(upper_multiple=2.0, lower_pct=-1.0, probability_margin=0.0)
        # gain=1.0, loss=1.0 -> breakeven 0.5
        self.assertAlmostEqual(pol.breakeven_p, 0.5)


class TestEpsilonSchedule(unittest.TestCase):
    def test_starts_at_epsilon_start(self):
        pol = make_policy(epsilon_start=0.9, epsilon_floor=0.15, epsilon_decay_scale=60.0)
        self.assertAlmostEqual(pol.epsilon, 0.9)

    def test_decays_toward_floor_and_never_below_it(self):
        pol = make_policy(epsilon_start=0.9, epsilon_floor=0.15, epsilon_decay_scale=60.0)
        prev = pol.epsilon
        for _ in range(500):
            pol.decide({"f1": 1.0, "f2": 1.0})
            cur = pol.epsilon
            self.assertLessEqual(cur, prev + 1e-12)
            self.assertGreaterEqual(cur, pol.epsilon_floor - 1e-12)
            prev = cur
        # after many decisions it should be close to (not below) the floor
        self.assertAlmostEqual(pol.epsilon, pol.epsilon_floor, delta=0.05)


class TestFeatureVector(unittest.TestCase):
    def test_missing_feature_contributes_zero_to_dot_product(self):
        pol = make_policy(epsilon_start=0.0, epsilon_floor=0.0)
        pol.weights["f1"] = 5.0  # would matter a lot if f1 were treated as raw 0
        pol.weights["f2"] = 5.0
        x = pol._vector({"f2": 1.0})  # f1 entirely missing
        self.assertEqual(x[0], 0.0)  # f1 -> exactly neutral, not a punished "0"

    def test_decide_does_not_crash_with_all_features_missing(self):
        pol = make_policy(epsilon_start=0.0, epsilon_floor=0.0)
        out = pol.decide({})
        self.assertIn(out["action"], ("enter", "abstain"))
        self.assertAlmostEqual(out["p_raw"], 0.5)  # bias=0, all-zero vector -> sigmoid(0)


class TestExploreExploit(unittest.TestCase):
    def test_epsilon_one_always_explores(self):
        pol = make_policy(epsilon_start=1.0, epsilon_floor=1.0)
        for _ in range(50):
            out = pol.decide({"f1": 1.0, "f2": -1.0})
            self.assertTrue(out["explore"])

    def test_epsilon_zero_never_explores_and_thresholds_on_min_probability(self):
        pol = make_policy(epsilon_start=0.0, epsilon_floor=0.0, probability_margin=0.0,
                          upper_multiple=2.0, lower_pct=-1.0)  # breakeven/min_prob = 0.5
        # Force a known belief: bias very positive -> p_raw > 0.5 -> enter
        pol.bias = 10.0
        out = pol.decide({"f1": 0.0, "f2": 0.0})
        self.assertFalse(out["explore"])
        self.assertEqual(out["action"], "enter")
        self.assertGreater(out["p_raw"], pol.min_probability)

        pol2 = make_policy(epsilon_start=0.0, epsilon_floor=0.0, probability_margin=0.0,
                           upper_multiple=2.0, lower_pct=-1.0)
        pol2.bias = -10.0
        out2 = pol2.decide({"f1": 0.0, "f2": 0.0})
        self.assertFalse(out2["explore"])
        self.assertEqual(out2["action"], "abstain")


class TestUpdate(unittest.TestCase):
    def test_gradient_direction_matches_hand_computed_value(self):
        pol = make_policy(learning_rate=0.1, epsilon_start=0.0, epsilon_floor=0.0)
        # Seed running stats so f1's standardized value is exactly 1.0:
        # feed two observations (0.0, 2.0) -> mean=1.0, std=sqrt(2)... instead,
        # directly control by monkeypatching stats to a known, simple state.
        pol._stats["f1"].n = 2
        pol._stats["f1"].mean = 0.0
        pol._stats["f1"].m2 = 1.0  # std = sqrt(1/(2-1)) = 1.0
        pol._stats["f2"].n = 2
        pol._stats["f2"].mean = 0.0
        pol._stats["f2"].m2 = 1.0
        # x = [(1-0)/1, (0-0)/1] = [1.0, 0.0]; weights/bias start at 0 -> p=0.5
        features = {"f1": 1.0, "f2": 0.0}
        pol.update(features, y=1)
        # grad = p - y = 0.5 - 1 = -0.5; weight -= lr*grad*x -> w1 -= 0.1*(-0.5)*1.0 = +0.05
        self.assertAlmostEqual(pol.weights["f1"], 0.05)
        self.assertAlmostEqual(pol.weights["f2"], 0.0)   # x2 was 0 -> untouched
        self.assertAlmostEqual(pol.bias, 0.05)            # bias -= lr*grad = -0.1*(-0.5)=0.05
        self.assertEqual(pol.n_updates, 1)

    def test_update_rejects_non_binary_y(self):
        pol = make_policy()
        with self.assertRaises(ValueError):
            pol.update({"f1": 1.0}, y=2)

    def test_repeated_updates_on_a_genuinely_predictive_feature_learn_the_right_sign(self):
        """The mechanism test: f1 perfectly predicts y (f1>0 -> y=1, f1<0 -> y=0).
        After many full-feedback updates (epsilon forced to 0 so decide()
        reads pure belief), the learned weight on f1 must be positive and
        decide() must recover the correct action on both cases."""
        pol = make_policy(feature_names=["f1", "f2"], learning_rate=0.5,
                          epsilon_start=0.0, epsilon_floor=0.0,
                          upper_multiple=2.0, lower_pct=-1.0)  # min_probability=0.5
        rows = [({"f1": 3.0, "f2": 0.0}, 1), ({"f1": -3.0, "f2": 0.0}, 0)] * 200
        for feats, y in rows:
            pol.decide(feats)          # advances running stats (also fine to skip)
            pol.update(feats, y)
        self.assertGreater(pol.weights["f1"], 0.0)
        pos_decision = pol.decide({"f1": 3.0, "f2": 0.0})
        neg_decision = pol.decide({"f1": -3.0, "f2": 0.0})
        self.assertEqual(pos_decision["action"], "enter")
        self.assertEqual(neg_decision["action"], "abstain")
        self.assertGreater(pos_decision["p_raw"], neg_decision["p_raw"])


class TestPersistence(unittest.TestCase):
    def test_save_load_round_trip_preserves_state(self):
        pol = make_policy(learning_rate=0.3, epsilon_start=0.8, epsilon_floor=0.2)
        for feats, y in [({"f1": 1.0, "f2": 2.0}, 1), ({"f1": -1.0, "f2": 0.5}, 0)]:
            pol.decide(feats)
            pol.update(feats, y)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "policy.json"
            pol.save(path)
            loaded = OnlinePolicy.load(path)
        self.assertEqual(loaded.weights, pol.weights)
        self.assertEqual(loaded.bias, pol.bias)
        self.assertEqual(loaded.n_decisions, pol.n_decisions)
        self.assertEqual(loaded.n_updates, pol.n_updates)
        self.assertEqual(loaded.feature_names, pol.feature_names)
        for name in pol.feature_names:
            self.assertEqual(loaded._stats[name].to_dict(), pol._stats[name].to_dict())

    def test_loaded_policy_produces_identical_decide_output_given_same_rng_draw(self):
        pol = make_policy(epsilon_start=0.0, epsilon_floor=0.0)
        pol.weights["f1"] = 2.0
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "policy.json"
            pol.save(path)
            loaded = OnlinePolicy.load(path)
        feats = {"f1": 1.0, "f2": 1.0}
        # epsilon=0 -> deterministic regardless of RNG state, so this must match exactly
        self.assertEqual(pol.decide(feats)["action"], loaded.decide(feats)["action"])


class TestPaperRails(unittest.TestCase):
    def base_features(self):
        return {"n_bars": 5.0, "age_ms": 10_000.0, "liquidity": 50.0,
                "unique_buyers_10": 4.0, "largest_buyer_share_10": 0.3}

    def test_passes_with_healthy_features(self):
        self.assertIsNone(evaluate_paper_rails(self.base_features(), PaperRails()))

    def test_rejects_too_few_bars(self):
        f = self.base_features(); f["n_bars"] = 1.0
        reason, layer = evaluate_paper_rails(f, PaperRails())
        self.assertEqual(reason, "not_enough_bars")

    def test_rejects_missing_age_not_zero(self):
        f = self.base_features(); del f["age_ms"]
        reason, layer = evaluate_paper_rails(f, PaperRails())
        self.assertEqual(reason, "missing_age")

    def test_rejects_too_young(self):
        f = self.base_features(); f["age_ms"] = 100.0
        reason, layer = evaluate_paper_rails(f, PaperRails())
        self.assertEqual(reason, "token_too_young")

    def test_missing_liquidity_does_not_reject(self):
        """D86 (docs/DECISIONS.md): D85's real rejection-reason tally found
        `liquidity` unmeasured for 65/65 real tokens (this pipeline's Helius
        source never populates `quote_reserve_after`), so this check was
        loosened to match `largest_buyer_share_10`'s existing pattern --
        enforce the floor only when a real reading exists."""
        f = self.base_features(); del f["liquidity"]
        self.assertIsNone(evaluate_paper_rails(f, PaperRails()))

    def test_rejects_liquidity_below_floor_when_measured(self):
        f = self.base_features(); f["liquidity"] = -1.0
        reason, layer = evaluate_paper_rails(f, PaperRails(min_liquidity=0.0))
        self.assertEqual(reason, "liquidity_below_floor")

    def test_rejects_too_few_unique_buyers(self):
        f = self.base_features(); f["unique_buyers_10"] = 0.0
        reason, layer = evaluate_paper_rails(f, PaperRails())
        self.assertEqual(reason, "too_few_unique_buyers")

    def test_rejects_single_wallet_dominance(self):
        f = self.base_features(); f["largest_buyer_share_10"] = 0.95
        reason, layer = evaluate_paper_rails(f, PaperRails())
        self.assertEqual(reason, "single_wallet_dominates_buying")

    def test_missing_largest_buyer_share_does_not_reject(self):
        """This field (like `liquidity` as of D86) is allowed to be
        genuinely unmeasured without rejecting -- matches
        evaluate_paper_rails's own `if lbs is not None and ...` (breadth
        attribution can be partial without meaning something is unsafe,
        unlike a missing age, which still hard-rejects)."""
        f = self.base_features(); del f["largest_buyer_share_10"]
        self.assertIsNone(evaluate_paper_rails(f, PaperRails()))


if __name__ == "__main__":
    unittest.main()
