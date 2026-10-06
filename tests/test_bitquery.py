import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tape.sources.bitquery as bitquery
from tape.sources.bitquery import (
    BitquerySource, DATASET_ARCHIVE, DATASET_REALTIME, PUMPFUN_PROGRAM,
    PUMPSWAP_PROGRAM, _fetch_oauth_token, _parse_bq_iso_ms, _ms_to_bq_iso, SOL_MINT,
)


def _raw_trade(sig="sig1", side_type="buy", side_amount="2.5", base_amount="1000.0",
                price=0.001, time_iso="2026-09-20T00:00:11Z", slot=123,
                protocol_name="pump", market="pool1", account="wallet1"):
    """Shape matches TRADES_QUERY's selected fields exactly -- same
    convention as tests/test_probe.py::TestParseBitqueryTrade._raw, since
    _to_canonical is meant to mirror parse_bitquery_trade field-for-field.
    """
    return {
        "Block": {"Time": time_iso, "Slot": slot},
        "Transaction": {"Signature": sig, "Signer": "someone"},
        "Trade": {
            "Dex": {"ProtocolName": protocol_name, "ProtocolFamily": "pumpfun",
                    "ProgramAddress": "prog1"},
            "Market": {"MarketAddress": market},
            "Account": {"Address": account},
            "Side": {"Type": side_type, "Amount": side_amount, "AmountInUSD": 1.23,
                      "Account": {"Address": "counterparty"}},
            "Amount": base_amount,
            "Price": price,
            "PriceInUSD": 1.0,
            "Currency": {"MintAddress": "m", "Decimals": 6},
        },
    }


class TestParseBqIsoMs(unittest.TestCase):
    def test_parses_z_suffixed_utc(self):
        self.assertIsNotNone(_parse_bq_iso_ms("2026-09-20T12:00:00Z"))

    def test_none_on_garbage_input_not_a_crash_or_a_zero(self):
        self.assertIsNone(_parse_bq_iso_ms("not a timestamp"))
        self.assertIsNone(_parse_bq_iso_ms(""))
        self.assertIsNone(_parse_bq_iso_ms(None))

    def test_agrees_with_probes_parser_on_the_same_input(self):
        # Deliberately duplicated from probe.py rather than imported (see
        # bitquery.py's _to_canonical docstring) -- this pins the two
        # implementations to the same answer so they can't silently drift.
        from tape.scripts.probe import _parse_block_time_ms
        s = "2026-09-20T00:00:11Z"
        self.assertEqual(_parse_bq_iso_ms(s), _parse_block_time_ms(s))


class TestMsToBqIso(unittest.TestCase):
    def test_round_trips_through_parse(self):
        ms = 1790000000123  # arbitrary, with non-zero milliseconds
        iso = _ms_to_bq_iso(ms)
        self.assertEqual(_parse_bq_iso_ms(iso), ms)

    def test_output_is_z_suffixed(self):
        self.assertTrue(_ms_to_bq_iso(1790000000000).endswith("Z"))


class TestToCanonical(unittest.TestCase):
    def setUp(self):
        self.source = BitquerySource(api_key="test-key")

    def test_parses_a_buy(self):
        swap = self.source._to_canonical(_raw_trade(side_type="buy", side_amount="2.5"), mint="m")
        self.assertIsNotNone(swap)
        self.assertEqual(swap.side, "buy")
        self.assertEqual(swap.quote_amount, 2.5)
        self.assertEqual(swap.base_amount, 1000.0)
        self.assertEqual(swap.quote_mint, SOL_MINT)
        self.assertEqual(swap.sig, "sig1")
        self.assertEqual(swap.wallet, "wallet1")
        self.assertEqual(swap.venue, "pump")
        self.assertEqual(swap.pool, "pool1")
        self.assertEqual(swap.source, "bitquery")

    def test_parses_a_sell(self):
        swap = self.source._to_canonical(_raw_trade(side_type="sell"), mint="m")
        self.assertEqual(swap.side, "sell")

    def test_side_type_is_case_insensitive(self):
        swap = self.source._to_canonical(_raw_trade(side_type="BUY"), mint="m")
        self.assertEqual(swap.side, "buy")

    def test_unrecognized_side_type_returns_none_not_a_guess(self):
        self.assertIsNone(self.source._to_canonical(_raw_trade(side_type="swap"), mint="m"))

    def test_missing_field_returns_none_not_a_guess(self):
        raw = _raw_trade()
        del raw["Trade"]["Side"]
        self.assertIsNone(self.source._to_canonical(raw, mint="m"))

    def test_unparsable_timestamp_returns_none(self):
        raw = _raw_trade(time_iso="not a timestamp")
        self.assertIsNone(self.source._to_canonical(raw, mint="m"))

    def test_venue_falls_back_to_protocol_family_then_unknown(self):
        raw = _raw_trade()
        raw["Trade"]["Dex"] = {"ProtocolFamily": "pumpfun"}
        swap = self.source._to_canonical(raw, mint="m")
        self.assertEqual(swap.venue, "pumpfun")

        raw2 = _raw_trade()
        raw2["Trade"]["Dex"] = {}
        swap2 = self.source._to_canonical(raw2, mint="m")
        self.assertEqual(swap2.venue, "unknown")

    def test_missing_account_gives_none_wallet_not_a_fabricated_id(self):
        raw = _raw_trade()
        raw["Trade"]["Account"] = None
        swap = self.source._to_canonical(raw, mint="m")
        self.assertIsNone(swap.wallet)


def _mock_response(body: dict, status_code: int = 200, headers: dict = None) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value=body)
    resp.text = str(body)
    resp.request = Mock(url="https://streaming.bitquery.io/graphql")
    resp.headers = headers if headers is not None else {}
    return resp


def _body(trades: list) -> dict:
    return {"data": {"Solana": {"DEXTradeByTokens": trades}}}


class TestHistorical(unittest.TestCase):
    def setUp(self):
        self.source = BitquerySource(api_key="test-key")
        self._orig_page_limit = bitquery.PAGE_LIMIT

    def tearDown(self):
        bitquery.PAGE_LIMIT = self._orig_page_limit

    def test_rejects_a_start_older_than_the_retention_floor(self):
        """D21's ~9h empirical `realtime` retention floor -- must fail loud,
        never silently return zero rows for a range that looks recent.
        """
        now_ms = int(time.time() * 1000)
        old_start = now_ms - 100 * 3600 * 1000  # 100h ago, well past the 9h floor
        with self.assertRaises(RuntimeError):
            list(self.source.historical(mint="m", start_ms=old_start, end_ms=now_ms))

    @patch("httpx.post")
    def test_raises_on_graphql_errors_in_body(self, mock_post):
        mock_post.return_value = _mock_response({"errors": [{"message": "boom"}]})
        now_ms = int(time.time() * 1000)
        with self.assertRaises(RuntimeError):
            list(self.source.historical(mint="m", start_ms=now_ms - 1000, end_ms=now_ms))

    @patch("httpx.post")
    def test_raises_a_clear_error_on_shape_drift(self, mock_post):
        mock_post.return_value = _mock_response({"data": {"Solana": {}}})
        now_ms = int(time.time() * 1000)
        with self.assertRaises(RuntimeError) as ctx:
            list(self.source.historical(mint="m", start_ms=now_ms - 1000, end_ms=now_ms))
        self.assertIn("schema may have drifted", str(ctx.exception))

    @patch("httpx.post")
    def test_empty_first_page_yields_nothing(self, mock_post):
        mock_post.return_value = _mock_response(_body([]))
        now_ms = int(time.time() * 1000)
        swaps = list(self.source.historical(mint="m", start_ms=now_ms - 1000, end_ms=now_ms))
        self.assertEqual(swaps, [])

    @patch("time.sleep")
    @patch("httpx.post")
    def test_paginates_and_dedups_across_the_second_boundary(self, mock_post, _sleep):
        """Re-requests FROM (not after) the last-seen second and relies on
        sig-based dedup, per historical()'s pagination docstring -- this
        pins that behaviour: a trade repeated across two pages (same second,
        page boundary) must be yielded exactly once, and a full page
        (== PAGE_LIMIT) must trigger a second request.
        """
        bitquery.PAGE_LIMIT = 3
        now_ms = int(time.time() * 1000)
        t0 = now_ms - 100000

        page1 = [
            _raw_trade(sig="a", time_iso=_ms_to_bq_iso(t0)),
            _raw_trade(sig="b", time_iso=_ms_to_bq_iso(t0)),
            _raw_trade(sig="c", time_iso=_ms_to_bq_iso(t0 + 1000)),
        ]
        # Page 2: re-includes "c" (same second as the cursor) plus one new
        # trade "d" -- fewer than PAGE_LIMIT, so this is the last page.
        page2 = [
            _raw_trade(sig="c", time_iso=_ms_to_bq_iso(t0 + 1000)),
            _raw_trade(sig="d", time_iso=_ms_to_bq_iso(t0 + 2000)),
        ]
        mock_post.side_effect = [_mock_response(_body(page1)), _mock_response(_body(page2))]

        swaps = list(self.source.historical(mint="m", start_ms=t0, end_ms=t0 + 100000))

        self.assertEqual([s.sig for s in swaps], ["a", "b", "c", "d"])
        self.assertEqual(mock_post.call_count, 2)
        second_call_vars = mock_post.call_args_list[1].kwargs["json"]["variables"]
        self.assertEqual(_parse_bq_iso_ms(second_call_vars["since"]), t0 + 1000)

    @patch("time.sleep")
    @patch("httpx.post")
    def test_stalled_pagination_raises_instead_of_looping_forever(self, mock_post, _sleep):
        """If a full page of PAGE_LIMIT trades comes back with everything
        already seen, twice in a row at the same cursor, that means a single
        second has >= PAGE_LIMIT trades for this mint -- must raise, not
        infinite-loop or silently truncate.
        """
        bitquery.PAGE_LIMIT = 2
        now_ms = int(time.time() * 1000)
        t0 = now_ms - 100000
        same_second_page = [
            _raw_trade(sig="a", time_iso=_ms_to_bq_iso(t0)),
            _raw_trade(sig="b", time_iso=_ms_to_bq_iso(t0)),
        ]
        mock_post.side_effect = [_mock_response(_body(same_second_page)) for _ in range(3)]

        with self.assertRaises(RuntimeError) as ctx:
            list(self.source.historical(mint="m", start_ms=t0, end_ms=t0 + 100000))
        self.assertIn("stalled", str(ctx.exception))


class TestDataset(unittest.TestCase):
    """D39: `dataset` is now a real constructor param -- archive is
    purchased, but must default to `realtime` so every pre-existing call
    site and test keeps its exact prior behaviour."""

    def test_defaults_to_realtime(self):
        self.assertEqual(BitquerySource(api_key="k").dataset, DATASET_REALTIME)

    def test_accepts_archive(self):
        self.assertEqual(BitquerySource(api_key="k", dataset=DATASET_ARCHIVE).dataset,
                          DATASET_ARCHIVE)

    def test_rejects_an_unknown_dataset(self):
        with self.assertRaises(ValueError):
            BitquerySource(api_key="k", dataset="combined")  # D19: confirmed 403 on this account

    @patch("httpx.post")
    def test_archive_dataset_has_no_retention_floor_guard(self, mock_post):
        """The realtime-only guard (D21's ~9h floor) must not fire for
        archive -- its real reach is unverified, not zero."""
        mock_post.return_value = _mock_response(_body([]))
        source = BitquerySource(api_key="k", dataset=DATASET_ARCHIVE)
        very_old = int(time.time() * 1000) - 200 * 24 * 3600 * 1000  # 200 days ago
        swaps = list(source.historical(mint="m", start_ms=very_old, end_ms=very_old + 1000))
        self.assertEqual(swaps, [])  # no raise -- the point of this test
        sent_query = mock_post.call_args.kwargs["json"]["query"]
        self.assertIn("dataset: archive", sent_query)

    @patch("httpx.post")
    def test_realtime_dataset_is_interpolated_into_the_query(self, mock_post):
        mock_post.return_value = _mock_response(_body([]))
        now_ms = int(time.time() * 1000)
        list(BitquerySource(api_key="k").historical(mint="m", start_ms=now_ms - 1000, end_ms=now_ms))
        sent_query = mock_post.call_args.kwargs["json"]["query"]
        self.assertIn("dataset: realtime", sent_query)


class TestOAuthRefresh(unittest.TestCase):
    """D39: client_credentials refresh is opt-in and additive -- a bare
    api_key must behave exactly as it always has."""

    def test_static_api_key_never_calls_oauth(self):
        source = BitquerySource(api_key="static-token")
        source._ensure_fresh_token()  # no client_id/secret -- must be a no-op
        self.assertEqual(source.api_key, "static-token")

    @patch("tape.sources.bitquery._fetch_oauth_token")
    def test_fetches_a_token_on_first_use_when_creds_given(self, mock_fetch):
        mock_fetch.return_value = ("fresh-token", time.time() + 3600)
        source = BitquerySource(api_key="placeholder", client_id="cid", client_secret="csecret")
        source._ensure_fresh_token()
        self.assertEqual(source.api_key, "fresh-token")
        mock_fetch.assert_called_once_with("cid", "csecret")

    @patch("tape.sources.bitquery._fetch_oauth_token")
    def test_does_not_refetch_a_still_fresh_token(self, mock_fetch):
        mock_fetch.return_value = ("tok1", time.time() + 3600)
        source = BitquerySource(api_key="placeholder", client_id="cid", client_secret="csecret")
        source._ensure_fresh_token()
        source._ensure_fresh_token()
        self.assertEqual(mock_fetch.call_count, 1)

    @patch("tape.sources.bitquery._fetch_oauth_token")
    def test_refreshes_a_token_about_to_expire(self, mock_fetch):
        mock_fetch.side_effect = [("tok1", time.time() + 30), ("tok2", time.time() + 3600)]
        source = BitquerySource(api_key="placeholder", client_id="cid", client_secret="csecret")
        source._ensure_fresh_token()
        source._ensure_fresh_token()  # within TOKEN_REFRESH_MARGIN_S (60s) of tok1's expiry
        self.assertEqual(source.api_key, "tok2")
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("httpx.post")
    def test_fetch_oauth_token_parses_a_standard_response(self, mock_post):
        mock_post.return_value = _mock_response({"access_token": "abc", "expires_in": 3600})
        token, expires_at = _fetch_oauth_token("cid", "csecret")
        self.assertEqual(token, "abc")
        self.assertGreater(expires_at, time.time())


def _discover_trade(sig="sig1", mint="mintA", pool="poolA", time_iso="2026-06-01T00:00:00Z"):
    return {
        "Block": {"Time": time_iso, "Slot": 1},
        "Transaction": {"Signature": sig},
        "Trade": {"Currency": {"MintAddress": mint}, "Market": {"MarketAddress": pool}},
    }


def _discover_body(trades: list) -> dict:
    return _body(trades)


class TestDiscover(unittest.TestCase):
    """D39: the actual universe-scaling mechanism -- a broad, no-mint scan
    filtered on the verified pump.fun/PumpSwap program ids, yielding each
    mint the first time it's seen rather than requiring one known upfront."""

    def setUp(self):
        self.source = BitquerySource(api_key="k", dataset=DATASET_ARCHIVE)

    @patch("httpx.post")
    def test_filters_on_the_verified_program_ids_by_default(self, mock_post):
        mock_post.return_value = _mock_response(_discover_body([]))
        list(self.source.discover(start_ms=0, end_ms=1000))
        sent_vars = mock_post.call_args.kwargs["json"]["variables"]
        self.assertEqual(sent_vars["programs"], [PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM])

    @patch("httpx.post")
    def test_yields_each_mint_once_on_first_sighting(self, mock_post):
        trades = [
            _discover_trade(sig="a", mint="mintA", time_iso="2026-06-01T00:00:00Z"),
            _discover_trade(sig="b", mint="mintA", time_iso="2026-06-01T00:00:01Z"),  # same mint again
            _discover_trade(sig="c", mint="mintB", time_iso="2026-06-01T00:00:02Z"),
        ]
        mock_post.return_value = _mock_response(_discover_body(trades))
        found = list(self.source.discover(start_ms=0, end_ms=10_000_000_000))
        self.assertEqual([f["mint"] for f in found], ["mintA", "mintB"])
        self.assertEqual(found[0]["pool"], "poolA")
        self.assertEqual(found[0]["sig"], "a")

    @patch("httpx.post")
    def test_empty_page_yields_nothing(self, mock_post):
        mock_post.return_value = _mock_response(_discover_body([]))
        self.assertEqual(list(self.source.discover(start_ms=0, end_ms=1000)), [])

    @patch("httpx.post")
    def test_shape_drift_raises_a_clear_error(self, mock_post):
        mock_post.return_value = _mock_response({"data": {"Solana": {}}})
        with self.assertRaises(RuntimeError) as ctx:
            list(self.source.discover(start_ms=0, end_ms=1000))
        self.assertIn("schema may have drifted", str(ctx.exception))

    @patch("httpx.post")
    def test_custom_programs_list_is_honored(self, mock_post):
        mock_post.return_value = _mock_response(_discover_body([]))
        list(self.source.discover(start_ms=0, end_ms=1000, programs=["custom1"]))
        sent_vars = mock_post.call_args.kwargs["json"]["variables"]
        self.assertEqual(sent_vars["programs"], ["custom1"])

    @patch("httpx.post")
    def test_sol_mint_never_yielded_as_a_discovered_mint(self, mock_post):
        """D46 (2026-09-22, docs/DECISIONS.md): CONFIRMED LIVE -- a real
        backfill run picked up SOL_MINT itself as a "discovered mint" (some
        DEXTradeByTokens rows report the quote leg as `Trade.Currency`), and
        historical(mint=SOL_MINT) hung for minutes since both sides of its
        trade filter become SOL. discover() must never yield it."""
        trades = [
            _discover_trade(sig="a", mint=SOL_MINT, time_iso="2026-06-01T00:00:00Z"),
            _discover_trade(sig="b", mint="mintA", time_iso="2026-06-01T00:00:01Z"),
        ]
        mock_post.return_value = _mock_response(_discover_body(trades))
        found = list(self.source.discover(start_ms=0, end_ms=10_000_000_000))
        self.assertEqual([f["mint"] for f in found], ["mintA"])

    @patch("httpx.post")
    def test_known_quote_mints_never_yielded_as_discovered_mints(self, mock_post):
        """D52 (2026-09-22, docs/DECISIONS.md): CONFIRMED LIVE -- the same
        leak D46 fixed for SOL_MINT recurred with USDC (a real Phase 2 run
        spent 400+ pages / 300k+ rows on it before being caught). Generalized
        to KNOWN_QUOTE_MINTS -- pin that every member is excluded, not just
        SOL."""
        from tape.sources.bitquery import KNOWN_QUOTE_MINTS
        trades = [
            _discover_trade(sig=f"sig-{i}", mint=quote_mint, time_iso="2026-06-01T00:00:00Z")
            for i, quote_mint in enumerate(KNOWN_QUOTE_MINTS)
        ] + [_discover_trade(sig="real", mint="mintA", time_iso="2026-06-01T00:00:01Z")]
        mock_post.return_value = _mock_response(_discover_body(trades))
        found = list(self.source.discover(start_ms=0, end_ms=10_000_000_000))
        self.assertEqual([f["mint"] for f in found], ["mintA"])


class TestRaiseForStatusWithBody(unittest.TestCase):
    """2026-09-22: a real `dataset: archive` call 403'd with a token proven
    valid moments earlier on `dataset: realtime` -- httpx's own
    `raise_for_status()` gives just 'HTTP 403', no indication of WHICH of
    several possible causes produced it. This surfaces the response body
    (where Bitquery, like most GraphQL APIs, puts the actual reason) in
    the raised message instead of throwing it away."""

    @patch("httpx.post")
    def test_error_status_includes_response_body_in_historical(self, mock_post):
        mock_post.return_value = _mock_response(
            {"error": "archive dataset not enabled for this API key"}, status_code=403)
        now_ms = int(time.time() * 1000)
        source = BitquerySource(api_key="k", dataset=DATASET_ARCHIVE)
        with self.assertRaises(RuntimeError) as ctx:
            list(source.historical(mint="m", start_ms=now_ms - 1000, end_ms=now_ms))
        self.assertIn("403", str(ctx.exception))
        self.assertIn("archive dataset not enabled", str(ctx.exception))

    @patch("httpx.post")
    def test_error_status_includes_response_body_in_discover(self, mock_post):
        mock_post.return_value = _mock_response({"error": "forbidden"}, status_code=403)
        source = BitquerySource(api_key="k", dataset=DATASET_ARCHIVE)
        with self.assertRaises(RuntimeError) as ctx:
            list(source.discover(start_ms=0, end_ms=1000))
        self.assertIn("403", str(ctx.exception))

    @patch("httpx.post")
    def test_2xx_status_does_not_raise(self, mock_post):
        mock_post.return_value = _mock_response(_body([]), status_code=200)
        now_ms = int(time.time() * 1000)
        source = BitquerySource(api_key="k")
        self.assertEqual(list(source.historical(mint="m", start_ms=now_ms - 1000, end_ms=now_ms)), [])


class TestRateLimitRetry(unittest.TestCase):
    """D44 (2026-09-22, docs/DECISIONS.md): CONFIRMED LIVE -- a real
    `discover()` run tripped Bitquery's per-minute rate limit
    ("access restricted by rate limit: too many requests per minute")
    within seconds, because neither historical() nor discover() had any
    429 handling at all. These pin the fix: retry-with-backoff (mirroring
    tape/sources/helius.py::_rpc_call's pattern) so a transient 429 never
    crashes a real backfill/discovery run."""

    @patch("time.sleep")
    @patch("httpx.post")
    def test_historical_retries_past_a_429_then_succeeds(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _mock_response({"errors": [{"message": "too many requests per minute"}]},
                           status_code=429),
            _mock_response(_body([_raw_trade(sig="a")])),
        ]
        now_ms = int(time.time() * 1000)
        source = BitquerySource(api_key="k")
        swaps = list(source.historical(mint="m", start_ms=now_ms - 1000, end_ms=now_ms))
        self.assertEqual([s.sig for s in swaps], ["a"])
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("time.sleep")
    @patch("httpx.post")
    def test_discover_retries_past_a_429_then_succeeds(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _mock_response({"errors": [{"message": "too many requests per minute"}]},
                           status_code=429),
            _mock_response(_discover_body([_discover_trade(sig="a", mint="mintA")])),
        ]
        source = BitquerySource(api_key="k", dataset=DATASET_ARCHIVE)
        found = list(source.discover(start_ms=0, end_ms=1000))
        self.assertEqual([f["mint"] for f in found], ["mintA"])
        self.assertEqual(mock_post.call_count, 2)

    @patch("time.sleep")
    @patch("httpx.post")
    def test_retry_after_header_is_honored_over_backoff(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _mock_response({}, status_code=429, headers={"Retry-After": "3"}),
            _mock_response(_body([])),
        ]
        now_ms = int(time.time() * 1000)
        source = BitquerySource(api_key="k")
        list(source.historical(mint="m", start_ms=now_ms - 1000, end_ms=now_ms))
        mock_sleep.assert_called_once_with(3.0)

    @patch("time.sleep")
    @patch("httpx.post")
    def test_sustained_429_raises_after_max_retries_not_an_infinite_loop(self, mock_post, mock_sleep):
        mock_post.return_value = _mock_response(
            {"errors": [{"message": "too many requests per minute"}]}, status_code=429)
        now_ms = int(time.time() * 1000)
        source = BitquerySource(api_key="k")
        with self.assertRaises(RuntimeError) as ctx:
            list(source.historical(mint="m", start_ms=now_ms - 1000, end_ms=now_ms))
        self.assertIn("429", str(ctx.exception))
        self.assertEqual(mock_post.call_count, bitquery.BITQUERY_MAX_RETRIES)

    @patch("httpx.post")
    def test_non_429_error_status_is_not_retried(self, mock_post):
        """A real 403 (wrong dataset entitlement, D40) must surface
        immediately with its body -- must not be mistaken for a rate
        limit and retried/delayed."""
        mock_post.return_value = _mock_response({"error": "forbidden"}, status_code=403)
        source = BitquerySource(api_key="k", dataset=DATASET_ARCHIVE)
        with self.assertRaises(RuntimeError) as ctx:
            list(source.discover(start_ms=0, end_ms=1000))
        self.assertIn("403", str(ctx.exception))
        self.assertEqual(mock_post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
