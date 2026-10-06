"""D121: collected-hour mask for store v2 (partial vendor files are not 'collected')."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import explore_facts_v2 as ef2  # noqa: E402
from tape import pf_store as ps  # noqa: E402


class TestCollectedHours(unittest.TestCase):
    def test_partial_and_skipped_files_excluded(self):
        rows = [{"date": "2026-02-26", "hour": h, "status": "boundary_noise", "n_swaps": 100_000}
                for h in (0, 2, 4, 6)]
        rows.append({"date": "2026-02-26", "hour": 15, "status": "ok", "n_swaps": 9_474})
        rows.append({"date": "2026-02-26", "hour": 8, "status": "ts_suspect", "n_swaps": 100_000})
        hours, partial = ef2.collected_hours(rows, 0.25)
        lo = ps.hour_window_ms("2026-02-26", 0)[0] // ps.HOUR_MS
        self.assertEqual(hours, {lo, lo + 2, lo + 4, lo + 6})
        self.assertEqual([(p["hour"], p["n_swaps"]) for p in partial], [(15, 9_474)])

    def test_median_is_per_day(self):
        rows = [{"date": "2026-08-12", "hour": h, "status": "ok", "n_swaps": 4_000} for h in (0, 2)]
        rows += [{"date": "2026-02-26", "hour": h, "status": "ok", "n_swaps": 100_000} for h in (0, 2)]
        hours, partial = ef2.collected_hours(rows, 0.25)
        self.assertEqual(len(hours), 4)
        self.assertEqual(partial, [])


if __name__ == "__main__":
    unittest.main()
