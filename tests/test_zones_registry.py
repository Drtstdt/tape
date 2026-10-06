"""D123: frozen zones + trial registry -- the guards are the point."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tape import zones as zn  # noqa: E402
from tape.registry import Registry, RegistryError, bh, FAMILY_BUDGET  # noqa: E402

H = zn.HOUR_MS


class TestZones(unittest.TestCase):
    def test_choose_cuts(self):
        counts = {f"2026-02-{d:02d}": 10 for d in range(1, 11)}      # 10 equal days
        self.assertEqual(zn.choose_cuts(counts), ("2026-02-06", "2026-02-08"))

    def test_assign_zone_and_embargo(self):
        c1, c2, end = zn.utc_ms("2026-05-10"), zn.utc_ms("2026-06-15"), zn.utc_ms("2026-10-01")
        self.assertEqual(zn.assign_zone(c1 - 2 * H - 1, c1, c2, end), "discovery")
        self.assertEqual(zn.assign_zone(c1 - 2 * H, c1, c2, end), "embargo")
        self.assertEqual(zn.assign_zone(c1 - 1, c1, c2, end), "embargo")
        self.assertEqual(zn.assign_zone(c1, c1, c2, end), "validation")
        self.assertEqual(zn.assign_zone(c2 - H, c1, c2, end), "embargo")
        self.assertEqual(zn.assign_zone(c2, c1, c2, end), "sealed")
        self.assertEqual(zn.assign_zone(end, c1, c2, end), "out")

    def test_hash_order_independent(self):
        self.assertEqual(zn.list_hash(["b", "a"]), zn.list_hash(["a", "b"]))
        self.assertNotEqual(zn.list_hash(["a"]), zn.list_hash(["a", "b"]))

    def _frozen(self, d):
        d = Path(d)
        meta = {"zones": {}}
        for z, ms in (("discovery", ["m1", "m2"]), ("sealed_dense", ["m9"])):
            h = zn.write_list(d / f"mints_{z}.txt", ms)
            meta["zones"][z] = {"file": f"mints_{z}.txt", "sha256": h}
        (d / "zones.json").write_text(json.dumps(meta), encoding="utf-8")
        return d

    def test_load_verifies_and_guards_sealed(self):
        with tempfile.TemporaryDirectory() as d:
            self._frozen(d)
            self.assertEqual(zn.load_zone(d, "discovery"), ["m1", "m2"])
            with self.assertRaises(PermissionError):
                zn.load_zone(d, "sealed_dense")
            (Path(d) / "mints_discovery.txt").write_text("m1\nm2\nm3\n", encoding="utf-8")
            with self.assertRaises(zn.ZoneIntegrityError):
                zn.load_zone(d, "discovery")


class TestRegistry(unittest.TestCase):
    def test_register_ids_budget_and_rules(self):
        with tempfile.TemporaryDirectory() as d:
            r = Registry(d)
            ids = [r.register("A", f"a{i}", "h", {"i": i}, "discovery", p_value=0.5) for i in range(3)]
            self.assertEqual(ids, ["A000", "A001", "A002"])
            with self.assertRaises(RegistryError):
                r.register("Z", "x", "h", {}, "discovery")
            with self.assertRaises(RegistryError):
                r.register("A", "x", "h", {}, "sealed")
            for i in range(3, FAMILY_BUDGET["E"] + 3):
                if i - 3 < FAMILY_BUDGET["E"]:
                    r.register("E", f"e{i}", "h", {}, "discovery")
            with self.assertRaises(RegistryError):
                r.register("E", "one too many", "h", {}, "discovery")
            r.update("A001", status="screened_out", metrics={"auc": 0.51})
            t = r.trials()
            self.assertEqual(t["A001"]["status"], "screened_out")
            self.assertEqual(len(r.events()), 3 + FAMILY_BUDGET["E"] + 1)   # append-only

    def test_bh(self):
        q = bh({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5})
        self.assertAlmostEqual(q["a"], 0.04)
        self.assertAlmostEqual(q["c"], 0.04 * 4 / 3)     # min(0.03*4/2, q_b)
        self.assertAlmostEqual(q["b"], 0.04 * 4 / 3)
        self.assertAlmostEqual(q["d"], 0.5)
        self.assertTrue(q["a"] <= q["c"] <= q["b"] <= q["d"])

    def test_sealed_once_and_search_closes(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as zd:
            TestZones()._frozen(zd)
            r = Registry(d)
            tid = r.register("A", "a", "h", {}, "discovery", p_value=0.001)
            with self.assertRaises(RegistryError):
                r.open_sealed(zd)                               # nothing pre-registered
            with self.assertRaises(RegistryError):
                r.preregister_candidates([{"trial_id": tid}])   # not passed validation
            r.update(tid, status="passed_validation")
            r.preregister_candidates([{"trial_id": tid, "threshold": 0.4}])
            with self.assertRaises(RegistryError):
                r.preregister_candidates([{"trial_id": tid}])   # written once
            got = r.open_sealed(zd)
            self.assertEqual(got, {"sealed_dense": ["m9"]})
            with self.assertRaises(RegistryError):
                r.open_sealed(zd)                               # exactly once
            with self.assertRaises(RegistryError):
                r.register("B", "late idea", "h", {}, "discovery")


class TestFreezeScript(unittest.TestCase):
    def test_freeze_end_to_end_and_refuse_rerun(self):
        import importlib.util
        import pandas as pd
        from tape import pf_store as ps
        spec = importlib.util.spec_from_file_location("fz", os.path.join(ROOT, "scripts", "freeze_zones_v1.py"))
        fz = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fz)
        rows, mi = [], []
        t0 = zn.utc_ms("2026-02-08")
        for i in range(3000):
            ts = t0 + (i * 239 * 60_000) % (230 * 86_400_000)
            rows.append({"event_type": "create", "mint": f"M{i}", "ts_ms": float(ts)})
            if i % 10:
                mi.append({"mint": f"M{i}", "bucket": 0, "first_ts": ts, "last_ts": ts, "n_raw": 1})
        rows.append({"event_type": "bonding_complete", "mint": "M1", "ts_ms": float(t0 + 5)})
        old_e, old_m = ps.load_events, ps.load_mint_index
        ps.load_events = lambda s: pd.DataFrame(rows)
        ps.load_mint_index = lambda s: pd.DataFrame(mi)
        try:
            with tempfile.TemporaryDirectory() as d:
                store = Path(d) / "store"
                store.mkdir()
                (store / "compacted.json").write_text("{}", encoding="utf-8")
                out, reg = Path(d) / "zones_v1", Path(d) / "registry"
                argv = ["x", "--store", str(store), "--out", str(out), "--registry", str(reg),
                        "--facts", str(Path(d) / "none.json")]
                sys.argv = argv
                self.assertEqual(fz.main(), 0)
                meta = json.loads((out / "zones.json").read_text(encoding="utf-8"))
                n = sum(z["n"] for z in meta["zones"].values()) + meta["counts"].get("embargo", 0) \
                    + meta["counts"].get("out", 0)
                self.assertEqual(n, 2700)
                self.assertEqual(zn.load_zone(out, "discovery")[:1], sorted(zn.load_zone(out, "discovery"))[:1])
                all_m = set()
                for z in meta["zones"]:
                    ms = set(zn.read_list(out / meta["zones"][z]["file"]))
                    self.assertFalse(all_m & ms)
                    all_m |= ms
                sys.argv = argv
                self.assertEqual(fz.main(), 2)          # frozen: refuses to recompute
                for f in out.iterdir():                 # let the tempdir clean up on Windows
                    os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
        finally:
            ps.load_events, ps.load_mint_index = old_e, old_m


if __name__ == "__main__":
    unittest.main()
