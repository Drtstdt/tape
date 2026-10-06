"""D118 tests (no duckdb needed): the pure parts of tape/swaps_cache.py and
scripts/info_audit_v2.py's compact FeatureMatrix path.

What this CANNOT verify here: the real DuckDB COPY/sort/row-group pruning and
the source-vs-cache EXCEPT check -- those run on the user's machine inside
scripts/build_swaps_cache.py, which refuses to move an unverified cache into
place."""
import dataclasses
import importlib.util
import json
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tape import swaps_cache as sc  # noqa: E402
from tape.schema import CanonicalSwap  # noqa: E402


def _load_audit_v2():
    """Import scripts/info_audit_v2.py with duckdb stubbed ONLY for the
    duration of the import (so other tests' duckdb detection is untouched)."""
    had = "duckdb" in sys.modules
    try:
        import duckdb  # noqa: F401
        real = True
    except ImportError:
        real = False
    if not real:
        sys.modules["duckdb"] = types.ModuleType("duckdb")
    try:
        spec = importlib.util.spec_from_file_location(
            "info_audit_v2_under_test", ROOT / "scripts" / "info_audit_v2.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if not real and not had:
            sys.modules.pop("duckdb", None)
        sys.modules.pop("tape.store_v2", None)


ia = _load_audit_v2()


class TestFingerprint(unittest.TestCase):
    def test_changes_when_a_file_is_added_or_grows(self):
        with tempfile.TemporaryDirectory() as d:
            sw = Path(d) / "swaps" / "venue=v" / "dt=2026-01-01"
            sw.mkdir(parents=True)
            (sw / "a.parquet").write_bytes(b"x" * 10)
            f1 = sc.source_fingerprint(d)
            self.assertEqual(f1["files"], 1)
            (sw / "b.parquet").write_bytes(b"y" * 5)
            f2 = sc.source_fingerprint(d)
            self.assertNotEqual(f1, f2)
            self.assertEqual(f2["files"], 2)

    def test_stale_cache_is_refused_fresh_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            sw = Path(d) / "swaps" / "venue=v"
            sw.mkdir(parents=True)
            (sw / "a.parquet").write_bytes(b"x")
            cache = Path(d) / "cache"
            cache.mkdir()
            for b in range(sc.N_BUCKETS):
                sc.bucket_file(cache, b).write_bytes(b"c")
            sc.meta_path(cache).write_text(json.dumps(
                {"source_fingerprint": sc.source_fingerprint(d), "n_buckets": sc.N_BUCKETS}))
            sc.check_cache_fresh(cache, d)  # fresh: no raise
            (sw / "b.parquet").write_bytes(b"z")
            with self.assertRaises(RuntimeError):
                sc.check_cache_fresh(cache, d)

    def test_incomplete_cache_raises(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "swaps").mkdir()
            cache = Path(d) / "cache"
            cache.mkdir()
            sc.meta_path(cache).write_text(json.dumps(
                {"source_fingerprint": sc.source_fingerprint(d), "n_buckets": sc.N_BUCKETS}))
            with self.assertRaises(FileNotFoundError):  # bucket files missing
                sc.check_cache_fresh(cache, d)

    def test_missing_cache_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                sc.check_cache_fresh(Path(d) / "nope", d)

    def test_bucket_is_deterministic_in_range_and_spreads(self):
        ms = [f"mint{i}pump" for i in range(5000)]
        bs = [sc.bucket_of(m) for m in ms]
        self.assertEqual(bs, [sc.bucket_of(m) for m in ms])
        self.assertTrue(all(0 <= b < sc.N_BUCKETS for b in bs))
        self.assertEqual(len(set(bs)), sc.N_BUCKETS)
        self.assertEqual(sc.bucket_of("abc"), 891568578 % sc.N_BUCKETS)  # crc32('abc')


class _FakeCon:
    """Mimics con.execute(sql).df() for fetch_swaps: records the SQL and
    answers from an in-memory frame."""

    def __init__(self, frame):
        self.frame, self.sqls = frame, []

    def execute(self, sql):
        self.sqls.append(sql)
        mint = sql.split("WHERE mint = '")[1].split("'")[0]
        df = self.frame[self.frame.mint == mint].sort_values(["ts_ms", "slot", "sig"])
        return types.SimpleNamespace(df=lambda: df.reset_index(drop=True))


def _frame(n_mints=5, seed=0):
    rnd = random.Random(seed)
    names = [f.name for f in dataclasses.fields(CanonicalSwap)]
    rows = []
    for k in range(n_mints):
        for i in range(rnd.choice([0, 4, 9])):
            d = {n: None for n in names}
            d.update(mint=f"M{k}", venue="v", pool="p", ts_ms=1000 + rnd.randint(0, 99),
                     slot=rnd.randint(0, 5), sig=f"s{rnd.random()}", side="buy",
                     base_amount=1.0, quote_amount=1.0, quote_mint="q", price=1.0,
                     source="x")
            d["dt"] = "2026-01-01"  # extra hive column, must be dropped
            rows.append(d)
    return pd.DataFrame(rows)


class TestFetchSwaps(unittest.TestCase):
    def test_returns_canonical_swaps_per_mint_in_order_and_drops_extra_cols(self):
        con = _FakeCon(_frame())
        out = sc.fetch_swaps(con, "x.parquet", ["M0", "M1", "M2", "M3", "M4"])
        self.assertEqual(set(out), {"M0", "M1", "M2", "M3", "M4"})
        for swaps in out.values():
            keys = [(s.ts_ms, s.slot, s.sig) for s in swaps]
            self.assertEqual(keys, sorted(keys))
            self.assertTrue(all(isinstance(s, CanonicalSwap) for s in swaps))

    def test_sql_uses_same_dedup_window_as_store_view(self):
        con = _FakeCon(_frame())
        sc.fetch_swaps(con, "x.parquet", ["M0"])
        sql = con.sqls[0]
        self.assertIn("PARTITION BY sig, mint, side, round(base_amount, 12) ORDER BY source", sql)
        self.assertIn("WHERE rn = 1", sql)
        self.assertIn("ORDER BY ts_ms, slot, sig", sql)

    def test_quote_escapes(self):
        self.assertEqual(sc._q("a'b"), "'a''b'")


class TestFeatureMatrix(unittest.TestCase):
    def _rows(self):
        names = ia.feature_names_fixed()
        rnd = random.Random(3)
        rows = []
        for _ in range(200):
            r = {}
            for n in names:
                x = rnd.random()
                r[n] = None if x < 0.2 else (float(rnd.randint(0, 3)) if x < 0.5 else rnd.gauss(0, 1))
            rows.append(r)
        return names, rows

    def test_probe_names_cover_real_feature_keys(self):
        """The worker's fixed name list must equal what TokenState emits on a
        real (non-probe) update path, else dicts_to_matrix would raise."""
        from tape.features import TokenState
        self.assertEqual(ia.feature_names_fixed(), sorted(TokenState("probe").features()))

    def test_matrix_equals_old_dict_path_bit_for_bit(self):
        names, rows = self._rows()
        fm = ia.FeatureMatrix.from_dicts(rows, names)
        for n in names:
            old = np.array([ia._f(r.get(n)) for r in rows])
            np.testing.assert_array_equal(fm.col(n), old)

    def test_feature_table_identical_old_vs_new(self):
        names, rows = self._rows()
        y = np.array([random.Random(5).random() < 0.4 for _ in range(len(rows))], dtype=float)
        rnd = np.random.default_rng(1)
        y = (rnd.random(len(rows)) < 0.4).astype(float)
        mask = rnd.random(len(rows)) < 0.7
        groups = np.array([f"g{i % 20}" for i in range(len(rows))])
        fm = ia.FeatureMatrix.from_dicts(rows, names)
        new = ia.feature_table(fm, y, groups, mask, names)
        # reference: the original dict-based computation
        ref = []
        for n in names:
            col = np.array([ia._f(r.get(n)) for r in rows])
            a = ia.auc(col[mask], y[mask])
            if np.isnan(a):
                continue
            ref.append((n, a, abs(a - 0.5), ia.direction_of(a), int((~np.isnan(col[mask])).sum())))
        ref.sort(key=lambda r: r[2], reverse=True)
        self.assertEqual(new, ref)

    def test_permutation_null_identical_old_vs_new(self):
        names, rows = self._rows()
        rnd = np.random.default_rng(2)
        y = (rnd.random(len(rows)) < 0.4).astype(float)
        groups = np.array([f"g{i % 20}" for i in range(len(rows))])
        fm = ia.FeatureMatrix.from_dicts(rows, names)
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            a = ia.permutation_null_max_edge(fm, y, groups, names, 5, 0)
            b = ia.permutation_null_max_edge(fm.subset(np.ones(len(rows), bool)), y, groups, names, 5, 0)
        np.testing.assert_array_equal(a, b)

    def test_unknown_feature_key_is_an_error_not_dropped(self):
        with self.assertRaises(KeyError):
            ia.dicts_to_matrix([{"a": 1.0, "zzz": 2.0}], ["a"])

    def test_stack_and_empty(self):
        names = ["a", "b"]
        fm = ia.FeatureMatrix.stack([np.ones((2, 2)), np.empty((0, 2)), np.zeros((3, 2))], names)
        self.assertEqual(len(fm), 5)
        self.assertEqual(len(ia.FeatureMatrix.stack([], names)), 0)


class TestWorkerChunk(unittest.TestCase):
    def test_chunk_results_match_build_one_and_keep_order_cache_and_store_paths(self):
        frame = _frame(n_mints=12, seed=9)
        # enough swaps per mint to pass nothing in particular; we only need
        # status/rows parity with build_one on identical swaps.
        mints = [f"M{k}" for k in range(12)]

        class FakeStore:
            def sql(self, q):
                ms = {x.strip().strip("'") for x in q.split("IN (")[1].split(")")[0].split(",")}
                return frame[frame.mint.isin(ms)].sort_values(
                    ["mint", "ts_ms", "slot", "sig"]).reset_index(drop=True)

        ia._worker_names = ia.feature_names_fixed()
        ia._worker_cfg = ia.BarrierConfig(1.6, -0.3, 1_800_000)
        ia._worker_bar_fraction, ia._worker_stride, ia._worker_non_overlap = 0.01, 5, False

        ia._worker_store, ia._worker_cache_con = FakeStore(), None
        via_store = ia._worker_build_chunk(mints)
        ia._worker_store, ia._worker_cache_con = None, _FakeCon(frame)
        ia._worker_cache_path = "x.parquet"
        via_cache = ia._worker_build_chunk(mints)
        for m, a, b in zip(mints, via_store, via_cache):
            ref_swaps = sc.rows_to_swaps(frame[frame.mint == m].sort_values(["ts_ms", "slot", "sig"]))
            ref = ia.build_one(None, m, ia._worker_cfg, 0.01, 5, False, swaps=ref_swaps)
            self.assertEqual(a[0], ref[0])
            self.assertEqual(b[0], ref[0])
            np.testing.assert_array_equal(a[1], ia.dicts_to_matrix(ref[1], ia._worker_names))
            np.testing.assert_array_equal(b[1], a[1])
            self.assertEqual(a[2], ref[2])


class TestWorkerChunkWithRealRows(unittest.TestCase):
    def test_ok_mint_rows_survive_the_matrix_roundtrip(self):
        names = [f.name for f in dataclasses.fields(CanonicalSwap)]
        rnd = random.Random(4)
        rows, price = [], 1.0
        for i in range(400):
            price *= 1 + rnd.uniform(-0.02, 0.025)
            d = {n: None for n in names}
            d.update(mint="OK1", venue="v", pool="p", ts_ms=10_000 * i, slot=i, sig=f"s{i}",
                     side="buy" if rnd.random() < 0.55 else "sell",
                     base_amount=1.0 / price, quote_amount=1.0, quote_mint="q", price=price,
                     wallet=f"w{rnd.randint(0, 30)}", quote_reserve_after=100.0,
                     base_reserve_after=100.0 / price, source="x")
            rows.append(d)
        frame = pd.DataFrame(rows)
        ia._worker_names = ia.feature_names_fixed()
        ia._worker_cfg = ia.BarrierConfig(1.6, -0.3, 1_800_000)
        ia._worker_bar_fraction, ia._worker_stride, ia._worker_non_overlap = 0.01, 5, False
        ia._worker_store, ia._worker_cache_con = None, _FakeCon(frame)
        ia._worker_cache_path = "x.parquet"
        (res,) = ia._worker_build_chunk(["OK1"])
        ref = ia.build_one(None, "OK1", ia._worker_cfg, 0.01, 5, False,
                           swaps=sc.rows_to_swaps(frame))
        self.assertEqual(ref[0], "ok")
        self.assertGreater(len(ref[1]), 0)
        self.assertEqual(res[0], "ok")
        self.assertEqual(res[1].shape, (len(ref[1]), len(ia._worker_names)))
        np.testing.assert_array_equal(res[1], ia.dicts_to_matrix(ref[1], ia._worker_names))
        self.assertEqual(res[2], ref[2])


class TestCommitPreflight(unittest.TestCase):
    def test_parse_gb(self):
        self.assertEqual(sc.parse_gb("2GB"), 2.0)
        self.assertEqual(sc.parse_gb("4 GiB"), 4.0)
        self.assertAlmostEqual(sc.parse_gb("512MB"), 0.512)

    def test_warns_only_when_need_exceeds_available(self):
        msgs = []
        orig = sc.commit_headroom
        try:
            sc.commit_headroom = lambda: (5.3, 35.6)
            sc.commit_preflight(14.0, "x", msgs.append)
            self.assertTrue(any("WARNING" in m for m in msgs))
            msgs.clear()
            sc.commit_preflight(3.0, "x", msgs.append)
            self.assertFalse(any("WARNING" in m for m in msgs))
            msgs.clear()
            sc.commit_headroom = lambda: None  # non-Windows: silent
            sc.commit_preflight(99.0, "x", msgs.append)
            self.assertEqual(msgs, [])
        finally:
            sc.commit_headroom = orig


if __name__ == "__main__":
    unittest.main()
