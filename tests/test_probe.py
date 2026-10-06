import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape.scripts.probe import (
    load_tape, bucket_tape_windows, _reserve_price, _parse_block_time_ms,
    parse_bitquery_trade, bucket_bitquery_windows, diff_windows, summarize,
    ProbeSwap,
)


SAMPLE_TAPE_LINES = [
    # txCount=0, no attributed volume yet -- must be dropped, not zeroed.
    '{"mintAddress":"m","timestamp":1000,"price":1.0e-6,'
    '"virtualSolReserves":"100000000000","virtualTokenReserves":"1000000000000000",'
    '"buyVolumeSol":null,"sellVolumeSol":null,"swapCountInWindow":null,"txCount":0}',
    # a real window with attributed volume
    '{"mintAddress":"m","timestamp":11000,"price":1.05e-6,'
    '"virtualSolReserves":"105000000000","virtualTokenReserves":"1000000000000000",'
    '"buyVolumeSol":5.0,"sellVolumeSol":2.0,"swapCountInWindow":3,"txCount":4}',
    # another real window
    '{"mintAddress":"m","timestamp":21000,"price":1.10e-6,'
    '"virtualSolReserves":"110000000000","virtualTokenReserves":"1000000000000000",'
    '"buyVolumeSol":1.0,"sellVolumeSol":1.0,"swapCountInWindow":2,"txCount":2}',
]


class TestLoadTape(unittest.TestCase):
    def test_parses_valid_jsonl_and_skips_garbage_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mint.jsonl"
            p.write_text("\n".join(SAMPLE_TAPE_LINES) + "\nNOT JSON\n\n")
            rows = load_tape(p)
            self.assertEqual(len(rows), 3)


class TestReservePrice(unittest.TestCase):
    def test_computes_sol_per_token_from_virtual_reserves(self):
        row = {"virtualSolReserves": "100000000000", "virtualTokenReserves": "1000000000000000"}
        self.assertAlmostEqual(_reserve_price(row), 100000000000 / 1000000000000000)

    def test_missing_reserve_returns_none_not_zero(self):
        self.assertIsNone(_reserve_price({"virtualSolReserves": None, "virtualTokenReserves": "1"}))

    def test_zero_token_reserve_returns_none_not_a_crash(self):
        self.assertIsNone(_reserve_price({"virtualSolReserves": "1", "virtualTokenReserves": "0"}))


class TestBucketTapeWindows(unittest.TestCase):
    def test_drops_null_volume_windows_instead_of_zeroing_them(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mint.jsonl"
            p.write_text("\n".join(SAMPLE_TAPE_LINES))
            rows = load_tape(p)
            windows = bucket_tape_windows(rows)
            # 3 rows in, only 2 have non-null buy/sell volume
            self.assertEqual(len(windows), 2)
            self.assertNotIn(0, windows)   # the txCount=0 / null-volume window

    def test_window_bucketing_matches_the_10s_grid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mint.jsonl"
            p.write_text("\n".join(SAMPLE_TAPE_LINES))
            windows = bucket_tape_windows(load_tape(p))
            self.assertIn(10000, windows)   # ts=11000 -> window start 10000
            self.assertIn(20000, windows)   # ts=21000 -> window start 20000
            self.assertEqual(windows[10000]["buy_sol"], 5.0)
            self.assertEqual(windows[10000]["sell_sol"], 2.0)


class TestParseBlockTime(unittest.TestCase):
    def test_parses_z_suffixed_utc(self):
        ms = _parse_block_time_ms("2026-09-20T12:00:00Z")
        self.assertIsNotNone(ms)

    def test_none_on_garbage_input_not_a_crash_or_a_zero(self):
        self.assertIsNone(_parse_block_time_ms("not a timestamp"))
        self.assertIsNone(_parse_block_time_ms(""))
        self.assertIsNone(_parse_block_time_ms(None))


class TestParseBitqueryTrade(unittest.TestCase):
    """D20 (docs/DECISIONS.md): `DEXTradeByTokens` shape, not `DEXTrades`
    Buy/Sell -- the schema's `Trade` where-filter has no `Buy`/`Sell` field at
    all, confirmed by live introspection. Side comes from `Trade.Side.Type`;
    the SOL amount is `Trade.Side.Amount`, unambiguous because the query
    filters `Trade.Side.Currency` to SOL_MINT.
    """

    def _raw(self, side_type="buy", side_amount="2.5", price=0.001):
        return {
            "Block": {"Time": "2026-09-20T00:00:11Z"},
            "Transaction": {"Signature": "sig123"},
            "Trade": {
                "Currency": {"MintAddress": "m", "Decimals": 6},
                "Side": {"Type": side_type, "Amount": side_amount,
                         "AmountInUSD": 1.23},
                "Amount": "1000.0",
                "Price": price,
            },
        }

    def test_labels_a_buy_from_side_type(self):
        swap = parse_bitquery_trade(self._raw(side_type="buy", side_amount="2.5"), mint="m")
        self.assertIsNotNone(swap)
        self.assertEqual(swap.side, "buy")
        self.assertEqual(swap.sol_amount, 2.5)

    def test_labels_a_sell_from_side_type(self):
        swap = parse_bitquery_trade(self._raw(side_type="sell", side_amount="1.0"), mint="m")
        self.assertEqual(swap.side, "sell")
        self.assertEqual(swap.sol_amount, 1.0)

    def test_side_type_is_case_insensitive(self):
        swap = parse_bitquery_trade(self._raw(side_type="BUY"), mint="m")
        self.assertEqual(swap.side, "buy")

    def test_unrecognized_side_type_returns_none_not_a_guess(self):
        self.assertIsNone(parse_bitquery_trade(self._raw(side_type="swap"), mint="m"))

    def test_missing_field_returns_none_not_a_guess(self):
        raw = self._raw()
        del raw["Trade"]["Side"]["Amount"]
        self.assertIsNone(parse_bitquery_trade(raw, mint="m"))

    def test_unparsable_timestamp_returns_none(self):
        raw = self._raw()
        raw["Block"]["Time"] = "garbage"
        self.assertIsNone(parse_bitquery_trade(raw, mint="m"))


class TestBucketBitqueryWindows(unittest.TestCase):
    def test_aggregates_multiple_swaps_into_one_window(self):
        swaps = [
            ProbeSwap(ts_ms=10500, side="buy", sol_amount=3.0, price=1.0, raw={}),
            ProbeSwap(ts_ms=10800, side="buy", sol_amount=2.0, price=1.0, raw={}),
            ProbeSwap(ts_ms=11500, side="sell", sol_amount=1.5, price=1.0, raw={}),
        ]
        windows = bucket_bitquery_windows(swaps)
        self.assertEqual(windows[10000]["buy_sol"], 5.0)
        self.assertEqual(windows[10000]["sell_sol"], 1.5)
        self.assertEqual(windows[10000]["tx_count"], 3)


class TestDiffWindows(unittest.TestCase):
    def test_agreement_within_tolerance_is_not_flagged(self):
        tape = {10000: {"buy_sol": 5.0, "sell_sol": 2.0, "tx_count": 3, "price": 1.0, "reserve_price": 1.0}}
        vendor = {10000: {"buy_sol": 5.2, "sell_sol": 2.0, "tx_count": 3}}
        rows = diff_windows(tape, vendor, volume_tolerance_pct=15.0)
        self.assertFalse(rows[0]["mismatch"])

    def test_large_disagreement_is_flagged(self):
        tape = {10000: {"buy_sol": 5.0, "sell_sol": 2.0, "tx_count": 3, "price": 1.0, "reserve_price": 1.0}}
        vendor = {10000: {"buy_sol": 1.0, "sell_sol": 2.0, "tx_count": 3}}   # buy off by 5x
        rows = diff_windows(tape, vendor, volume_tolerance_pct=15.0)
        self.assertTrue(rows[0]["mismatch"])

    def test_swapped_side_convention_would_be_caught(self):
        """The whole point: if Bitquery's buy/sell were swapped, the buy
        column would look like the tape's sell column and vice versa -- this
        must NOT accidentally look like agreement."""
        tape = {10000: {"buy_sol": 5.0, "sell_sol": 2.0, "tx_count": 3, "price": 1.0, "reserve_price": 1.0}}
        vendor_swapped = {10000: {"buy_sol": 2.0, "sell_sol": 5.0, "tx_count": 3}}
        rows = diff_windows(tape, vendor_swapped, volume_tolerance_pct=15.0)
        self.assertTrue(rows[0]["mismatch"])

    def test_one_sided_windows_are_reported_but_not_flagged(self):
        tape = {10000: {"buy_sol": 5.0, "sell_sol": 2.0, "tx_count": 3, "price": 1.0, "reserve_price": 1.0}}
        rows = diff_windows(tape, {})
        self.assertEqual(rows[0]["vendor_buy_sol"], None)
        self.assertFalse(rows[0]["mismatch"])


class TestSummarize(unittest.TestCase):
    def test_counts_each_category(self):
        rows = [
            {"tape_buy_sol": 1.0, "vendor_buy_sol": 1.0, "mismatch": False},
            {"tape_buy_sol": 1.0, "vendor_buy_sol": None, "mismatch": False},
            {"tape_buy_sol": None, "vendor_buy_sol": 1.0, "mismatch": False},
            {"tape_buy_sol": 1.0, "vendor_buy_sol": 9.0, "mismatch": True},
        ]
        s = summarize(rows)
        self.assertIn("4 windows total", s)
        self.assertIn("1 mismatched", s)
        self.assertIn("1 tape-only", s)
        self.assertIn("1 vendor-only", s)


if __name__ == "__main__":
    unittest.main()
