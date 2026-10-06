import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tape.sources.helius as helius
from tape.sources.helius import (
    HeliusSource, SOL_MINT, _counterparty_owner, _fetch_signatures, _native_sol_delta,
    _signer_pubkey, _token_delta, _token_deltas_all, _ui_amount,
    PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM, _MAX_VENUE_LEN, _venue_from_program_ids,
)

MINT = "3tUn8dGmDceJ9pCwFLUVo2jkD6FLWaZ78YEpAa3bpump"
OWNER = "PoolOwner11111111111111111111111111111111"
SIGNER = "Signer1111111111111111111111111111111111"


def _tb(idx, owner, mint, amount, decimals=6):
    return {"accountIndex": idx, "owner": owner, "mint": mint,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}


def _tx(sig="sig1", block_time=1758400000, slot=999, err=None,
        account_keys=None, pre_token=None, post_token=None,
        pre_bal=None, post_bal=None, instructions=None):
    account_keys = account_keys if account_keys is not None else [SIGNER, "other"]
    return {
        "result": {
            "blockTime": block_time,
            "slot": slot,
            "meta": {
                "err": err,
                "preTokenBalances": pre_token or [],
                "postTokenBalances": post_token or [],
                "preBalances": pre_bal if pre_bal is not None else [2_000_000_000, 0],
                "postBalances": post_bal if post_bal is not None else [1_500_000_000, 0],
            },
            "transaction": {
                "message": {
                    "accountKeys": account_keys,
                    "instructions": instructions if instructions is not None else [],
                }
            },
        }
    }


class TestVenueFromProgramIds(unittest.TestCase):
    """D65 (2026-09-22, docs/DECISIONS.md): a >200-char joined-program-ids
    venue string crashed Store.write_swaps() on Windows (WinError 123) the
    first time a transaction bundling many programs (ATA creation + compute
    budget + memo + token program + the real pump.fun call -- a common
    "snipe" pattern seconds after a token's create_v2) was backfilled."""

    def test_known_pumpfun_program_present_returns_just_that(self):
        ids = sorted(["ComputeBudget111111111111111111111111111111",
                       "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
                       PUMPFUN_PROGRAM,
                       "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                       "11111111111111111111111111111111"])
        self.assertEqual(_venue_from_program_ids(ids), PUMPFUN_PROGRAM)

    def test_both_known_programs_present_are_sorted_and_joined(self):
        ids = [PUMPSWAP_PROGRAM, "SomeOtherProgram1111111111111111111111111", PUMPFUN_PROGRAM]
        expected = "+".join(sorted([PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM]))
        self.assertEqual(_venue_from_program_ids(ids), expected)

    def test_no_known_program_short_list_unchanged(self):
        # Matches the existing test_parses_a_buy/test_no_instructions_gives_
        # unknown_venue expectations below: fake ids fall through unchanged.
        self.assertEqual(_venue_from_program_ids(["ProgA", "ProgB"]), "ProgA+ProgB")

    def test_no_known_program_long_list_is_truncated_and_bounded(self):
        ids = [f"Program{i}" * 3 for i in range(10)]
        venue = _venue_from_program_ids(ids)
        self.assertLessEqual(len(venue), _MAX_VENUE_LEN)
        self.assertIn("_", venue)  # hash suffix separator
        joined = "+".join(ids)
        self.assertGreater(len(joined), _MAX_VENUE_LEN, "test setup should exceed the cap")

    def test_empty_list_gives_unknown(self):
        self.assertEqual(_venue_from_program_ids([]), "unknown")

    def test_truncated_venue_never_exceeds_windows_safe_length(self):
        """The original bug: a >200-char venue in the path. Any fallback
        output, whatever the input, must stay well under that."""
        ids = [f"Program{i}" * 5 for i in range(30)]
        venue = _venue_from_program_ids(ids)
        self.assertLessEqual(len(venue), _MAX_VENUE_LEN)


class TestUiAmount(unittest.TestCase):
    def test_none_entry_gives_zero_zero(self):
        self.assertEqual(_ui_amount(None), (0.0, 0))

    def test_normal_entry(self):
        entry = {"uiTokenAmount": {"amount": "123456", "decimals": 6}}
        self.assertEqual(_ui_amount(entry), (123456.0, 6))

    def test_garbage_amount_does_not_crash(self):
        entry = {"uiTokenAmount": {"amount": "not-a-number", "decimals": 6}}
        self.assertEqual(_ui_amount(entry), (0.0, 0))


class TestSignerPubkey(unittest.TestCase):
    def test_empty_list_gives_none(self):
        self.assertIsNone(_signer_pubkey([]))

    def test_string_shaped_first_entry(self):
        self.assertEqual(_signer_pubkey(["abc", "def"]), "abc")

    def test_dict_shaped_first_entry(self):
        self.assertEqual(_signer_pubkey([{"pubkey": "abc", "signer": True}, "def"]), "abc")


class TestNativeSolDelta(unittest.TestCase):
    def test_normal_case_lamports_to_sol(self):
        pre = [2_000_000_000]
        post = [1_500_000_000]
        self.assertAlmostEqual(_native_sol_delta(pre, post, 0), -0.5)

    def test_index_out_of_range_gives_none(self):
        self.assertIsNone(_native_sol_delta([1], [], 0))
        self.assertIsNone(_native_sol_delta([], [1], 0))

    def test_non_numeric_gives_none_not_a_crash(self):
        self.assertIsNone(_native_sol_delta(["oops"], [1], 0))


class TestTokenDelta(unittest.TestCase):
    def test_first_ever_buy_absent_from_pre_reads_as_zero(self):
        """D22 follow-up: a freshly-created token account is absent from
        pre_token_balances entirely, not present with amount=0."""
        post = [_tb(2, SIGNER, MINT, 1000, decimals=6)]
        delta = _token_delta([], post, SIGNER, MINT)
        self.assertAlmostEqual(delta, 0.001)  # 1000 / 1e6

    def test_full_exit_sell_closes_account_absent_from_post(self):
        """D22 follow-up: selling the entire balance closes the account, so
        it's absent from post_token_balances -- must read as balance 0, not
        None/unmeasurable."""
        pre = [_tb(2, SIGNER, MINT, 1000, decimals=6)]
        delta = _token_delta(pre, [], SIGNER, MINT)
        self.assertAlmostEqual(delta, -0.001)

    def test_partial_sell_present_on_both_sides(self):
        pre = [_tb(2, SIGNER, MINT, 1000, decimals=6)]
        post = [_tb(2, SIGNER, MINT, 400, decimals=6)]
        delta = _token_delta(pre, post, SIGNER, MINT)
        self.assertAlmostEqual(delta, (400 - 1000) / 1e6)

    def test_no_matching_owner_or_mint_gives_none(self):
        pre = [_tb(2, "someone-else", MINT, 1000)]
        post = [_tb(2, "someone-else", MINT, 400)]
        self.assertIsNone(_token_delta(pre, post, SIGNER, MINT))

    def test_wrong_mint_at_same_index_gives_none(self):
        pre = [_tb(2, SIGNER, "different-mint", 1000)]
        self.assertIsNone(_token_delta(pre, [], SIGNER, MINT))

    def test_entries_missing_account_index_are_skipped_not_a_crash(self):
        pre = [{"owner": SIGNER, "mint": MINT, "uiTokenAmount": {"amount": "1", "decimals": 0}}]
        self.assertIsNone(_token_delta(pre, [], SIGNER, MINT))


class TestTokenDeltasAll(unittest.TestCase):
    """`_token_deltas_all` -- the multi-mint generalization of `_token_delta`
    that `from_signatures` needs (D38, docs/DECISIONS.md): a signature found
    via a bare program-id scan doesn't come with a known mint attached."""

    def test_single_mint_matches_token_delta(self):
        mint2 = "OtherMint2222222222222222222222222222222"
        pre = [_tb(1, SIGNER, MINT, 1000, decimals=6)]
        post = [_tb(1, SIGNER, MINT, 400, decimals=6)]
        deltas = _token_deltas_all(pre, post, SIGNER)
        self.assertEqual(set(deltas), {MINT})
        self.assertAlmostEqual(deltas[MINT], (400 - 1000) / 1e6)
        self.assertNotIn(mint2, deltas)

    def test_multiple_mints_all_reported(self):
        mint_a, mint_b = "MintA1111111111111111111111111111111111", "MintB2222222222222222222222222222222222"
        pre = [_tb(1, SIGNER, mint_a, 1000, decimals=6), _tb(2, SIGNER, mint_b, 5000, decimals=3)]
        post = [_tb(1, SIGNER, mint_a, 400, decimals=6), _tb(2, SIGNER, mint_b, 6000, decimals=3)]
        deltas = _token_deltas_all(pre, post, SIGNER)
        self.assertAlmostEqual(deltas[mint_a], (400 - 1000) / 1e6)
        self.assertAlmostEqual(deltas[mint_b], (6000 - 5000) / 1e3)

    def test_zero_delta_omitted(self):
        pre = [_tb(1, SIGNER, MINT, 1000)]
        post = [_tb(1, SIGNER, MINT, 1000)]
        self.assertEqual(_token_deltas_all(pre, post, SIGNER), {})

    def test_other_owner_ignored(self):
        pre = [_tb(1, "someone-else", MINT, 1000)]
        post = [_tb(1, "someone-else", MINT, 400)]
        self.assertEqual(_token_deltas_all(pre, post, SIGNER), {})

    def test_no_balances_gives_empty_dict_not_none(self):
        self.assertEqual(_token_deltas_all([], [], SIGNER), {})


class TestCounterpartyOwner(unittest.TestCase):
    """Reads the pool vault owner straight from a `getTransaction` response
    already in hand -- the free alternative to `resolve_pool_owner`'s extra
    RPC round-trip that `from_signatures` uses (D38)."""

    def test_finds_the_other_party(self):
        pre = [_tb(1, SIGNER, MINT, 1000), _tb(2, OWNER, MINT, 50_000)]
        post = [_tb(1, SIGNER, MINT, 400), _tb(2, OWNER, MINT, 50_600)]
        self.assertEqual(_counterparty_owner(pre, post, MINT, SIGNER), OWNER)

    def test_no_counterparty_found_gives_none_not_a_guess(self):
        pre = [_tb(1, SIGNER, MINT, 1000)]
        self.assertIsNone(_counterparty_owner(pre, [], MINT, SIGNER))

    def test_ignores_a_different_mint(self):
        pre = [_tb(2, OWNER, "different-mint", 50_000)]
        self.assertIsNone(_counterparty_owner(pre, [], MINT, SIGNER))


def _raw_ix(program_id):
    return {"programId": program_id}


class TestToCanonical(unittest.TestCase):
    def setUp(self):
        self.source = HeliusSource(api_key="test-key")

    def test_parses_a_buy(self):
        tx = _tx(
            pre_token=[],
            post_token=[_tb(1, SIGNER, MINT, 500_000, decimals=6)],
            pre_bal=[2_000_000_000, 0], post_bal=[1_000_000_000, 0],
            instructions=[_raw_ix("ProgB"), _raw_ix("ProgA")],
        )
        swap = self.source._to_canonical(tx, sig="sig1", mint=MINT, pool=OWNER)
        self.assertIsNotNone(swap)
        self.assertEqual(swap.side, "buy")
        self.assertAlmostEqual(swap.base_amount, 0.5)
        self.assertAlmostEqual(swap.quote_amount, 1.0)
        self.assertEqual(swap.quote_mint, SOL_MINT)
        self.assertEqual(swap.wallet, SIGNER)
        self.assertEqual(swap.pool, OWNER)
        self.assertEqual(swap.sig, "sig1")
        self.assertEqual(swap.source, "helius")
        self.assertEqual(swap.venue, "ProgA+ProgB")  # sorted, joined

    def test_parses_a_sell(self):
        tx = _tx(
            pre_token=[_tb(1, SIGNER, MINT, 500_000, decimals=6)],
            post_token=[],
            pre_bal=[1_000_000_000, 0], post_bal=[1_500_000_000, 0],
        )
        swap = self.source._to_canonical(tx, sig="sig2", mint=MINT, pool=OWNER)
        self.assertEqual(swap.side, "sell")
        self.assertAlmostEqual(swap.base_amount, 0.5)
        self.assertAlmostEqual(swap.quote_amount, 0.5)

    def test_failed_transaction_returns_none(self):
        tx = _tx(err={"InstructionError": [0, "boom"]})
        self.assertIsNone(self.source._to_canonical(tx, sig="s", mint=MINT, pool=OWNER))

    def test_no_token_delta_returns_none_not_a_guess(self):
        tx = _tx(pre_token=[], post_token=[])
        self.assertIsNone(self.source._to_canonical(tx, sig="s", mint=MINT, pool=OWNER))

    def test_zero_token_delta_returns_none(self):
        tx = _tx(
            pre_token=[_tb(1, SIGNER, MINT, 1000)],
            post_token=[_tb(1, SIGNER, MINT, 1000)],
        )
        self.assertIsNone(self.source._to_canonical(tx, sig="s", mint=MINT, pool=OWNER))

    def test_missing_block_time_returns_none(self):
        tx = _tx(post_token=[_tb(1, SIGNER, MINT, 1000)])
        tx["result"]["blockTime"] = None
        self.assertIsNone(self.source._to_canonical(tx, sig="s", mint=MINT, pool=OWNER))

    def test_no_result_returns_none(self):
        self.assertIsNone(self.source._to_canonical({}, sig="s", mint=MINT, pool=OWNER))

    def test_no_instructions_gives_unknown_venue(self):
        tx = _tx(post_token=[_tb(1, SIGNER, MINT, 1000)], instructions=[])
        swap = self.source._to_canonical(tx, sig="s", mint=MINT, pool=OWNER)
        self.assertEqual(swap.venue, "unknown")


class TestFetchSignatures(unittest.TestCase):
    """`_fetch_signatures` now uses `getTransfersByAddress` (server-side
    mint + blockTime filtered) purely for signature discovery -- NOT
    `getSignaturesForAddress`, which was found live (2026-09-21) to have no
    time filter at all and to page backward through unrelated future
    history first. These tests pin the new endpoint, the dedup (a pool's
    own transfer records can repeat a signature across both legs of a
    swap), and the same stall-guard `helius_probe.py::fetch_helius_transfers`
    already established as necessary for a busy pool."""

    def _transfer_page(self, sigs, token=None):
        return {"result": {"data": [{"signature": s} for s in sigs], "paginationToken": token}}

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    def test_single_page_returns_signatures_in_order(self, mock_rpc, _sleep):
        mock_rpc.return_value = self._transfer_page(["a", "b", "c"], token=None)
        sigs = _fetch_signatures(OWNER, MINT, 0, 10_000, "key")
        self.assertEqual(sigs, ["a", "b", "c"])
        method, params, _ = mock_rpc.call_args[0]
        self.assertEqual(method, "getTransfersByAddress")
        self.assertEqual(params[0], OWNER)
        self.assertEqual(params[1]["mint"], MINT)

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    def test_paginates_until_token_is_null(self, mock_rpc, _sleep):
        mock_rpc.side_effect = [
            self._transfer_page(["a", "b"], token="tok1"),
            self._transfer_page(["c"], token=None),
        ]
        sigs = _fetch_signatures(OWNER, MINT, 0, 10_000, "key")
        self.assertEqual(sigs, ["a", "b", "c"])
        self.assertEqual(mock_rpc.call_count, 2)

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    def test_dedups_a_signature_repeated_across_pages(self, mock_rpc, _sleep):
        """A pool's own transfer record can appear twice for one swap (its
        incoming AND outgoing leg) -- must not double-count the signature."""
        mock_rpc.side_effect = [
            self._transfer_page(["a", "b"], token="tok1"),
            self._transfer_page(["b", "c"], token=None),
        ]
        sigs = _fetch_signatures(OWNER, MINT, 0, 10_000, "key")
        self.assertEqual(sigs, ["a", "b", "c"])

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    def test_stuck_pagination_token_raises(self, mock_rpc, _sleep):
        mock_rpc.side_effect = [self._transfer_page(["a"], token="tok1") for _ in range(3)]
        with self.assertRaises(RuntimeError) as ctx:
            _fetch_signatures(OWNER, MINT, 0, 10_000, "key")
        self.assertIn("did not advance", str(ctx.exception))

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    def test_busy_pool_many_pages_does_not_false_positive(self, mock_rpc, _sleep):
        """Regression test for the exact MAX_PAGES-style false positive
        `helius_probe.py` hit (D25) -- many real, advancing pages must not
        be mistaken for a stall."""
        pages = [self._transfer_page([f"sig{i}"], token=f"tok{i}") for i in range(60)]
        pages.append(self._transfer_page(["sig_last"], token=None))
        mock_rpc.side_effect = pages
        sigs = _fetch_signatures(OWNER, MINT, 0, 10_000, "key")
        self.assertEqual(len(sigs), 61)

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    def test_blocktime_filter_covers_the_full_inclusive_window(self, mock_rpc, _sleep):
        mock_rpc.return_value = self._transfer_page([], token=None)
        _fetch_signatures(OWNER, MINT, 5_000, 9_000, "key")
        _, params, _ = mock_rpc.call_args[0]
        bt = params[1]["filters"]["blockTime"]
        self.assertEqual(bt["gte"], 5)
        self.assertEqual(bt["lt"], 10)  # until_ms=9000 -> until_s=9 -> lt=9+1, inclusive


class TestToCanonicalAnyMint(unittest.TestCase):
    """The discovery-path decoder (D38): no mint or pool given up front --
    both come out of the transaction itself."""

    def setUp(self):
        self.source = HeliusSource(api_key="test-key")

    def test_single_mint_buy_discovers_mint_and_pool(self):
        tx = _tx(
            pre_token=[_tb(2, OWNER, MINT, 50_000, decimals=6)],
            post_token=[_tb(1, SIGNER, MINT, 500_000, decimals=6),
                        _tb(2, OWNER, MINT, 49_500, decimals=6)],
            pre_bal=[2_000_000_000, 0], post_bal=[1_000_000_000, 0],
        )
        swaps = self.source._to_canonical_any_mint(tx, sig="sig1")
        self.assertEqual(len(swaps), 1)
        swap = swaps[0]
        self.assertEqual(swap.mint, MINT)
        self.assertEqual(swap.pool, OWNER)
        self.assertEqual(swap.side, "buy")
        self.assertAlmostEqual(swap.base_amount, 0.5)

    def test_signer_untouched_gives_no_swaps(self):
        """Signer was only the fee payer -- no token balance change for them
        at all. Must not fabricate a swap."""
        tx = _tx(pre_token=[], post_token=[])
        self.assertEqual(self.source._to_canonical_any_mint(tx, sig="s"), [])

    def test_sol_mint_leg_excluded_from_results(self):
        """A wrapped-SOL balance entry for the signer is the quote leg, not
        a base token being swapped -- must not turn into its own swap."""
        tx = _tx(
            pre_token=[_tb(1, SIGNER, SOL_MINT, 2_000_000_000, decimals=9)],
            post_token=[_tb(1, SIGNER, SOL_MINT, 1_000_000_000, decimals=9)],
        )
        self.assertEqual(self.source._to_canonical_any_mint(tx, sig="s"), [])

    def test_no_counterparty_falls_back_to_unknown_pool(self):
        tx = _tx(post_token=[_tb(1, SIGNER, MINT, 500_000, decimals=6)])
        swaps = self.source._to_canonical_any_mint(tx, sig="s")
        self.assertEqual(swaps[0].pool, "unknown")

    def test_failed_transaction_gives_no_swaps(self):
        tx = _tx(err={"InstructionError": [0, "boom"]})
        self.assertEqual(self.source._to_canonical_any_mint(tx, sig="s"), [])


class TestFromSignatures(unittest.TestCase):
    def setUp(self):
        self.source = HeliusSource(api_key="test-key")

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    def test_decodes_each_signature(self, mock_rpc, _sleep):
        def _fake_rpc(method, params, api_key):
            sig = params[0]
            return _tx(sig=sig, post_token=[_tb(1, SIGNER, MINT, 100_000, decimals=6)])
        mock_rpc.side_effect = _fake_rpc

        swaps = list(self.source.from_signatures(["a", "b"]))
        self.assertEqual({s.sig for s in swaps}, {"a", "b"})

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    def test_checkpoint_skips_already_done_and_is_appended_to(self, mock_rpc, _sleep):
        import tempfile
        mock_rpc.return_value = _tx(sig="b", post_token=[_tb(1, SIGNER, MINT, 100_000, decimals=6)])
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("a\n")
            ckpt = f.name
        try:
            swaps = list(self.source.from_signatures(["a", "b"], checkpoint_path=ckpt))
            self.assertEqual([s.sig for s in swaps], ["b"])  # "a" skipped, never fetched
            self.assertEqual(mock_rpc.call_count, 1)
            with open(ckpt) as f:
                self.assertEqual(f.read().splitlines(), ["a", "b"])
        finally:
            os.unlink(ckpt)

    @patch("tape.sources.helius._rpc_call")
    def test_concurrent_fetch_preserves_order_and_checkpoint_order(self, mock_rpc):
        """D42: from_signatures() also now fetches via a thread pool. The
        checkpoint file must still be written in INPUT order (never
        completion order), since a resumed run relies on it being a valid
        prefix of `sigs` -- this pins that guarantee under scrambled
        completion times, same as TestHistorical's equivalent test."""
        import tempfile
        import time as _time

        sigs = ["a", "b", "c", "d", "e"]
        delays = {"a": 0.05, "b": 0.03, "c": 0.01, "d": 0.0, "e": 0.0}

        def _fake_rpc(method, params, api_key):
            sig = params[0]
            _time.sleep(delays[sig])
            return _tx(sig=sig, post_token=[_tb(1, SIGNER, MINT, 100_000, decimals=6)])

        mock_rpc.side_effect = _fake_rpc
        source = HeliusSource(api_key="test-key", max_workers=5)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            ckpt = f.name
        try:
            swaps = list(source.from_signatures(sigs, checkpoint_path=ckpt))
            self.assertEqual([s.sig for s in swaps], sigs)
            with open(ckpt) as f:
                self.assertEqual(f.read().splitlines(), sigs)
        finally:
            os.unlink(ckpt)

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    def test_max_workers_is_configurable(self, mock_rpc, _sleep):
        mock_rpc.side_effect = lambda method, params, api_key: _tx(
            sig=params[0], post_token=[_tb(1, SIGNER, MINT, 100_000, decimals=6)])

        source = HeliusSource(api_key="test-key", max_workers=1)
        self.assertEqual(source.max_workers, 1)
        swaps = list(source.from_signatures(["a", "b"]))
        self.assertEqual({s.sig for s in swaps}, {"a", "b"})


class TestHistorical(unittest.TestCase):
    def setUp(self):
        self.source = HeliusSource(api_key="test-key")

    @patch("tape.sources.helius.resolve_pool_owner")
    def test_raises_loud_if_pool_owner_cannot_be_resolved(self, mock_resolve):
        mock_resolve.return_value = None
        with self.assertRaises(RuntimeError):
            list(self.source.historical(mint=MINT, start_ms=0, end_ms=1000))

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    @patch("tape.sources.helius._fetch_signatures")
    @patch("tape.sources.helius.resolve_pool_owner")
    def test_yields_swaps_ascending_deduped(self, mock_resolve, mock_fetch_sigs, mock_rpc, _sleep):
        mock_resolve.return_value = OWNER
        # _fetch_signatures returns ascending order already (getTransfersByAddress
        # with sortOrder: "asc" -- see its docstring); "b" repeated (should be
        # deduped by historical()'s own seen_sigs safety net) must still only
        # be fetched once.
        mock_fetch_sigs.return_value = ["a", "b", "b", "c"]

        def _fake_rpc(method, params, api_key):
            self.assertEqual(method, "getTransaction")
            sig = params[0]
            amounts = {"a": 100_000, "b": 200_000, "c": 300_000}
            return _tx(sig=sig, post_token=[_tb(1, SIGNER, MINT, amounts[sig])])

        mock_rpc.side_effect = _fake_rpc

        swaps = list(self.source.historical(mint=MINT, start_ms=0, end_ms=10_000_000))

        self.assertEqual([s.sig for s in swaps], ["a", "b", "c"])
        # only 3 getTransaction calls -- the duplicate "b" must not be re-fetched
        self.assertEqual(mock_rpc.call_count, 3)

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    @patch("tape.sources.helius._fetch_signatures")
    @patch("tape.sources.helius.resolve_pool_owner")
    def test_a_signature_with_no_real_swap_is_silently_skipped(self, mock_resolve, mock_fetch_sigs, mock_rpc, _sleep):
        mock_resolve.return_value = OWNER
        mock_fetch_sigs.return_value = ["a"]
        mock_rpc.return_value = _tx(sig="a", pre_token=[], post_token=[])  # no token delta at all

        swaps = list(self.source.historical(mint=MINT, start_ms=0, end_ms=10_000_000))
        self.assertEqual(swaps, [])

    @patch("tape.sources.helius._rpc_call")
    @patch("tape.sources.helius._fetch_signatures")
    @patch("tape.sources.helius.resolve_pool_owner")
    def test_concurrent_fetch_preserves_ascending_order_regardless_of_completion_order(
            self, mock_resolve, mock_fetch_sigs, mock_rpc):
        """D42: getTransaction calls now run on a thread pool, not one at a
        time. `historical()`'s contract (SourceAdapter, tape/sources/__init__.py)
        requires deterministic, ascending-by-ts_ms output -- this pins that
        the OUTPUT order still matches input signature order even when the
        slowest call is first and the fastest is last, i.e. completion order
        is deliberately scrambled here, not left to chance."""
        import time as _time
        mock_resolve.return_value = OWNER
        sigs = ["a", "b", "c", "d", "e"]
        mock_fetch_sigs.return_value = sigs
        # "a" sleeps longest, "e" not at all -- if results leaked out in
        # completion order this test would see them arrive scrambled.
        delays = {"a": 0.05, "b": 0.03, "c": 0.01, "d": 0.0, "e": 0.0}

        def _fake_rpc(method, params, api_key):
            sig = params[0]
            _time.sleep(delays[sig])
            amounts = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}
            return _tx(sig=sig, post_token=[_tb(1, SIGNER, MINT, amounts[sig] * 100_000)])

        mock_rpc.side_effect = _fake_rpc
        source = HeliusSource(api_key="test-key", max_workers=5)

        swaps = list(source.historical(mint=MINT, start_ms=0, end_ms=10_000_000))
        self.assertEqual([s.sig for s in swaps], sigs)

    @patch("time.sleep")
    @patch("tape.sources.helius._rpc_call")
    @patch("tape.sources.helius._fetch_signatures")
    @patch("tape.sources.helius.resolve_pool_owner")
    def test_max_workers_is_configurable(self, mock_resolve, mock_fetch_sigs, mock_rpc, _sleep):
        mock_resolve.return_value = OWNER
        mock_fetch_sigs.return_value = ["a", "b"]
        mock_rpc.side_effect = lambda method, params, api_key: _tx(
            sig=params[0], post_token=[_tb(1, SIGNER, MINT, 100_000)])

        source = HeliusSource(api_key="test-key", max_workers=1)
        self.assertEqual(source.max_workers, 1)
        swaps = list(source.historical(mint=MINT, start_ms=0, end_ms=10_000_000))
        self.assertEqual(len(swaps), 2)

    def test_default_max_workers(self):
        from tape.sources.helius import DEFAULT_MAX_WORKERS
        self.assertEqual(HeliusSource(api_key="k").max_workers, DEFAULT_MAX_WORKERS)


if __name__ == "__main__":
    unittest.main()
