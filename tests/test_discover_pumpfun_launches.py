"""Tests for scripts/discover_pumpfun_launches.py's pure logic -- the D60-D62
follow-up production discovery tool. Same importlib-by-path loading as
tests/test_information_audit.py (scripts/ has no __init__.py).
"""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "discover_pumpfun_launches", ROOT / "scripts" / "discover_pumpfun_launches.py")
dpl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dpl)


# ---------------------------------------------------------------------------
# A. extract_mint -- matches by NAME (case-insensitive), never a fixed index.
# ---------------------------------------------------------------------------

class TestExtractMint(unittest.TestCase):
    def test_matches_mint_by_name_not_position(self):
        accounts = [{"Address": "userAddr"}, {"Address": "mintAddr"}, {"Address": "otherAddr"}]
        names = ["user", "mint", "bonding_curve"]
        self.assertEqual(dpl.extract_mint(accounts, names), "mintAddr")

    def test_case_insensitive(self):
        accounts = [{"Address": "mintAddr"}]
        names = ["MINT"]
        self.assertEqual(dpl.extract_mint(accounts, names), "mintAddr")

    def test_no_mint_name_returns_none(self):
        accounts = [{"Address": "a"}, {"Address": "b"}]
        names = ["user", "bonding_curve"]
        self.assertIsNone(dpl.extract_mint(accounts, names))

    def test_misaligned_lists_returns_none_not_wrong_answer(self):
        accounts = [{"Address": "a"}]
        names = ["user", "mint"]  # mint at index 1, but accounts only has index 0
        self.assertIsNone(dpl.extract_mint(accounts, names))

    def test_empty_inputs_return_none(self):
        self.assertIsNone(dpl.extract_mint([], []))
        self.assertIsNone(dpl.extract_mint(None, None))


# ---------------------------------------------------------------------------
# B. parse_row -- requires ALL of mint/ts/sig; never guesses a partial result.
# ---------------------------------------------------------------------------

class TestParseRow(unittest.TestCase):
    def test_full_row_parses(self):
        row = {
            "Block": {"Time": "2026-09-22T12:26:10.000Z"},
            "Transaction": {"Signature": "sig123"},
            "Instruction": {
                "Accounts": [{"Address": "mintAddr"}],
                "Program": {"AccountNames": ["mint"]},
            },
        }
        parsed = dpl.parse_row(row)
        self.assertEqual(parsed["mint"], "mintAddr")
        self.assertEqual(parsed["sig"], "sig123")
        self.assertIsInstance(parsed["ts_ms"], int)

    def test_missing_mint_returns_none(self):
        row = {
            "Block": {"Time": "2026-09-22T12:26:10.000Z"},
            "Transaction": {"Signature": "sig123"},
            "Instruction": {"Accounts": [], "Program": {"AccountNames": []}},
        }
        self.assertIsNone(dpl.parse_row(row))

    def test_missing_signature_returns_none(self):
        row = {
            "Block": {"Time": "2026-09-22T12:26:10.000Z"},
            "Transaction": {},
            "Instruction": {
                "Accounts": [{"Address": "mintAddr"}],
                "Program": {"AccountNames": ["mint"]},
            },
        }
        self.assertIsNone(dpl.parse_row(row))


# ---------------------------------------------------------------------------
# C. dedupe_keep_earliest -- D60's multi-quote-currency finding: same mint,
# multiple create_v2 events, keep the earliest.
# ---------------------------------------------------------------------------

class TestDedupeKeepEarliest(unittest.TestCase):
    def test_keeps_earliest_of_duplicate_mint(self):
        records = [
            {"mint": "m1", "ts_ms": 3000, "sig": "late"},
            {"mint": "m1", "ts_ms": 1000, "sig": "early"},
            {"mint": "m1", "ts_ms": 2000, "sig": "middle"},
            {"mint": "m2", "ts_ms": 500, "sig": "other"},
        ]
        result = dpl.dedupe_keep_earliest(records)
        self.assertEqual(set(result.keys()), {"m1", "m2"})
        self.assertEqual(result["m1"]["sig"], "early")
        self.assertEqual(result["m1"]["ts_ms"], 1000)
        self.assertEqual(result["m2"]["sig"], "other")

    def test_empty_input(self):
        self.assertEqual(dpl.dedupe_keep_earliest([]), {})


# ---------------------------------------------------------------------------
# D. merge_into_cache -- never overwrites an existing "ok" entry, from ANY
# source; fills in missing/error/implausible ones and wholly new mints.
# ---------------------------------------------------------------------------

class TestMergeIntoCache(unittest.TestCase):
    def test_new_mint_is_added(self):
        cache = {}
        new = {"m1": {"mint": "m1", "ts_ms": 1000, "sig": "s1"}}
        cache, added, skipped = dpl.merge_into_cache(cache, new, now_ms=9999)
        self.assertEqual(added, 1)
        self.assertEqual(skipped, 0)
        self.assertEqual(cache["m1"]["status"], "ok")
        self.assertEqual(cache["m1"]["real_created_ts_ms"], 1000)
        self.assertEqual(cache["m1"]["source"], "bitquery_create_v2")

    def test_existing_ok_entry_from_any_source_is_never_overwritten(self):
        cache = {"m1": {"status": "ok", "real_created_ts_ms": 42, "source": "pumpfun_api"}}
        new = {"m1": {"mint": "m1", "ts_ms": 999, "sig": "s1"}}
        cache, added, skipped = dpl.merge_into_cache(cache, new, now_ms=9999)
        self.assertEqual(added, 0)
        self.assertEqual(skipped, 1)
        self.assertEqual(cache["m1"]["real_created_ts_ms"], 42)
        self.assertEqual(cache["m1"]["source"], "pumpfun_api")

    def test_existing_error_entry_is_fixed_not_skipped(self):
        cache = {"m1": {"status": "error", "real_created_ts_ms": None}}
        new = {"m1": {"mint": "m1", "ts_ms": 1000, "sig": "s1"}}
        cache, added, skipped = dpl.merge_into_cache(cache, new, now_ms=9999)
        self.assertEqual(added, 1)
        self.assertEqual(skipped, 0)
        self.assertEqual(cache["m1"]["status"], "ok")
        self.assertEqual(cache["m1"]["real_created_ts_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
