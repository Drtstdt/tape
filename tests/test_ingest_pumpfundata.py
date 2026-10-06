"""Tests for scripts/ingest_pumpfundata.py's pure logic, _corroborate's
python-side tallying (D100), and _merge_creation_times (D101, docs/DECISIONS.md).
Same importlib-by-path loading as tests/test_backfill_discovered_launches.py.

_corroborate()'s actual SQL (the register+JOIN against a real DuckDB
connection) is NOT exercised here -- this sandbox cannot install duckdb
(same restriction D93/D98/D99 hit for pyarrow). A FakeStore stands in for
the real tape.store.Store: its `.sql()` returns a pre-built pandas
DataFrame (standing in for what a real JOIN would return) and its `.con`
records register()/unregister() calls so the test can assert the temp-table
lifecycle without a real DuckDB connection. The user's own real
`ingest_pumpfundata.py` run (D99/D100) is the actual end-to-end verification
that the real SQL executes and returns the right rows. `_merge_creation_times`
needs no Store/DuckDB at all -- it's plain JSON file I/O, exercised here
against real temp files.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "ingest_pumpfundata", ROOT / "scripts" / "ingest_pumpfundata.py")
ingest_pumpfundata = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingest_pumpfundata)

from tape.schema import BUY, SELL, CanonicalSwap

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _swap(sig: str, side: str = BUY, base_amount: float = 1.0) -> CanonicalSwap:
    return CanonicalSwap(
        mint="ExampleMint111111111111111111111111111111",
        venue=PUMPFUN_PROGRAM,
        pool="CreatorWallet111111111111111111111111111",
        ts_ms=1_770_530_400_000,
        slot=123,
        sig=sig,
        side=side,
        base_amount=base_amount,
        quote_amount=0.5,
        quote_mint="So11111111111111111111111111111111111111112",
        price=0.5,
        source="pumpfundata",
    )


class FakeConnection:
    """Stands in for the real duckdb connection `_corroborate` calls
    `.register()`/`.unregister()` on."""

    def __init__(self):
        self.registered = []
        self.unregistered = []

    def register(self, name, df):
        self.registered.append((name, df))

    def unregister(self, name):
        self.unregistered.append(name)


class FakeStore:
    """Stands in for tape.store.Store: `.con` is a FakeConnection, `.sql()`
    ignores the query text entirely and returns a pre-built frame -- good
    enough to test _corroborate's own python-side tallying logic, not the
    real SQL. `write_swaps()` just records what it was called with (D102's
    `_flush_batch` tests need this; `_corroborate`'s own tests never call
    write_swaps, so it was never needed before)."""

    def __init__(self, sql_result: pd.DataFrame):
        self.con = FakeConnection()
        self._sql_result = sql_result
        self.written_batches = []

    def sql(self, query: str):
        return self._sql_result

    def write_swaps(self, swaps):
        self.written_batches.append(list(swaps))
        return []


class TestBatched(unittest.TestCase):
    def test_splits_into_even_chunks(self):
        chunks = list(ingest_pumpfundata._batched([1, 2, 3, 4], 2))
        self.assertEqual(chunks, [[1, 2], [3, 4]])

    def test_last_chunk_may_be_shorter(self):
        chunks = list(ingest_pumpfundata._batched([1, 2, 3, 4, 5], 2))
        self.assertEqual(chunks, [[1, 2], [3, 4], [5]])

    def test_empty_input_yields_nothing(self):
        self.assertEqual(list(ingest_pumpfundata._batched([], 5)), [])

    def test_n_larger_than_input_yields_one_chunk(self):
        self.assertEqual(list(ingest_pumpfundata._batched([1, 2], 100)), [[1, 2]])

    def test_every_item_preserved_in_order_across_chunks(self):
        items = list(range(37))
        chunks = list(ingest_pumpfundata._batched(items, 5))
        self.assertEqual([x for chunk in chunks for x in chunk], items)


class TestCorroborate(unittest.TestCase):
    def test_no_sigs_returns_zeroed_tally_without_touching_store(self):
        store = FakeStore(pd.DataFrame())
        result = ingest_pumpfundata._corroborate(store, [])
        self.assertEqual(result, {"checked": 0, "side_agree": 0, "side_disagree": 0,
                                   "amount_agree": 0, "amount_disagree": 0,
                                   "disagreements": []})
        self.assertEqual(store.con.registered, [])

    def test_registers_and_unregisters_the_candidate_sig_table(self):
        store = FakeStore(pd.DataFrame(columns=["sig", "side", "base_amount",
                                                  "quote_amount", "source"]))
        ingest_pumpfundata._corroborate(store, [_swap("Sig1")])
        self.assertEqual(len(store.con.registered), 1)
        name, df = store.con.registered[0]
        self.assertEqual(name, "_pumpfundata_corrob_sigs")
        self.assertEqual(list(df["sig"]), ["Sig1"])
        self.assertEqual(store.con.unregistered, ["_pumpfundata_corrob_sigs"])

    def test_matching_side_and_amount_counts_as_agreement(self):
        existing = pd.DataFrame([
            {"sig": "Sig1", "side": BUY, "base_amount": 1.0,
             "quote_amount": 0.5, "source": "helius"},
        ])
        store = FakeStore(existing)
        result = ingest_pumpfundata._corroborate(store, [_swap("Sig1", side=BUY, base_amount=1.0)])
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["side_agree"], 1)
        self.assertEqual(result["amount_agree"], 1)
        self.assertEqual(result["disagreements"], [])

    def test_side_disagreement_is_flagged_and_skips_amount_check(self):
        existing = pd.DataFrame([
            {"sig": "Sig1", "side": SELL, "base_amount": 1.0,
             "quote_amount": 0.5, "source": "helius"},
        ])
        store = FakeStore(existing)
        result = ingest_pumpfundata._corroborate(store, [_swap("Sig1", side=BUY, base_amount=1.0)])
        self.assertEqual(result["side_disagree"], 1)
        self.assertEqual(result["amount_agree"], 0)
        self.assertEqual(result["amount_disagree"], 0)
        self.assertEqual(len(result["disagreements"]), 1)
        self.assertIn("side=", result["disagreements"][0])

    def test_amount_disagreement_beyond_rtol_is_flagged(self):
        existing = pd.DataFrame([
            {"sig": "Sig1", "side": BUY, "base_amount": 1.0,
             "quote_amount": 0.5, "source": "helius"},
        ])
        store = FakeStore(existing)
        result = ingest_pumpfundata._corroborate(store, [_swap("Sig1", side=BUY, base_amount=2.0)])
        self.assertEqual(result["side_agree"], 1)
        self.assertEqual(result["amount_disagree"], 1)
        self.assertEqual(len(result["disagreements"]), 1)
        self.assertIn("base_amount=", result["disagreements"][0])

    def test_sig_with_no_matching_existing_row_is_not_checked(self):
        existing = pd.DataFrame(columns=["sig", "side", "base_amount",
                                          "quote_amount", "source"])
        store = FakeStore(existing)
        result = ingest_pumpfundata._corroborate(store, [_swap("SigNoMatch")])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["disagreements"], [])

    def test_swap_with_no_sig_is_excluded_from_candidate_table(self):
        existing = pd.DataFrame(columns=["sig", "side", "base_amount",
                                          "quote_amount", "source"])
        store = FakeStore(existing)
        swaps = [_swap("Sig1"), _swap("")]
        # sig="" is falsy -> excluded from the candidate set, same `if s.sig`
        # filter _row_to_swap's own required-field check already relies on
        # (a real swap can never actually have an empty sig, but the filter
        # itself is worth pinning).
        ingest_pumpfundata._corroborate(store, swaps)
        _, df = store.con.registered[0]
        self.assertEqual(list(df["sig"]), ["Sig1"])


class TestMergeCreationTimes(unittest.TestCase):
    """D101 (docs/DECISIONS.md): plain JSON file I/O, no Store/DuckDB needed."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.cache_path = self.data_dir / "real_creation_times.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_creates_file_when_none_exists(self):
        added, conflicts = ingest_pumpfundata._merge_creation_times(
            self.data_dir, {"MintA": 1000})
        self.assertEqual(added, 1)
        self.assertEqual(conflicts, [])
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual(cache["MintA"], {"status": "ok", "real_created_ts_ms": 1000,
                                           "source": "pumpfundata_create_event"})

    def test_fills_in_a_mint_missing_from_existing_cache(self):
        self.cache_path.write_text(json.dumps({
            "MintExisting": {"status": "ok", "real_created_ts_ms": 500},
        }))
        added, conflicts = ingest_pumpfundata._merge_creation_times(
            self.data_dir, {"MintNew": 2000})
        self.assertEqual(added, 1)
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual(cache["MintExisting"], {"status": "ok", "real_created_ts_ms": 500})
        self.assertEqual(cache["MintNew"]["real_created_ts_ms"], 2000)

    def test_fills_in_a_mint_whose_existing_record_has_no_timestamp(self):
        # e.g. a cached "error" from fetch_real_creation_times.py -- no
        # real_created_ts_ms recorded, so pumpfundata's own value should fill it.
        self.cache_path.write_text(json.dumps({
            "MintA": {"status": "error", "real_created_ts_ms": None},
        }))
        added, conflicts = ingest_pumpfundata._merge_creation_times(
            self.data_dir, {"MintA": 1000})
        self.assertEqual(added, 1)
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual(cache["MintA"]["status"], "ok")
        self.assertEqual(cache["MintA"]["real_created_ts_ms"], 1000)

    def test_matching_existing_value_is_not_counted_as_added_or_conflict(self):
        self.cache_path.write_text(json.dumps({
            "MintA": {"status": "ok", "real_created_ts_ms": 1000},
        }))
        added, conflicts = ingest_pumpfundata._merge_creation_times(
            self.data_dir, {"MintA": 1000})
        self.assertEqual(added, 0)
        self.assertEqual(conflicts, [])

    def test_disagreeing_existing_value_is_a_conflict_and_is_not_overwritten(self):
        self.cache_path.write_text(json.dumps({
            "MintA": {"status": "ok", "real_created_ts_ms": 1000},
        }))
        added, conflicts = ingest_pumpfundata._merge_creation_times(
            self.data_dir, {"MintA": 9999})
        self.assertEqual(added, 0)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("1000", conflicts[0])
        self.assertIn("9999", conflicts[0])
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual(cache["MintA"]["real_created_ts_ms"], 1000)  # unchanged

    def test_no_new_times_does_not_create_a_file(self):
        added, conflicts = ingest_pumpfundata._merge_creation_times(self.data_dir, {})
        self.assertEqual(added, 0)
        self.assertFalse(self.cache_path.exists())

    def test_multiple_new_mints_all_added(self):
        added, conflicts = ingest_pumpfundata._merge_creation_times(
            self.data_dir, {"MintA": 1000, "MintB": 2000, "MintC": 3000})
        self.assertEqual(added, 3)
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual(set(cache.keys()), {"MintA", "MintB", "MintC"})


class TestMergeCreationTimesIntoCache(unittest.TestCase):
    """D103 (docs/DECISIONS.md): the pure in-memory half of D101's original
    _merge_creation_times -- no file I/O, just a dict merge. This is what
    _flush_batch calls on every flush now; _merge_creation_times itself
    (below) becomes a thin load+this+save-if-changed wrapper."""

    def test_adds_a_new_mint(self):
        cache: dict = {}
        added, conflicts = ingest_pumpfundata._merge_creation_times_into_cache(
            cache, {"MintA": 1000})
        self.assertEqual(added, 1)
        self.assertEqual(conflicts, [])
        self.assertEqual(cache["MintA"], {"status": "ok", "real_created_ts_ms": 1000,
                                           "source": "pumpfundata_create_event"})

    def test_fills_in_a_mint_whose_existing_record_has_no_timestamp(self):
        cache = {"MintA": {"status": "error", "real_created_ts_ms": None}}
        added, conflicts = ingest_pumpfundata._merge_creation_times_into_cache(
            cache, {"MintA": 1000})
        self.assertEqual(added, 1)
        self.assertEqual(cache["MintA"]["real_created_ts_ms"], 1000)

    def test_matching_existing_value_is_not_counted_as_added_or_conflict(self):
        cache = {"MintA": {"status": "ok", "real_created_ts_ms": 1000}}
        added, conflicts = ingest_pumpfundata._merge_creation_times_into_cache(
            cache, {"MintA": 1000})
        self.assertEqual(added, 0)
        self.assertEqual(conflicts, [])

    def test_disagreeing_existing_value_is_a_conflict_and_is_not_overwritten(self):
        cache = {"MintA": {"status": "ok", "real_created_ts_ms": 1000}}
        added, conflicts = ingest_pumpfundata._merge_creation_times_into_cache(
            cache, {"MintA": 9999})
        self.assertEqual(added, 0)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(cache["MintA"]["real_created_ts_ms"], 1000)  # unchanged

    def test_mutates_cache_in_place_and_returns_nothing_about_it(self):
        cache: dict = {}
        ingest_pumpfundata._merge_creation_times_into_cache(cache, {"MintA": 1, "MintB": 2})
        self.assertEqual(set(cache.keys()), {"MintA", "MintB"})


class TestLoadSaveCreationTimesCache(unittest.TestCase):
    """D103 (docs/DECISIONS.md): the explicit disk-I/O half, now called once
    per run (load) and periodically (save) by main(), instead of on every
    single flush."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.cache_path = self.data_dir / "real_creation_times.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_load_returns_empty_dict_when_file_does_not_exist(self):
        self.assertEqual(ingest_pumpfundata._load_creation_times_cache(self.data_dir), {})

    def test_load_returns_existing_contents(self):
        self.cache_path.write_text(json.dumps({"MintA": {"real_created_ts_ms": 1}}))
        cache = ingest_pumpfundata._load_creation_times_cache(self.data_dir)
        self.assertEqual(cache, {"MintA": {"real_created_ts_ms": 1}})

    def test_save_writes_the_whole_cache(self):
        ingest_pumpfundata._save_creation_times_cache(
            self.data_dir, {"MintA": {"real_created_ts_ms": 1}})
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual(cache, {"MintA": {"real_created_ts_ms": 1}})

    def test_save_then_load_round_trips(self):
        original = {"MintA": {"status": "ok", "real_created_ts_ms": 42,
                               "source": "pumpfundata_create_event"}}
        ingest_pumpfundata._save_creation_times_cache(self.data_dir, original)
        self.assertEqual(ingest_pumpfundata._load_creation_times_cache(self.data_dir), original)


class TestFlushBatch(unittest.TestCase):
    """D102 (docs/DECISIONS.md): _flush_batch routes a batch through
    corroborate + write_swaps + merge-creation-times + mark-.ingested, and is
    what main()'s dual-threshold (file-count OR swap-count) loop calls at
    each flush point instead of the old fixed-size _batched() grouping.

    D103 (docs/DECISIONS.md): creation-time merging is now purely in-memory
    here -- `creation_times_cache` is a plain dict (mutated in place) rather
    than a data_dir + skip-flag pair that triggered a full-file load/save on
    every call. main() owns loading/persisting the on-disk cache."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_empty_batch_does_nothing(self):
        store = FakeStore(pd.DataFrame(columns=["sig", "side", "base_amount",
                                                  "quote_amount", "source"]))
        result = ingest_pumpfundata._flush_batch(store, [], [], {}, {})
        self.assertEqual(store.written_batches, [])
        self.assertEqual(result["creation_times_added"], 0)

    def test_nonempty_batch_corroborates_and_writes(self):
        store = FakeStore(pd.DataFrame(columns=["sig", "side", "base_amount",
                                                  "quote_amount", "source"]))
        swaps = [_swap("Sig1"), _swap("Sig2")]
        result = ingest_pumpfundata._flush_batch(store, swaps, [], {}, {})
        self.assertEqual(len(store.written_batches), 1)
        self.assertEqual(len(store.written_batches[0]), 2)
        self.assertEqual(result["corroboration"]["checked"], 0)

    def test_marks_every_path_in_the_batch_as_ingested(self):
        store = FakeStore(pd.DataFrame(columns=["sig", "side", "base_amount",
                                                  "quote_amount", "source"]))
        f1 = self.data_dir / "hour=01.parquet"
        f2 = self.data_dir / "hour=02.parquet"
        f1.write_bytes(b"")
        f2.write_bytes(b"")
        ingest_pumpfundata._flush_batch(store, [_swap("Sig1")], [f1, f2], {}, {})
        self.assertTrue(f1.with_name(f1.name + ".ingested").exists())
        self.assertTrue(f2.with_name(f2.name + ".ingested").exists())

    def test_creation_times_merged_into_cache_unless_skipped(self):
        store = FakeStore(pd.DataFrame(columns=["sig", "side", "base_amount",
                                                  "quote_amount", "source"]))
        cache: dict = {}
        result = ingest_pumpfundata._flush_batch(store, [], [], {"MintA": 1000}, cache)
        self.assertEqual(result["creation_times_added"], 1)
        self.assertEqual(cache["MintA"]["real_created_ts_ms"], 1000)
        # D103: _flush_batch itself never touches disk.
        self.assertFalse((self.data_dir / "real_creation_times.json").exists())

    def test_creation_times_skipped_when_cache_is_none(self):
        store = FakeStore(pd.DataFrame(columns=["sig", "side", "base_amount",
                                                  "quote_amount", "source"]))
        result = ingest_pumpfundata._flush_batch(store, [], [], {"MintA": 1000}, None)
        self.assertEqual(result["creation_times_added"], 0)

    def test_store_none_skips_corroborate_and_write_even_with_swaps(self):
        # D104 (docs/DECISIONS.md): --creation-times-only passes store=None
        # so a backfill-only run never pays _corroborate()'s real, measured
        # 141.2s-per-flush full-corpus-scan cost (or write_swaps()'s, though
        # that one was cheap) for files already corroborated/written in a
        # prior run.
        swaps = [_swap("Sig1"), _swap("Sig2")]
        cache: dict = {}
        result = ingest_pumpfundata._flush_batch(None, swaps, [], {"MintA": 1000}, cache)
        self.assertEqual(result["corroboration"]["checked"], 0)
        self.assertEqual(result["corroboration"]["disagreements"], [])
        # Creation-time merging still happens -- that's the whole point of
        # --creation-times-only.
        self.assertEqual(result["creation_times_added"], 1)
        self.assertEqual(cache["MintA"]["real_created_ts_ms"], 1000)

    def test_store_none_does_not_mark_ingested_itself(self):
        # D105 (docs/DECISIONS.md): with store=None, _flush_batch must NOT
        # write any marker itself -- main() owns marking
        # --creation-times-only progress, and only once the creation-times
        # cache has actually been persisted to disk (not merely merged in
        # memory here). Marking at flush time, before that persist, would
        # let a crash between the two silently lose a file's creation times
        # while the next run believes it already done.
        f1 = self.data_dir / "hour=01.parquet"
        f1.write_bytes(b"")
        ingest_pumpfundata._flush_batch(None, [_swap("Sig1")], [f1], {}, {})
        self.assertFalse(f1.with_name(f1.name + ".ingested").exists())
        self.assertFalse(f1.with_name(
            f1.name + ingest_pumpfundata.CREATION_TIMES_MARKER_SUFFIX).exists())

    def test_caller_accumulators_can_be_reset_after_flush(self):
        # _flush_batch itself never mutates its list/dict arguments in
        # place -- the caller (main()'s _flush_now closure) is the one that
        # resets its own batch_swaps/batch_ok_paths/batch_creation_times to
        # fresh containers after each flush. Pin that _flush_batch doesn't
        # secretly rely on being handed the SAME container back empty.
        store = FakeStore(pd.DataFrame(columns=["sig", "side", "base_amount",
                                                  "quote_amount", "source"]))
        swaps_batch_1 = [_swap("Sig1")]
        ingest_pumpfundata._flush_batch(store, swaps_batch_1, [], {}, {})
        swaps_batch_2 = [_swap("Sig2")]
        ingest_pumpfundata._flush_batch(store, swaps_batch_2, [], {}, {})
        self.assertEqual(len(store.written_batches), 2)
        self.assertEqual(store.written_batches[0][0].sig, "Sig1")
        self.assertEqual(store.written_batches[1][0].sig, "Sig2")


if __name__ == "__main__":
    unittest.main()
