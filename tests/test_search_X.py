"""D142: family X search -- inference helpers and an end-to-end run on
synthetic paths (parquet/lightgbm replaced by test stand-ins)."""

import importlib.util
import json
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

spec = importlib.util.spec_from_file_location("sx", os.path.join(ROOT, "scripts", "search_X.py"))
sx = importlib.util.module_from_spec(spec)
sys.modules["sx"] = sx
spec.loader.exec_module(sx)

import run_family_E as rfe  # noqa: E402
from tape import curve as cv  # noqa: E402


class TestInference(unittest.TestCase):
    def test_reality_check_null_and_power(self):
        rng = np.random.default_rng(0)
        C, D = 300, 60
        N = np.full((C, D), 20.0, np.float32)
        S = rng.normal(0, 0.2, (C, D)).astype(np.float32) * 20 / np.sqrt(20)
        r0 = sx.reality_check(S, N, n_boot=300)
        self.assertGreater(r0["p_value"], 0.05)                       # pure noise: the max is luck
        S2 = S.copy()
        S2[7] += 0.05 * 20                                             # one real +5% config
        r1 = sx.reality_check(S2, N, n_boot=300)
        self.assertLess(r1["p_value"], 0.05)

    def test_pbo_noise_vs_signal(self):
        rng = np.random.default_rng(1)
        C, D = 200, 60
        N = np.full((C, D), 10.0, np.float32)
        S = rng.normal(0, 1, (C, D)).astype(np.float32)
        self.assertGreater(sx.pbo_cscv(S, N)["pbo"], 0.3)
        S[3] += 3.0
        self.assertLess(sx.pbo_cscv(S, N)["pbo"], 0.1)

    def test_selection_uses_calibration_week(self):
        pred = np.array([0.1, 0.5, 0.9, np.nan, 0.2])
        week = np.array([5, 5, 5, 5, 6])
        cal = {5: np.sort(np.array([0.0, 0.4, 0.8])), 6: np.sort(np.array([0.3]))}
        self.assertEqual(sx.selection(pred, week, cal, "pos").tolist(), [True, True, True, False, True])
        self.assertEqual(sx.selection(pred, week, cal, 1.0).tolist(), [True, True, True, False, True])
        top = sx.selection(pred, week, cal, 0.34)
        self.assertEqual(top.tolist(), [False, False, True, False, False])


def synth(tmp, rng, n_per_k=1600, name="paths", weeks=8, week0=0, prefix=""):
    paths_dir = Path(tmp) / name
    paths_dir.mkdir()
    t0 = 1_770_595_200_000 + week0 * 7 * 86_400_000                  # a Monday
    uni, metas, arrs = {}, {b: [] for b in range(64)}, {b: ([], [], []) for b in range(64)}
    offs = {b: 0 for b in range(64)}
    for K in sx.KS:
        n = n_per_k
        ts = t0 + np.sort(rng.integers(0, weeks * 7 * 86_400_000, n))
        X = rng.normal(size=(n, 3)).astype(np.float32)
        good = X[:, 0] > 1.0
        mints = [f"{prefix}K{K}m{i}" for i in range(n)]
        for i, m in enumerate(mints):
            ln = int(rng.integers(20, 200))
            slot_rel = np.concatenate([[0], np.cumsum(rng.integers(0, 3, ln - 1))]).astype(np.int32)
            rel = (slot_rel.astype(np.int64) * 400 // 1000 * 1000).astype(np.int32)
            drift = 0.3 if good[i] else -0.05
            q = (31.0 + np.abs(np.cumsum(rng.normal(drift, 0.5, ln)))).astype(np.float32)
            b = i % 64
            arrs[b][0].append(rel)
            arrs[b][1].append(slot_rel)
            arrs[b][2].append(q)
            metas[b].append({"mint": m, "K": K, "create_ts": int(ts[i]), "decision_ts": int(ts[i]) + 1000,
                             "day": int(ts[i]) // 86_400_000, "week": 0, "k": cv.K_STD, "fee_b": 0.0125,
                             "fee_s": 0.0125, "grad": ln, "offset": offs[b], "length": ln})
            offs[b] += ln
        u = pd.DataFrame({"mint": mints, "o_base_net": np.where(good, 0.2, -0.1) + rng.normal(0, 0.05, n),
                          "o_base_max_net": np.where(good, 0.5, 0.0)})
        week = (ts - rfe.MONDAY0) // rfe.WEEK_MS
        uni[K] = (u, ["a", "b", "c"], X, u["o_base_net"].to_numpy(), ts, week, ts // 86_400_000, np.unique(week))
    for b in range(64):
        np.savez(paths_dir / f"path-b{b:02d}.npz", rel_ts=np.concatenate(arrs[b][0]),
                 slot_rel=np.concatenate(arrs[b][1]), q=np.concatenate(arrs[b][2]))
        with open(paths_dir / f"meta-b{b:02d}.pkl", "wb") as f:
            pickle.dump(pd.DataFrame(metas[b]), f)
    return paths_dir, uni


class TestEndToEnd(unittest.TestCase):
    def test_search_runs_freezes_candidates_and_refuses_rerun(self):
        rng = np.random.default_rng(5)
        with tempfile.TemporaryDirectory() as d:
            paths_dir, uni = synth(d, rng)

            def fake_model():
                from sklearn.linear_model import Ridge

                def fit(X, y):
                    return Ridge(alpha=1.0).fit(X, y).predict
                return fit, "ridge (test stand-in)"

            orig = (sx.load_universe, sx.read_meta, rfe.make_model)
            self.addCleanup(lambda: (setattr(sx, "load_universe", orig[0]), setattr(sx, "read_meta", orig[1]),
                                     setattr(rfe, "make_model", orig[2])))
            # in-process stage B: patched readers are not visible to spawned workers (Windows spawn)
            sx.load_universe = lambda sets_dir, K: uni[K]
            sx.read_meta = lambda pdir, b: pd.read_pickle(Path(pdir) / f"meta-b{b:02d}.pkl")
            rfe.make_model = fake_model
            out = Path(d) / "out"
            sys.argv = ["x", "--paths", str(paths_dir), "--registry", str(Path(d) / "reg"), "--out", str(out),
                        "--workers", "0", "--hours", "0.02", "--max-rounds", "2", "--patience", "2"]
            self.assertEqual(sx.main(), 0)
            c = json.loads((out / "candidates.json").read_text())
            self.assertEqual(len(c["candidates"]), 4)
            self.assertEqual(c["candidates"][0]["slot"], "X000")
            self.assertEqual(c["candidates"][0]["config"]["tp"], 0.3)
            self.assertGreater(c["n_configs"], 100_000)
            self.assertIn("p_value", c["reality_check"])
            from tape.registry import Registry
            t = Registry(Path(d) / "reg").trials()
            self.assertEqual(sum(1 for v in t.values() if v["family"] == "X"), 4)
            groups = {(x["config"]["K"], x["config"]["target"], str(x["config"]["q"]), x["config"]["size"])
                      for x in c["candidates"]}
            self.assertEqual(len(groups), 4)                          # one candidate per group
            best = c["candidates"][1]["config"]
            self.assertNotEqual(best["q"], 1.0)                       # the signal is in the selective slices
            self.assertEqual(sx.main(), 2)                            # frozen: no re-roll

            # the one validation look
            vdir, vuni = synth(d, rng, n_per_k=800, name="vpaths", weeks=3, week0=8, prefix="v")
            sx.load_universe = lambda sets_dir, K: (vuni if "validation" in str(sets_dir) else uni)[K]
            sys.modules["search_X"] = sx
            vspec = importlib.util.spec_from_file_location("vx", os.path.join(ROOT, "scripts", "validate_X.py"))
            vx = importlib.util.module_from_spec(vspec)
            vspec.loader.exec_module(vx)
            sys.argv = ["x", "--sets", str(Path(d) / "sets"), "--paths", str(vdir), "--registry", str(Path(d) / "reg"),
                        "--xdir", str(out)]
            self.assertEqual(vx.main(), 0)
            vr = json.loads((out / "validation_results.json").read_text())
            self.assertEqual(len(vr), 4)
            t = Registry(Path(d) / "reg").trials()
            self.assertTrue(all(t[k]["status"] in ("passed_validation", "failed_validation") for k in vr))
            self.assertTrue(any(v["passed"] for v in vr.values()))    # the synthetic signal is real
            self.assertEqual(vx.main(), 2)                            # one look only


if __name__ == "__main__":
    unittest.main()
