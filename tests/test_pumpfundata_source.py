"""D98 (docs/DECISIONS.md): tests for tape/sources/pumpfundata.py.

Covers the parts that don't need pandas+pyarrow to read an actual Parquet
file (`_row_to_swap`, `_parse_timestamp_ms`, `_expected_hour_from_path`) --
this sandbox cannot install pyarrow (same restriction D93 hit), so
`load_file`/`iter_raw_dir` themselves are NOT exercised here. The user must
run `scripts/ingest_pumpfundata.py --self-check <a real downloaded file>`
on their own machine (where pyarrow is a real, already-installed project
dependency) to validate the full read path end-to-end.
"""

from __future__ import annotations

import unittest

from tape.schema import BUY, SELL, CanonicalSwap
from tape.sources.pumpfundata import (
    LAMPORTS_PER_SOL,
    PUMPFUN_PROGRAM,
    TOKEN_DECIMALS,
    SchemaMismatch,
    _check_timestamps_against_hour,
    _creation_record_from_row,
    _expected_hour_from_path,
    _parse_timestamp_ms,
    _row_to_swap,
)

# D93's real, verified sample values.
REAL_CREATE_VIRTUAL_TOKEN_RESERVE = 1_073_000_000_000_000
REAL_CREATE_VIRTUAL_LAMPORTS_RESERVE = 30_000_000_000
REAL_TOKEN_TOTAL_SUPPLY = 1_000_000_000_000_000


def _swap_row(**overrides) -> dict:
    row = {
        "event_type": "swap",
        "token_mint": "ExampleMint111111111111111111111111111111",
        "slot_number": 123456789,
        "signature": "ExampleSig1111111111111111111111111111111111111111111111111111111",
        "timestamp": 1_770_000_000,  # seconds-epoch, plausible 2026 value
        "token_creator": "CreatorWallet111111111111111111111111111",
        "virtual_token_reserve": REAL_CREATE_VIRTUAL_TOKEN_RESERVE - 1_000_000,
        "virtual_lamports_reserve": REAL_CREATE_VIRTUAL_LAMPORTS_RESERVE + 500_000_000,
        "real_token_reserve": 793_100_000_000_000 - 1_000_000,
        "real_lamports_reserve": 500_000_000,
        "action": "buy",
        "token_amount": 1_000_000,       # 1.0 token at 6 decimals
        "lamports_amount": 500_000_000,  # 0.5 SOL
        "fee_lamports": 1_000_000,
        "user_wallet": "WalletAbc1111111111111111111111111111111",
        "token_total_supply": None,
        "is_mayhem_mode": False,
    }
    row.update(overrides)
    return row


class TestParseTimestampMs(unittest.TestCase):
    def test_seconds_epoch(self):
        # 2026-02-08T00:00:00Z == 1770508800
        self.assertEqual(_parse_timestamp_ms(1_770_508_800), 1_770_508_800_000)

    def test_milliseconds_epoch(self):
        self.assertEqual(_parse_timestamp_ms(1_770_508_800_000), 1_770_508_800_000)

    def test_microseconds_epoch(self):
        self.assertEqual(_parse_timestamp_ms(1_770_508_800_000_000), 1_770_508_800_000)

    def test_none_and_zero_return_none(self):
        self.assertIsNone(_parse_timestamp_ms(None))
        self.assertIsNone(_parse_timestamp_ms(0))

    def test_python_datetime(self):
        import datetime
        dt = datetime.datetime(2026, 2, 8, 0, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(_parse_timestamp_ms(dt), 1_770_508_800_000)


class TestRowToSwap(unittest.TestCase):
    def test_buy_row_scales_units_correctly(self):
        swap = _row_to_swap(_swap_row())
        self.assertIsNotNone(swap)
        self.assertEqual(swap.side, BUY)
        self.assertAlmostEqual(swap.base_amount, 1.0)   # 1_000_000 / 10**6
        self.assertAlmostEqual(swap.quote_amount, 0.5)  # 500_000_000 / 1e9
        self.assertAlmostEqual(swap.price, 0.5)
        self.assertEqual(swap.venue, PUMPFUN_PROGRAM)

    def test_sell_row(self):
        swap = _row_to_swap(_swap_row(action="sell"))
        self.assertEqual(swap.side, SELL)

    def test_reserve_fields_scaled_to_match_helius_convention(self):
        swap = _row_to_swap(_swap_row(
            virtual_token_reserve=REAL_CREATE_VIRTUAL_TOKEN_RESERVE,
            virtual_lamports_reserve=REAL_CREATE_VIRTUAL_LAMPORTS_RESERVE,
        ))
        # D93: 1,073,000,000,000,000 raw / 10**6 = 1,073,000,000.0 UI tokens
        self.assertAlmostEqual(swap.base_reserve_after, 1_073_000_000.0)
        # 30,000,000,000 raw lamports / 1e9 = 30.0 SOL
        self.assertAlmostEqual(swap.quote_reserve_after, 30.0)

    def test_missing_signature_returns_none(self):
        self.assertIsNone(_row_to_swap(_swap_row(signature=None)))

    def test_missing_mint_returns_none(self):
        self.assertIsNone(_row_to_swap(_swap_row(token_mint=None)))

    def test_unrecognized_action_returns_none_not_a_guess(self):
        self.assertIsNone(_row_to_swap(_swap_row(action="transfer")))

    def test_unparsable_timestamp_returns_none(self):
        self.assertIsNone(_row_to_swap(_swap_row(timestamp="not-a-date")))

    def test_missing_reserve_fields_become_none_not_zero(self):
        swap = _row_to_swap(_swap_row(virtual_token_reserve=None, virtual_lamports_reserve=None))
        self.assertIsNone(swap.base_reserve_after)
        self.assertIsNone(swap.quote_reserve_after)

    def test_zero_token_amount_gives_zero_price_not_a_crash(self):
        swap = _row_to_swap(_swap_row(token_amount=0))
        self.assertEqual(swap.price, 0.0)


def _create_row(**overrides) -> dict:
    row = {
        "event_type": "create",
        "token_mint": "ExampleMint111111111111111111111111111111",
        "timestamp": 1_770_000_000,  # seconds-epoch, plausible 2026 value
        "token_total_supply": REAL_TOKEN_TOTAL_SUPPLY,
    }
    row.update(overrides)
    return row


class TestCreationRecordFromRow(unittest.TestCase):
    """D101 (docs/DECISIONS.md): the vendor's own ground-truth launch
    timestamp, extracted from `create` rows load_file previously only read
    for the token_total_supply sanity check and then discarded."""

    def test_valid_create_row_returns_mint_and_ts_ms(self):
        rec = _creation_record_from_row(_create_row())
        self.assertEqual(rec, ("ExampleMint111111111111111111111111111111",
                               1_770_000_000_000))

    def test_missing_mint_returns_none(self):
        self.assertIsNone(_creation_record_from_row(_create_row(token_mint=None)))

    def test_unparsable_timestamp_returns_none(self):
        self.assertIsNone(_creation_record_from_row(_create_row(timestamp="not-a-date")))

    def test_missing_timestamp_returns_none(self):
        self.assertIsNone(_creation_record_from_row(_create_row(timestamp=None)))


def _swap_at(ts_ms: int, sig: str = "Sig1111111111111111111111111111111111111111111111111111111111") -> CanonicalSwap:
    """A minimal valid CanonicalSwap with only `ts_ms`/`sig` controlled --
    everything else is an arbitrary-but-valid filler value, since
    _check_timestamps_against_hour only looks at ts_ms (and uses sig in its
    error/note text)."""
    return CanonicalSwap(
        mint="ExampleMint111111111111111111111111111111",
        venue=PUMPFUN_PROGRAM,
        pool="CreatorWallet111111111111111111111111111",
        ts_ms=ts_ms,
        slot=123456789,
        sig=sig,
        side=BUY,
        base_amount=1.0,
        quote_amount=0.5,
        quote_mint="So11111111111111111111111111111111111111112",
        price=0.5,
        source="pumpfundata",
    )


class TestCheckTimestampsAgainstHour(unittest.TestCase):
    """D98/D99 follow-up: the real downloaded hour=06 file had 177/85,472
    (0.21%) timestamps fall outside the filename's hour window, worst case
    only 6 seconds early -- vendor hour-boundary bucketing noise, not a
    wrong _parse_timestamp_ms guess (which would be off by hours/decades,
    not seconds). These tests pin that real scenario to NOTE-and-keep, and
    confirm a genuinely-wrong-encoding-shaped mismatch still raises.

    D106 (docs/DECISIONS.md): a real, very-low-volume hour (1134 total
    swaps) hit MAX_BOUNDARY_FRACTION (45/1134 = 3.97%) despite having FEWER
    absolute bad rows than the already-accepted D98 case above (177), purely
    because its own total was tiny -- a fixed percentage is noisy on a small
    sample. _check_timestamps_against_hour now also requires
    MIN_BOUNDARY_BAD_COUNT absolute bad rows before the fraction alone can
    raise; the offset check (MAX_BOUNDARY_SECONDS) is unaffected."""

    def setUp(self):
        # hour=06 on 2026-02-08 UTC, same window load_file/_expected_hour_from_path
        # computed for the user's real file.
        self.lo_ms = 1_770_530_400_000
        self.hi_ms = 1_770_534_000_000
        self.expected = (self.lo_ms, self.hi_ms)

    def test_all_timestamps_inside_window_returns_none(self):
        swaps = [_swap_at(self.lo_ms), _swap_at(self.lo_ms + 1_800_000),
                  _swap_at(self.hi_ms - 1)]
        self.assertIsNone(_check_timestamps_against_hour(swaps, self.expected, "irrelevant"))

    def test_real_boundary_noise_scenario_warns_and_keeps(self):
        # Mirror the real report: 177 bad out of 85,472 total, worst offender
        # 6 seconds before the window opens (ts_ms=1770530394000, i.e.
        # lo_ms - 6000). Scaled down for test speed but same fraction shape.
        n_total = 854
        n_bad = 1  # 1/854 ~= 0.12%, well under MAX_BOUNDARY_FRACTION (2%)
        swaps = [_swap_at(self.lo_ms - 6_000, sig=f"Sig{0}")]
        swaps += [_swap_at(self.lo_ms + i * 1000) for i in range(1, n_total - n_bad + 1)]
        note = _check_timestamps_against_hour(swaps, self.expected, "hour=06.parquet")
        self.assertIsNotNone(note)
        self.assertIn("NOTE", note)
        self.assertIn("1/854", note)
        self.assertIn("6s", note)
        # Must not raise -- this is the whole point of the D98/D99 fix.

    def test_large_fraction_outside_window_raises(self):
        # 15% of rows outside the window -- well over MAX_BOUNDARY_FRACTION
        # (2%) -- AND well over MIN_BOUNDARY_BAD_COUNT (100) in absolute
        # terms (D106), so this is a real, statistically-meaningful mismatch,
        # not small-sample noise.
        swaps = [_swap_at(self.lo_ms - 10_000) for _ in range(150)]
        swaps += [_swap_at(self.lo_ms + 1000) for _ in range(850)]
        with self.assertRaises(SchemaMismatch):
            _check_timestamps_against_hour(swaps, self.expected, "hour=06.parquet")

    def test_low_volume_hour_high_fraction_but_few_bad_rows_warns_not_raises(self):
        # D106 (docs/DECISIONS.md): the real evidence that prompted this fix
        # -- a quiet hour with only 1134 total swaps, 45 of them (3.97%)
        # outside the window, worst by 121s. 45 is FEWER absolute bad rows
        # than the already-accepted D98 case (177/85472) above, and 121s is
        # nowhere near MAX_BOUNDARY_SECONDS (300s) -- the real signature of a
        # wrong-unit encoding bug is hours/days of drift, not two minutes.
        # Must NOT raise -- it's exactly the same kind of boundary noise as
        # D98's case, just a higher percentage because the sample is tiny.
        n_total = 1134
        n_bad = 45
        swaps = [_swap_at(self.lo_ms - 121_000) for _ in range(n_bad)]
        swaps += [_swap_at(self.lo_ms + i * 1000) for i in range(n_total - n_bad)]
        note = _check_timestamps_against_hour(swaps, self.expected, "hour=02.parquet")
        self.assertIsNotNone(note)
        self.assertIn("NOTE", note)
        self.assertIn("45/1134", note)
        self.assertIn("121s", note)

    def test_note_and_raise_messages_include_offset_distribution(self):
        # D107 (docs/DECISIONS.md): a real case (1226/60888, worst 72s) sat
        # right at the fraction edge with a worst offset far larger than
        # every other real normal-volume noise sample seen so far (all
        # 2-6s) -- the single worst-case number alone couldn't say whether
        # that was one rare straggler or representative of the whole bad
        # set. Both messages now report median/p90/max, not just max.
        # Mixed offsets: nine rows at 2s, one row at 72s -- median should
        # stay low (2s) even though max is 72s, showing the straggler is an
        # outlier, not the norm.
        swaps = [_swap_at(self.lo_ms - 2_000) for _ in range(9)]
        swaps += [_swap_at(self.lo_ms - 72_000)]
        swaps += [_swap_at(self.lo_ms + i * 1000) for i in range(990)]
        note = _check_timestamps_against_hour(swaps, self.expected, "hour=07.parquet")
        self.assertIsNotNone(note)
        self.assertIn("median=2s", note)
        self.assertIn("max=72s", note)

    def test_fraction_with_few_absolute_bad_rows_but_wild_offset_still_raises(self):
        # D106's absolute-count floor must NOT weaken the offset check: even
        # with very few bad rows (well under MIN_BOUNDARY_BAD_COUNT), a
        # wildly-off timestamp is still real evidence of a wrong encoding and
        # must still raise.
        wildly_off_ts = self.lo_ms - (365 * 24 * 3600 * 1000)  # ~1 year early
        swaps = [_swap_at(wildly_off_ts)]
        swaps += [_swap_at(self.lo_ms + 1000) for _ in range(9)]
        with self.assertRaises(SchemaMismatch):
            _check_timestamps_against_hour(swaps, self.expected, "hour=06.parquet")

    def test_single_wildly_off_timestamp_raises_even_if_rare(self):
        # Only 1/100 bad (well under the fraction threshold) but it's off by
        # years, not seconds -- shaped like a genuine wrong-unit parsing bug,
        # not boundary noise, so this must still raise.
        wildly_off_ts = self.lo_ms - (365 * 24 * 3600 * 1000)  # ~1 year early
        swaps = [_swap_at(wildly_off_ts)]
        swaps += [_swap_at(self.lo_ms + 1000) for _ in range(99)]
        with self.assertRaises(SchemaMismatch):
            _check_timestamps_against_hour(swaps, self.expected, "hour=06.parquet")

    def test_empty_swaps_list_returns_none(self):
        self.assertIsNone(_check_timestamps_against_hour([], self.expected, "irrelevant"))


class TestExpectedHourFromPath(unittest.TestCase):
    def test_parses_a_well_formed_path(self):
        from pathlib import Path
        lo, hi = _expected_hour_from_path(
            Path("F:/pumpfundata/pump_fun/date=2026-02-08/hour=06.parquet"))
        self.assertEqual(hi - lo, 3_600_000)
        # hour=06 on 2026-02-08 UTC
        self.assertEqual(lo, 1_770_530_400_000)

    def test_unrecognized_layout_returns_none(self):
        from pathlib import Path
        self.assertIsNone(_expected_hour_from_path(Path("some/other/file.parquet")))


if __name__ == "__main__":
    unittest.main()
