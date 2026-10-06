"""Tests for scripts/information_audit.py -- the Stage 1 gate.

`scripts/` is a standalone-script directory, not an importable package (no
__init__.py, same as backfill_bitquery.py etc.), so the module under test is
loaded by file path with importlib rather than restructuring the project
around these tests.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tape.labels import BarrierConfig, Label, triple_barrier, UP, DOWN  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "information_audit", ROOT / "scripts" / "information_audit.py")
ia = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ia)


CFG = BarrierConfig(upper_multiple=1.5, lower_pct=-0.30, horizon_ms=5_000)


# ---------------------------------------------------------------------------
# A. Same-bar leakage: bar i's OWN high/low must never affect bar i's label.
# ---------------------------------------------------------------------------

class TestSameBarLeakage(unittest.TestCase):
    def test_bar_i_own_high_low_does_not_change_its_label(self):
        h = [1.0, 1.0, 1.05]
        l = [1.0, 1.0, 0.95]
        c = [1.0, 1.0, 1.0]
        t = [0, 1000, 2000]
        normal = triple_barrier(h, l, c, t, 0, CFG)

        # Blow bar 0's OWN high/low out to values that would trivially hit
        # either barrier if `triple_barrier` ever looked at index i itself.
        h_leaky = [999.0, 1.0, 1.05]
        l_leaky = [0.0001, 1.0, 0.95]
        leaky = triple_barrier(h_leaky, l_leaky, c, t, 0, CFG)

        self.assertEqual(normal.outcome, leaky.outcome,
                          "bar 0's own high/low changed its own label -- same-bar leak")
        self.assertEqual(normal.y, leaky.y)

    def test_build_one_pairs_bar_i_features_with_a_label_that_only_reads_forward(self):
        """Confirms the pairing `build_one` actually uses: `feats[lab.bar_index]`
        is state as of bar i closing; `lab` is computed from `range(i+1, n)`
        only (tape/labels.py). Perturbing bar i's own high/low must not move
        `lab.outcome`, mirroring the check above at the call the script makes."""
        h = [1.0, 1.0, 1.0, 1.6, 1.0]
        l = [1.0, 1.0, 1.0, 0.9, 1.0]
        c = [1.0, 1.0, 1.0, 1.0, 1.0]
        t = [0, 1000, 2000, 3000, 4000]
        i = 2
        lab = triple_barrier(h, l, c, t, i, CFG)
        h2, l2 = list(h), list(l)
        h2[i], l2[i] = 50.0, 0.0001
        lab2 = triple_barrier(h2, l2, c, t, i, CFG)
        self.assertEqual(lab.outcome, lab2.outcome)


# ---------------------------------------------------------------------------
# B. Chronological token split: every final_test token's first-seen time is
# strictly after every screen/validation token's.
# ---------------------------------------------------------------------------

class TestChronologicalSplit(unittest.TestCase):
    def test_split_is_chronological_not_alphabetical(self):
        # Deliberately alphabetically REVERSED vs. chronological order, so a
        # split that (like the old script) rode on alphabetical order would
        # fail this immediately.
        mints_sorted_by_time = [f"zzz{i}" if i % 2 else f"aaa{i}" for i in range(30)]
        screen, val, test = ia.split_tokens(mints_sorted_by_time, 0.5, 0.2, 0.3)
        self.assertEqual(screen + val + test, mints_sorted_by_time)
        # positions, not alphabetical order, define membership
        pos = {m: i for i, m in enumerate(mints_sorted_by_time)}
        self.assertLess(max(pos[m] for m in screen), min(pos[m] for m in val))
        self.assertLess(max(pos[m] for m in val), min(pos[m] for m in test))

    def test_fractions_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            ia.split_tokens(["a", "b", "c"], 0.5, 0.5, 0.5)


# ---------------------------------------------------------------------------
# C. Token bootstrap resamples TOKENS, not rows.
# ---------------------------------------------------------------------------

class TestAucClusterBootstrapResamplesTokens(unittest.TestCase):
    def test_row_level_groups_understate_the_interval(self):
        """Each token gets ONE random level; every row's label is a Bernoulli
        draw whose probability depends only on that shared level. AUC's real
        uncertainty is therefore mostly about which token-levels got drawn
        (12 of them), not about the ~1800 individual coin flips. A bootstrap
        that resamples rows as if they were independent averages the coin
        flips down to nothing and reports a CI far narrower than the truth;
        resampling tokens keeps each token's whole correlated block intact."""
        rng = np.random.default_rng(0)
        scores, y, groups = [], [], []
        for k in range(12):
            level = rng.normal(0, 1.2)
            p = 1.0 / (1.0 + np.exp(-level))
            for _ in range(150):
                scores.append(level + rng.normal(0, 0.3))
                y.append(1 if rng.random() < p else 0)
                groups.append(f"m{k}")
        scores, y = np.array(scores), np.array(y)
        lo_g, hi_g = ia.auc_cluster_bootstrap(scores, y, groups, ia.auc, n_boot=500, seed=1)
        row_groups = [str(i) for i in range(len(scores))]
        lo_r, hi_r = ia.auc_cluster_bootstrap(scores, y, row_groups, ia.auc, n_boot=500, seed=1)
        self.assertGreater(hi_g - lo_g, (hi_r - lo_r) * 2)


# ---------------------------------------------------------------------------
# D. The CI is actually computed FROM AUC, not silently from a hit rate.
# ---------------------------------------------------------------------------

class TestCIComesFromAUC(unittest.TestCase):
    def test_ci_brackets_the_true_auc_not_the_base_rate(self):
        rng = np.random.default_rng(3)
        scores, y, groups = [], [], []
        base_rate = 0.5
        for k in range(20):
            for _ in range(80):
                true_signal = rng.normal(0, 1.0)
                yy = 1 if true_signal > 0 else 0   # strong true signal, AUC should be high
                scores.append(true_signal)
                y.append(yy)
                groups.append(f"m{k}")
        scores, y = np.array(scores), np.array(y)
        point = ia.auc(scores, y)
        lo, hi = ia.auc_cluster_bootstrap(scores, y, groups, ia.auc, n_boot=500, seed=2)
        self.assertGreater(point, 0.9)
        self.assertLess(lo, point + 1e-9)
        self.assertGreater(hi, point - 1e-9)
        # The base rate (0.5) must NOT be what the interval is centred on --
        # that would indicate the old hit-rate-bootstrap bug.
        self.assertGreater(lo, base_rate + 0.2)


# ---------------------------------------------------------------------------
# E / F. Constant feature -> AUC 0.5. Inverse feature -> AUC < 0.5, and the
# audit reports it as "inverse", not silently sign-flipped.
# ---------------------------------------------------------------------------

class TestAucEdgeCases(unittest.TestCase):
    def test_constant_feature_is_exactly_half(self):
        y = np.array([0, 1, 0, 1, 1, 0, 1, 0])
        scores = np.full(len(y), 7.0)
        self.assertAlmostEqual(ia.auc(scores, y), 0.5)

    def test_inverse_feature_scores_below_half_and_is_labelled_inverse(self):
        rng = np.random.default_rng(5)
        true_signal = rng.normal(size=400)
        y = (true_signal > 0).astype(float)
        a_pos = ia.auc(true_signal, y)
        a_inv = ia.auc(-true_signal, y)
        self.assertGreater(a_pos, 0.5)
        self.assertLess(a_inv, 0.5)
        self.assertAlmostEqual(a_pos + a_inv, 1.0, places=6)
        self.assertEqual(ia.direction_of(a_inv), "inverse")
        self.assertEqual(ia.direction_of(a_pos), "positive")


# ---------------------------------------------------------------------------
# G. A synthetic null (many random features, no real relationship) must NOT
# routinely clear the gate just because the max random AUC exceeds 0.55.
# ---------------------------------------------------------------------------

class TestNullDatasetDoesNotRoutinelyPassTheGate(unittest.TestCase):
    def test_permutation_p_value_is_not_significant_for_pure_noise(self):
        rng = np.random.default_rng(11)
        n_tokens, per_token, n_features = 24, 60, 40
        rows, y, groups = [], [], []
        for k in range(n_tokens):
            for _ in range(per_token):
                rows.append({f"f{j}": rng.normal() for j in range(n_features)})
                y.append(rng.integers(0, 2))
                groups.append(f"m{k}")
        y = np.array(y, dtype=float)
        groups = np.array(groups)
        names = [f"f{j}" for j in range(n_features)]

        results = ia.feature_table(rows, y, groups, np.ones(len(y), dtype=bool), names)
        best_edge = results[0][2]
        # With 40 independent random features, SOME will clear a fixed AUC
        # 0.55 bar (edge 0.05) by chance -- that is exactly the failure mode
        # being guarded against, so this is not itself an assertion.
        null = ia.permutation_null_max_edge(rows, y, groups, names, n_perm=150, seed=1)
        p_value = float((1 + (null >= best_edge).sum()) / (len(null) + 1))
        self.assertGreater(p_value, 0.05,
                            "pure noise should not look significant against its own "
                            "run's multiple-testing null")


# ---------------------------------------------------------------------------
# H. Real-creation-time filter (D55): old survivor tokens rediscovered by
# Bitquery's `discover()` must not be silently counted as new launches, and
# a missing/failed lookup must not be silently treated as "young enough".
# ---------------------------------------------------------------------------

class TestRealCreationFilter(unittest.TestCase):
    def test_old_survivor_is_excluded(self):
        eligible = [("young", 1_000_000), ("old_survivor", 2_000_000)]
        cache = {
            "young": {"status": "ok", "real_created_ts_ms": 1_000_000 - 3600_000},   # 1h old
            "old_survivor": {"status": "ok", "real_created_ts_ms": 2_000_000 - 999_000_000_000},
        }
        kept, too_old, no_data = ia.filter_by_real_creation(eligible, cache, max_observed_age_hours=6.0)
        self.assertEqual([m for m, _ in kept], ["young"])
        self.assertEqual(too_old, 1)
        self.assertEqual(no_data, 0)

    def test_missing_cache_entry_is_excluded_not_assumed_young(self):
        eligible = [("unknown_mint", 1_000_000)]
        kept, too_old, no_data = ia.filter_by_real_creation(eligible, {}, max_observed_age_hours=6.0)
        self.assertEqual(kept, [])
        self.assertEqual(too_old, 0)
        self.assertEqual(no_data, 1)

    def test_error_status_is_excluded_not_assumed_young(self):
        eligible = [("m", 1_000_000)]
        cache = {"m": {"status": "error", "real_created_ts_ms": None}}
        kept, too_old, no_data = ia.filter_by_real_creation(eligible, cache, max_observed_age_hours=6.0)
        self.assertEqual(kept, [])
        self.assertEqual(no_data, 1)

    def test_exactly_at_the_threshold_is_kept(self):
        eligible = [("m", 21_600_000)]   # 6h in ms
        cache = {"m": {"status": "ok", "real_created_ts_ms": 0}}
        kept, too_old, no_data = ia.filter_by_real_creation(eligible, cache, max_observed_age_hours=6.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(too_old, 0)


if __name__ == "__main__":
    unittest.main()
