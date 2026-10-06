"""D127: trial statistics + family A runner guards."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tape import trials as tr  # noqa: E402


class TestStats(unittest.TestCase):
    def test_spearman(self):
        x = np.arange(100.0)
        self.assertAlmostEqual(tr.spearman(x, x ** 3), 1.0)
        self.assertAlmostEqual(tr.spearman(x, -x), -1.0)

    def test_blocks_equal_count_chronological(self):
        ts = np.array([5, 1, 4, 2, 3, 6])
        b = tr.block_ids(ts, 3)
        self.assertEqual(list(b), [2, 0, 1, 0, 1, 2])

    def test_null_is_calibrated_and_detects_signal(self):
        rng = np.random.default_rng(0)
        n = 4000
        y = rng.normal(size=n)
        feats = {"noise": rng.normal(size=n), "signal": y * 0.2 + rng.normal(size=n)}
        blocks = tr.block_ids(np.arange(n), 6)
        null = tr.permutation_null(feats, y, blocks, 300, seed=1)
        rho_noise = abs(tr.spearman(feats["noise"], y))
        rho_sig = abs(tr.spearman(feats["signal"], y))
        p_noise = (1 + np.sum(null["noise"] >= rho_noise)) / 301
        p_sig = (1 + np.sum(null["signal"] >= rho_sig)) / 301
        self.assertGreater(p_noise, 0.01)
        self.assertLess(p_sig, 0.01)

    def test_rule_uses_first_half_only(self):
        rng = np.random.default_rng(2)
        n = 2000
        x = rng.normal(size=n)
        y = x + rng.normal(size=n)
        first = np.arange(n) < n // 2
        r1 = tr.quintile_rule(x, y, first)
        x2 = x.copy()
        x2[~first] *= 100                      # second-half feature values do not move the threshold
        r2 = tr.quintile_rule(x2, y, first)
        self.assertEqual(r1["threshold"], r2["threshold"])
        self.assertEqual(r1["direction"], 1.0)
        y3 = y.copy()
        y3[first] = -x[first]                  # flipping the first-half relation flips the direction
        self.assertEqual(tr.quintile_rule(x, y3, first)["direction"], -1.0)

    def test_bootstrap_ci_contains_truth(self):
        rng = np.random.default_rng(3)
        y = rng.normal(0.1, 1, 3000)
        sel = rng.random(3000) < 0.2
        day = np.arange(3000) // 50
        b = tr.day_block_bootstrap(y, sel, day, 500)
        self.assertLess(b["sel_mean_ci"][0], 0.1)
        self.assertGreater(b["sel_mean_ci"][1], 0.1)


class TestRunner(unittest.TestCase):
    def test_end_to_end_and_no_rerun(self):
        spec = importlib.util.spec_from_file_location("rfa", os.path.join(ROOT, "scripts", "run_family_A.py"))
        rfa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rfa)
        rng = np.random.default_rng(4)
        n = 3000
        rows = []
        for k in (5, 10, 20):
            for i in range(n):
                r = {"mint": f"M{i}", "K": k, "create_ts": 1_770_000_000_000 + i * 600_000,
                     "decision_ts": 0, "is_mayhem": float(i % 7 == 0), "chk_curve_fitted": i % 7 == 0,
                     "chk_chain_match": 1.0, "outcome_usable": True,
                     "o_base_status": ["TP", "SL", "TIME", "CENS"][i % 4]}
                for f in rfa.FEATURES:
                    r[f] = rng.normal()
                r["o_base_net"] = 0.3 * r["swaps_per_min"] + rng.normal() if r["o_base_status"] != "CENS" else np.nan
                rows.append(r)
        df = pd.DataFrame(rows)
        rfa.load_universe = lambda d, ks, log: df[df["K"].isin(ks)]
        rfa.N_PERM = 200
        orig_load_zone = rfa.zn.load_zone            # shared module: restore afterwards
        self.addCleanup(setattr, rfa.zn, "load_zone", orig_load_zone)
        with tempfile.TemporaryDirectory() as d:
            rfa.zn.load_zone = lambda z, name: [f"M{i}" for i in range(n)]
            argv = ["x", "--registry", str(Path(d) / "reg"), "--out", str(Path(d) / "out")]
            sys.argv = argv
            self.assertEqual(rfa.main(), 0)
            from tape.registry import Registry
            t = Registry(Path(d) / "reg").trials()
            st = {v["name"]: v["status"] for v in t.values() if v["family"] == "A"}
            self.assertEqual(st["A swaps_per_min @K=10"], "passed_discovery")
            self.assertEqual(len(st), 7)
            sys.argv = argv
            self.assertEqual(rfa.main(), 2)       # no re-rolls
            # D133 replication: same specs on a corrected set, no new trials
            df.loc[df.index[:30], "o_base_net"] += 0.01
            rargv = ["x", "--registry", str(Path(d) / "reg"), "--out", str(Path(d) / "out_v3"),
                     "--sets", str(Path(d) / "sets_v3" / "discovery"), "--replicate"]
            sys.argv = rargv
            self.assertEqual(rfa.main(), 0)
            reg = Registry(Path(d) / "reg")
            t = reg.trials()
            self.assertEqual(sum(1 for v in t.values() if v["family"] == "A"), 7)
            diag = [v for v in t.values() if v["name"] == "A replication on sets_v3 (D133)"]
            self.assertEqual(len(diag), 1)
            a1 = [v for v in t.values() if v["name"] == "A swaps_per_min @K=10"][0]
            self.assertEqual(a1["status"], "passed_discovery")
            self.assertEqual(a1["replication_v3"]["diagnostic"], diag[0]["trial_id"])
            res = json.loads((Path(d) / "out_v3" / "results.json").read_text(encoding="utf-8"))
            self.assertTrue(res["replication"])
            self.assertEqual(res["set"], "sets_v3")
            self.assertIn("threshold", res["results"]["q_gain_sol"])
            sys.argv = rargv
            self.assertEqual(rfa.main(), 2)       # replicated once


if __name__ == "__main__":
    unittest.main()


class TestPartial(unittest.TestCase):
    def test_partial_removes_proxy(self):
        rng = np.random.default_rng(10)
        n = 5000
        c = rng.normal(size=n)
        proxy = c + 0.3 * rng.normal(size=n)
        y = c + rng.normal(size=n)
        self.assertGreater(abs(tr.spearman(proxy, y)), 0.4)
        self.assertLess(abs(tr.partial_spearman(proxy, y, c)), 0.05)

    def test_partial_null_calibrated_and_powerful(self):
        rng = np.random.default_rng(11)
        n = 6000
        c = rng.normal(size=n)
        sig = rng.normal(size=n)
        y = c + 0.25 * sig + rng.normal(size=n)
        feats = {"proxy": c + 0.3 * rng.normal(size=n), "signal": sig.copy()}
        feats["signal"][::3] = np.nan                # own rows per feature
        blocks = tr.block_ids(np.arange(n), 6)
        null = tr.partial_permutation_null(feats, y, c, blocks, 300, seed=1)
        p = {k: (1 + np.sum(null[k] >= abs(null["_obs"][k]))) / 301 for k in feats}
        self.assertGreater(p["proxy"], 0.01)
        self.assertLess(p["signal"], 0.01)
        self.assertEqual(null["_n"]["signal"], int(np.isfinite(feats["signal"]).sum()))

    def test_matched_diff_zero_when_selection_only_tracks_strata(self):
        rng = np.random.default_rng(12)
        n = 20000
        strata = rng.integers(0, 10, n)
        y = strata * 0.1 + rng.normal(scale=0.01, size=n)
        sel = strata >= 8
        self.assertGreater(np.mean(y[sel]) - np.mean(y), 0.3)
        self.assertAlmostEqual(tr.matched_diff(y, sel, strata), 0.0, places=2)


class TestRunnerC(unittest.TestCase):
    def test_end_to_end_absolute_gate_and_no_rerun(self):
        spec = importlib.util.spec_from_file_location("rfc", os.path.join(ROOT, "scripts", "run_family_C.py"))
        rfc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rfc)
        rng = np.random.default_rng(13)
        n = 3000
        rows = []
        for i in range(n):
            r = {"mint": f"M{i}", "K": 10, "create_ts": 1_770_000_000_000 + i * 600_000,
                 "is_mayhem": 0.0, "chk_curve_fitted": False, "o_base_status": "TIME",
                 "q_now": 30.0 + rng.uniform(0, 20), "fee_buy_ratio": 0.0125, "fee_sell_ratio": np.nan}
            for f in rfc.FEATURES:
                r[f] = rng.normal() if (f != "creator_prior_grad_rate" or i % 2) else np.nan
            r["o_base_net"] = -0.2 + 0.3 * r["unique_buyers"] + 0.1 * r["top1_hold_share"] + 0.2 * rng.normal()
            rows.append(r)
        df = pd.DataFrame(rows)
        rfc.load = lambda d, log: df
        rfc.N_PERM = 200
        orig = rfc.zn.load_zone
        self.addCleanup(setattr, rfc.zn, "load_zone", orig)
        with tempfile.TemporaryDirectory() as d:
            rfc.zn.load_zone = lambda z, name: [f"M{i}" for i in range(n)]
            argv = ["x", "--registry", str(Path(d) / "reg"), "--out", str(Path(d) / "out")]
            sys.argv = argv
            self.assertEqual(rfc.main(), 0)
            from tape.registry import Registry
            t = {v["name"]: v for v in Registry(Path(d) / "reg").trials().values() if v["family"] == "C"}
            self.assertEqual(len(t), 17)
            self.assertEqual(t["C unique_buyers @K=10"]["status"], "passed_discovery")   # mean net > 0
            self.assertEqual(t["C top1_hold_share @K=10"]["status"], "screened_out")     # real but not profitable
            sys.argv = argv
            self.assertEqual(rfc.main(), 2)
            sys.argv = argv[:3] + ["--out", str(Path(d) / "out_v3"), "--replicate"]
            self.assertEqual(rfc.main(), 0)
            t2 = Registry(Path(d) / "reg").trials()
            self.assertEqual(sum(1 for v in t2.values() if v["family"] == "C"), 17)
            self.assertEqual(sum(1 for v in t2.values() if v["name"] == "C replication on sets_v3 (D133)"), 1)
            self.assertEqual(rfc.main(), 2)


class TestRunnerE(unittest.TestCase):
    def test_walk_forward_no_lookahead_and_gate(self):
        spec = importlib.util.spec_from_file_location("rfe", os.path.join(ROOT, "scripts", "run_family_E.py"))
        rfe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rfe)
        from tape.pit_features import FEATURE_NAMES
        rng = np.random.default_rng(21)
        n = 4200
        t0 = 1_770_595_200_000                          # a Monday 00:00 UTC
        df = pd.DataFrame({f: rng.normal(size=n) for f in FEATURE_NAMES})
        df["st_x"] = rng.normal(size=n)
        df["mint"] = [f"M{i}" for i in range(n)]
        df["K"] = 10
        df["create_ts"] = t0 + np.arange(n) * (10 * 7 * 86_400_000 // n)   # 10 weeks
        df["is_mayhem"] = 0.0
        df["chk_curve_fitted"] = False
        df["o_base_status"] = "TIME"
        df["q_now"] = 30.0 + rng.uniform(0, 15, n)
        df["fee_buy_ratio"] = 0.0125
        df["fee_sell_ratio"] = 0.0125
        df["o_base_net"] = -0.1 + 0.5 * np.maximum(df["unique_buyers"], 0) + 0.05 * rng.normal(size=n)
        seen_max_train_ts = []

        def factory():
            from sklearn.ensemble import HistGradientBoostingRegressor

            def fit(X, yy):
                m = HistGradientBoostingRegressor(max_iter=60, random_state=0).fit(X, yy)
                return m.predict
            return fit, "sklearn-hgb (test stand-in)"

        rfe.load = lambda d, log: df
        orig = rfe.zn.load_zone
        self.addCleanup(setattr, rfe.zn, "load_zone", orig)
        with tempfile.TemporaryDirectory() as d:
            rfe.zn.load_zone = lambda z, name: list(df["mint"])
            sys.argv = ["x", "--registry", str(Path(d) / "reg"), "--out", str(Path(d) / "out")]
            holder = []
            try:
                self.assertEqual(rfe._main(holder, factory), 0)
            finally:
                for f in holder:
                    f.close()
            from tape.registry import Registry
            t = {v["name"]: v for v in Registry(Path(d) / "reg").trials().values() if v["family"] == "E"}
            self.assertEqual(len(t), 3)
            self.assertEqual(t["E pred > 0 @K=10"]["status"], "passed_discovery")
            holder = []
            try:
                self.assertEqual(rfe._main(holder, factory), 2)
            finally:
                for f in holder:
                    f.close()
            sys.argv = ["x", "--registry", str(Path(d) / "reg"), "--out", str(Path(d) / "out_v3"), "--replicate"]
            holder = []
            try:
                self.assertEqual(rfe._main(holder, factory), 0)
            finally:
                for f in holder:
                    f.close()
            t2 = Registry(Path(d) / "reg").trials()
            self.assertEqual(sum(1 for v in t2.values() if v["family"] == "E"), 3)
            res = json.loads((Path(d) / "out_v3" / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(res["set"], "sets_v3")
            e0 = res["e000_entry"]
            self.assertEqual(e0["n"], res["results"]["E000"]["n_selected"])
            self.assertAlmostEqual(e0["mean"], res["results"]["E000"]["mean_sel"], places=12)
            self.assertEqual(len(e0["mints_sha256"]), 64)

    def test_forbidden_columns_rejected(self):
        spec = importlib.util.spec_from_file_location("rfe2", os.path.join(ROOT, "scripts", "run_family_E.py"))
        rfe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rfe)
        df = pd.DataFrame({"st_ok": [1.0], "worst_case_pit": [0.0]})
        self.assertIn("worst_case_pit", rfe.feature_columns(df))
        bad = pd.DataFrame({"st_ok": [1.0], "worst_case_pit": [0.0], "q_now": [1.0]})
        rfe.FEATURE_NAMES = ["q_now", "create_ts"]
        bad["create_ts"] = 1
        with self.assertRaises(RuntimeError):
            rfe.feature_columns(bad)


class TestFamilyFGrid(unittest.TestCase):
    def test_grid_is_the_registered_24_and_contains_base(self):
        spec = importlib.util.spec_from_file_location("rff", os.path.join(ROOT, "scripts", "run_family_F.py"))
        rff = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rff)
        self.assertEqual(len(rff.GRID), 24)
        self.assertEqual(len(set(rff.GRID)), 24)
        self.assertIn((0.6, -0.30, 30), rff.GRID)
        c = rff.cfg_of(0.6, -0.30, 30)
        self.assertEqual((c.size_sol, c.latency_s, c.horizon_ms), (2.0, 1, 1_800_000))
        from tape.registry import FAMILY_BUDGET
        self.assertLessEqual(len(rff.GRID), FAMILY_BUDGET["F"])


class TestReplication(unittest.TestCase):
    def test_q_uses_replicated_p_and_status_follows_v3(self):
        from tape import replication as rp
        from tape.registry import Registry, RegistryError
        with tempfile.TemporaryDirectory() as d:
            reg = Registry(d)
            ids = [reg.register("A", f"a{i}", "h", {"feature": f"f{i}", "set": "sets_v2"}, "discovery",
                                p_value=p) for i, p in enumerate([0.001, 0.2, 0.5])]
            reg.update(ids[0], status="passed_discovery")
            reg.update(ids[1], status="screened_out")
            self.assertEqual(rp.original_ids(reg, "A", "feature"), {"f0": ids[0], "f1": ids[1], "f2": ids[2]})
            q = rp.replicated_q(reg, {ids[1]: 0.001})
            self.assertAlmostEqual(q[ids[1]], 0.0015)
            with self.assertRaises(RegistryError):
                rp.replicated_q(reg, {"A999": 0.1})
            tid = rp.record(reg, "A", {ids[0]: {"status": "screened_out", "p_value": 0.3, "q_bh": 0.45},
                                       ids[1]: {"status": "screened_out", "p_value": 0.2, "q_bh": 0.3}}, {})
            t = reg.trials()
            self.assertEqual(t[tid]["metrics"]["status_changes"], {ids[0]: ["passed_discovery", "screened_out"]})
            self.assertEqual(t[ids[0]]["status"], "screened_out")
            self.assertEqual(t[ids[0]]["p_value"], 0.3)               # later BH uses the corrected p
            self.assertTrue(rp.already_replicated(reg, "A"))
            self.assertFalse(rp.already_replicated(reg, "C"))
            self.assertEqual(sum(1 for v in t.values() if v["family"] == "A"), 3)
