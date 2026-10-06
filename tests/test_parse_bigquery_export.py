import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape.scripts.parse_bigquery_export import (
    load_bigquery_export, parse_bigquery_row, _signer_pubkey,
    _native_sol_delta, _token_delta, _parse_bq_timestamp_ms,
)

MINT = "3tUn8dGmDceJ9pCwFLUVo2jkD6FLWaZ78YEpAa3bpump"
SIGNER = "7JJY2BKvca25jzQMZhJzBijuNEvGnnm6u4rGX8M2XvFh"

# A real transaction from solana-data-sandbox.crypto_solana_mainnet_us,
# manually verified against the trade it represents (D22, docs/DECISIONS.md):
# signer's token balance drops 59075.531191 -> 25941.207769 (a sell of
# ~33134.32 tokens), signer's native SOL rises 0.01 -> 0.090520575 SOL.
REAL_ROW = {
    "block_timestamp": "2026-09-20 00:33:37.000000 UTC",
    "signature": "3hbsSX1ZgMGL4RRAakqmdqEZ3SiF5X73e4K6GaFwPEsdfb2royg7dwujuC6VdPR4AsUUmXAq1Tbi8mWy7Rnx5NNg",
    "fee": "15000",
    "accounts": [
        {"pubkey": SIGNER, "signer": "true", "writable": "true"},
        {"pubkey": "6xqkN2QXbbu2utgSBQRQ9eTALgvhUKbNus7V1L4Pjbqb", "signer": "false", "writable": "true"},
    ],
    "balance_changes": [
        {"account": SIGNER, "before": "10000000", "after": "90520575"},
    ],
    "pre_token_balances": [
        {"account_index": "1", "mint": "So11111111111111111111111111111111111111112",
         "owner": "POOL", "amount": "195575520889", "decimals": "9"},
        {"account_index": "2", "mint": MINT, "owner": SIGNER, "amount": "59075531191", "decimals": "6"},
    ],
    "post_token_balances": [
        {"account_index": "1", "mint": "So11111111111111111111111111111111111111112",
         "owner": "POOL", "amount": "195494211324", "decimals": "9"},
        {"account_index": "2", "mint": MINT, "owner": SIGNER, "amount": "25941207769", "decimals": "6"},
    ],
}


class TestLoadBigqueryExport(unittest.TestCase):
    def test_parses_json_array(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "export.json"
            p.write_text('[{"a": 1}, {"a": 2}]')
            self.assertEqual(load_bigquery_export(p), [{"a": 1}, {"a": 2}])

    def test_parses_newline_delimited_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "export.jsonl"
            p.write_text('{"a": 1}\n{"a": 2}\n')
            self.assertEqual(load_bigquery_export(p), [{"a": 1}, {"a": 2}])

    def test_empty_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.json"
            p.write_text("")
            self.assertEqual(load_bigquery_export(p), [])


class TestParseBqTimestamp(unittest.TestCase):
    """The real bug this project actually hit: the console JSON export omits
    fractional seconds entirely when they're `.000000` rather than keeping
    them, and an earlier version of this parser assumed a `.` would always
    be present to split on -- silently dropping 18,160/18,160 real rows.
    """

    def test_without_fractional_seconds(self):
        self.assertIsNotNone(_parse_bq_timestamp_ms("2026-09-20 00:21:01 UTC"))

    def test_with_fractional_seconds(self):
        self.assertIsNotNone(_parse_bq_timestamp_ms("2026-09-20 00:33:37.000000 UTC"))

    def test_both_forms_agree_on_the_same_second(self):
        a = _parse_bq_timestamp_ms("2026-09-20 00:21:01 UTC")
        b = _parse_bq_timestamp_ms("2026-09-20 00:21:01.000000 UTC")
        self.assertEqual(a, b)

    def test_known_value(self):
        # 2026-09-20T00:21:01Z in epoch ms.
        expected = int(__import__("datetime").datetime(
            2026, 9, 20, 0, 21, 1, tzinfo=__import__("datetime").timezone.utc
        ).timestamp() * 1000)
        self.assertEqual(_parse_bq_timestamp_ms("2026-09-20 00:21:01 UTC"), expected)

    def test_none_on_empty_or_garbage(self):
        self.assertIsNone(_parse_bq_timestamp_ms(""))
        self.assertIsNone(_parse_bq_timestamp_ms(None))
        self.assertIsNone(_parse_bq_timestamp_ms("not a timestamp"))


class TestSignerPubkey(unittest.TestCase):
    def test_finds_the_signer_account(self):
        self.assertEqual(_signer_pubkey(REAL_ROW), SIGNER)

    def test_none_when_no_signer_flagged(self):
        row = {"accounts": [{"pubkey": "x", "signer": "false"}]}
        self.assertIsNone(_signer_pubkey(row))

    def test_none_on_missing_accounts(self):
        self.assertIsNone(_signer_pubkey({}))


class TestNativeSolDelta(unittest.TestCase):
    def test_computes_lamports_to_sol_delta(self):
        delta = _native_sol_delta(REAL_ROW, SIGNER)
        self.assertAlmostEqual(delta, 0.080520575)

    def test_none_when_account_not_present(self):
        self.assertIsNone(_native_sol_delta(REAL_ROW, "someone else"))


class TestTokenDelta(unittest.TestCase):
    def test_computes_decimal_scaled_delta_for_owner_and_mint(self):
        delta = _token_delta(REAL_ROW, SIGNER, MINT)
        self.assertAlmostEqual(delta, 25941.207769 - 59075.531191, places=6)

    def test_none_when_owner_has_no_balance_for_that_mint(self):
        self.assertIsNone(_token_delta(REAL_ROW, "nobody", MINT))

    def test_post_balance_missing_means_full_exit_sell_not_unmeasured(self):
        """D22 follow-up (docs/DECISIONS.md): a token account absent from
        `post_token_balances` was CLOSED, which on Solana requires it to
        already be empty -- so this is a full sell of the pre-balance (-100
        here), not an unmeasurable case. Returning None instead silently
        dropped 71% of real rows the first time this ran (12,879/18,160),
        systematically undercounting sell volume.
        """
        row = {
            "pre_token_balances": [{"account_index": "1", "mint": MINT, "owner": SIGNER,
                                     "amount": "100", "decimals": "0"}],
            "post_token_balances": [],
        }
        self.assertAlmostEqual(_token_delta(row, SIGNER, MINT), -100.0)

    def test_empty_placeholder_entries_are_treated_the_same_as_closed(self):
        """Real data: BigQuery's export sometimes pads `post_token_balances`
        with a bare `{}` (no `account_index`) instead of omitting the entry,
        for the same "account closed" case as the test above. Must not
        raise KeyError -- it crashed 18,160/18,160 real rows the first time
        -- and must reach the same -100 answer, not a different one.
        """
        row = {
            "pre_token_balances": [{"account_index": "1", "mint": MINT, "owner": SIGNER,
                                     "amount": "100", "decimals": "0"}],
            "post_token_balances": [{}],
        }
        self.assertAlmostEqual(_token_delta(row, SIGNER, MINT), -100.0)

    def test_pre_balance_missing_means_first_ever_buy_not_unmeasured(self):
        """Mirror case: a brand-new buyer's associated token account is
        CREATED in this transaction, so it has no `pre_token_balances`
        entry. Absent-from-pre reads as pre-balance = 0 (the account did not
        exist yet), so this is a buy of the full post-balance (+250).
        """
        row = {
            "pre_token_balances": [],
            "post_token_balances": [{"account_index": "1", "mint": MINT, "owner": SIGNER,
                                      "amount": "250", "decimals": "0"}],
        }
        self.assertAlmostEqual(_token_delta(row, SIGNER, MINT), 250.0)


class TestParseBigqueryRow(unittest.TestCase):
    def test_real_transaction_parses_as_a_sell(self):
        """The whole point: a real, manually-verified transaction (D22)
        should come out as a sell of ~33134.32 tokens for ~0.0805 SOL --
        this is the ground-truth check for the whole balance-delta approach,
        not just a unit test of arithmetic.
        """
        swap = parse_bigquery_row(REAL_ROW, mint=MINT)
        self.assertIsNotNone(swap)
        self.assertEqual(swap.side, "sell")
        self.assertAlmostEqual(swap.sol_amount, 0.080520575)

    def test_buy_direction_when_token_balance_increases(self):
        row = dict(REAL_ROW)
        row["pre_token_balances"] = [
            {"account_index": "2", "mint": MINT, "owner": SIGNER, "amount": "1000", "decimals": "6"},
        ]
        row["post_token_balances"] = [
            {"account_index": "2", "mint": MINT, "owner": SIGNER, "amount": "2000", "decimals": "6"},
        ]
        row["balance_changes"] = [{"account": SIGNER, "before": "50000000", "after": "10000000"}]
        swap = parse_bigquery_row(row, mint=MINT)
        self.assertEqual(swap.side, "buy")
        self.assertAlmostEqual(swap.sol_amount, 0.04)

    def test_zero_token_delta_returns_none_not_a_zero_amount_swap(self):
        row = dict(REAL_ROW)
        row["post_token_balances"] = row["pre_token_balances"]
        self.assertIsNone(parse_bigquery_row(row, mint=MINT))

    def test_no_signer_returns_none(self):
        row = dict(REAL_ROW)
        row["accounts"] = [{"pubkey": "x", "signer": "false"}]
        self.assertIsNone(parse_bigquery_row(row, mint=MINT))

    def test_unparsable_timestamp_returns_none(self):
        row = dict(REAL_ROW)
        row["block_timestamp"] = ""
        self.assertIsNone(parse_bigquery_row(row, mint=MINT))


if __name__ == "__main__":
    unittest.main()
