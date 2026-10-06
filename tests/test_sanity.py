"""D95 (docs/DECISIONS.md): tests for tape/sanity.py's filter_post_migration_swaps.

No existing test file covered tape/sanity.py before this (filter_implausible_swaps,
D79, had only indirect coverage via tests/test_paper_trade_replay.py's reason-code
check). This adds direct coverage for the new migration cut, which was added after
real evidence (D94/D95) that the online policy was entering trades on mints whose
only captured data was post-migration PumpSwap activity, and losing on most of them.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from tape.sanity import PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM, filter_post_migration_swaps


@dataclass
class FakeSwap:
    venue: str
    ts_ms: int


class TestFilterPostMigrationSwaps(unittest.TestCase):
    def test_no_pumpswap_touch_keeps_everything(self):
        swaps = [FakeSwap(PUMPFUN_PROGRAM, i) for i in range(5)]
        kept, dropped = filter_post_migration_swaps(swaps)
        self.assertEqual(kept, swaps)
        self.assertEqual(dropped, [])

    def test_cuts_at_first_pumpswap_touching_swap(self):
        swaps = [
            FakeSwap(PUMPFUN_PROGRAM, 1000),
            FakeSwap(PUMPFUN_PROGRAM, 60000),
            FakeSwap(f"{PUMPFUN_PROGRAM}+{PUMPSWAP_PROGRAM}", 300000),  # the migration tx itself
            FakeSwap(PUMPSWAP_PROGRAM, 400000),
        ]
        kept, dropped = filter_post_migration_swaps(swaps)
        self.assertEqual([s.ts_ms for s in kept], [1000, 60000])
        self.assertEqual([s.ts_ms for s in dropped], [300000, 400000])

    def test_mint_with_only_post_migration_history_drops_everything(self):
        """The real D95 case: backfill never captured this mint's bonding-curve
        trading at all -- its first-ever observed swap already touches PumpSwap."""
        swaps = [FakeSwap(f"{PUMPFUN_PROGRAM}+{PUMPSWAP_PROGRAM}", 500),
                 FakeSwap(PUMPSWAP_PROGRAM, 600)]
        kept, dropped = filter_post_migration_swaps(swaps)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, swaps)

    def test_empty_input_returns_empty(self):
        kept, dropped = filter_post_migration_swaps([])
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [])

    def test_unrelated_venue_is_not_treated_as_migration(self):
        swaps = [FakeSwap("some_other_router_program", i) for i in range(3)]
        kept, dropped = filter_post_migration_swaps(swaps)
        self.assertEqual(kept, swaps)
        self.assertEqual(dropped, [])


if __name__ == "__main__":
    unittest.main()
