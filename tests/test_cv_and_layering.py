import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from pathlib import Path
import numpy as np
from tape.cv import purged_group_time_series_split, token_bootstrap


class TestPurgedCV(unittest.TestCase):
    def setup_rows(self, n_tokens=20, per=25, span=1000):
        t0, t1, g = [], [], []
        for k in range(n_tokens):
            base = k * span
            for i in range(per):
                t0.append(base + i * 10)
                t1.append(base + i * 10 + 50)    # labels overlap each other
                g.append(f"mint{k}")
        return np.array(t0), np.array(t1), np.array(g)

    def test_no_mint_appears_in_both_train_and_test(self):
        """Two bars of the same token are near-duplicates. A split that keeps
        bar 400 in train and 401 in test is leakage wearing a score."""
        t0, t1, g = self.setup_rows()
        folds = list(purged_group_time_series_split(t0, t1, g, n_splits=4))
        self.assertGreater(len(folds), 0)
        for tr, te in folds:
            self.assertEqual(set(g[tr]) & set(g[te]), set())

    def test_training_labels_never_overlap_the_test_window(self):
        t0, t1, g = self.setup_rows()
        for tr, te in purged_group_time_series_split(t0, t1, g, n_splits=4):
            lo, hi = t0[te].min(), t1[te].max()
            self.assertFalse(((t1[tr] >= lo) & (t0[tr] <= hi)).any())

    def test_every_fold_has_data(self):
        t0, t1, g = self.setup_rows()
        for tr, te in purged_group_time_series_split(t0, t1, g, n_splits=4):
            self.assertGreater(len(tr), 0)
            self.assertGreater(len(te), 0)


class TestBootstrap(unittest.TestCase):
    def test_resampling_tokens_is_wider_than_resampling_rows(self):
        """The effective sample size is the number of MINTS. A row bootstrap on
        3,000 near-identical bars of one token reports an interval that is
        wrong by orders of magnitude."""
        rng = np.random.default_rng(0)
        vals, groups = [], []
        for k in range(12):
            level = rng.normal(0, 1.0)
            for _ in range(200):
                vals.append(level + rng.normal(0, 0.01))
                groups.append(f"m{k}")
        vals = np.array(vals)
        lo_g, hi_g = token_bootstrap(vals, groups, n_boot=400, seed=1)
        row_groups = [str(i) for i in range(len(vals))]
        lo_r, hi_r = token_bootstrap(vals, row_groups, n_boot=400, seed=1)
        self.assertGreater(hi_g - lo_g, (hi_r - lo_r) * 3)


class TestLayering(unittest.TestCase):
    def test_labels_cannot_see_features(self):
        """The separation is the only reliable defence: a leak does not raise
        and does not look wrong in a diff."""
        import tape.labels as labels
        src = Path(labels.__file__).read_text()
        self.assertNotIn("from .features", src)
        self.assertNotIn("import features", src)

    def test_features_cannot_see_labels(self):
        import tape.features as features
        src = Path(features.__file__).read_text()
        self.assertNotIn("from .labels", src)
        self.assertNotIn("import labels", src)

    def test_policy_does_no_io(self):
        """decide() must be pure: same inputs, same answer, on any machine."""
        import tape.policy as policy
        src = Path(policy.__file__).read_text()
        for forbidden in ("open(", "requests.", "time.time", "datetime.now", "random."):
            self.assertNotIn(forbidden, src, f"policy.py must not contain {forbidden}")

    def test_every_dataclass_field_referenced_in_policy_exists(self):
        """The Python equivalent of v3's importIntegrity test -- the check that
        would have caught the decodeTradeEvent/decodeTradeEvents bug that
        silently voided an entire live run."""
        from tape.schema import Decision
        from tape.policy import decide, Rails, ConfidencePolicy
        from tape.costs import CostModel
        sig = dict(liquidity_usd=50_000.0, liquidity_sol=500.0, age_ms=300_000,
                   top_holder_pct=12.0, mint_authority_renounced=True,
                   freeze_authority_renounced=True, creator_blacklisted=False,
                   creator_rug_rate=0.1, attributed_trades=20, unique_buyers=9,
                   largest_buyer_share=0.2, expected_multiple=1.8)
        d = decide({}, sig, None, Rails(), ConfidencePolicy(), CostModel(), 10.0, 0)
        self.assertIsInstance(d, Decision)
        self.assertIn("size_sol", d.to_row())


if __name__ == "__main__":
    unittest.main()
