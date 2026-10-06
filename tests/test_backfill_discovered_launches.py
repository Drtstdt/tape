"""Tests for scripts/backfill_discovered_launches.py's pure logic (D63
follow-up). Same importlib-by-path loading as tests/test_information_audit.py.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "backfill_discovered_launches", ROOT / "scripts" / "backfill_discovered_launches.py")
bdl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bdl)


# ---------------------------------------------------------------------------
# A. spread_sample -- every Nth mint BY CREATION TIME, never by outcome (D18).
# ---------------------------------------------------------------------------

class TestSpreadSample(unittest.TestCase):
    def test_returns_all_when_under_target(self):
        mints = [(f"m{i}", i) for i in range(10)]
        self.assertEqual(bdl.spread_sample(mints, 300), mints)

    def test_downsamples_to_roughly_target_count(self):
        mints = [(f"m{i}", i * 1000) for i in range(1000)]
        sample = bdl.spread_sample(mints, 100)
        self.assertLessEqual(len(sample), 105)  # rounding slack, never wildly over
        self.assertGreaterEqual(len(sample), 95)

    def test_sample_spans_the_full_range_not_just_the_start(self):
        """A bug that only took the FIRST target_count mints would silently
        reintroduce D56's narrow-window problem -- explicitly checked here."""
        mints = [(f"m{i}", i * 1000) for i in range(1000)]
        sample = bdl.spread_sample(mints, 100)
        first_ts = sample[0][1]
        last_ts = sample[-1][1]
        self.assertLess(first_ts, 50_000, "sample should include early mints")
        self.assertGreater(last_ts, 900_000, "sample should include late mints, "
                                              "not stop short of the full range")

    def test_zero_or_negative_target_returns_everything(self):
        mints = [(f"m{i}", i) for i in range(5)]
        self.assertEqual(bdl.spread_sample(mints, 0), mints)


# ---------------------------------------------------------------------------
# B. load_discovered_mints -- only source=="bitquery_create_v2" AND
# status=="ok" entries count; sorted by creation time.
# ---------------------------------------------------------------------------

class TestLoadDiscoveredMints(unittest.TestCase):
    def _write_cache(self, cache: dict) -> Path:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(cache, tmp)
        tmp.close()
        return Path(tmp.name)

    def test_filters_to_bitquery_create_v2_ok_entries_only(self):
        cache = {
            "new1": {"status": "ok", "real_created_ts_ms": 2000, "source": "bitquery_create_v2"},
            "new2": {"status": "ok", "real_created_ts_ms": 1000, "source": "bitquery_create_v2"},
            "old_survivor": {"status": "ok", "real_created_ts_ms": 500, "source": "pumpfun_api"},
            "broken": {"status": "error", "real_created_ts_ms": None, "source": "bitquery_create_v2"},
        }
        path = self._write_cache(cache)
        result = bdl.load_discovered_mints(path)
        self.assertEqual([m for m, _ in result], ["new2", "new1"])  # sorted by ts

    def test_empty_cache_returns_empty_list(self):
        path = self._write_cache({})
        self.assertEqual(bdl.load_discovered_mints(path), [])


if __name__ == "__main__":
    unittest.main()
