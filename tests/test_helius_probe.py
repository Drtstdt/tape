import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape.scripts.helius_probe import (
    resolve_pool_owner, fetch_helius_transfers, _rpc_call, MAX_PAGES, MAX_RETRIES,
)

MINT = "3tUn8dGmDceJ9pCwFLUVo2jkD6FLWaZ78YEpAa3bpump"


def _http_resp(status_code=200, json_body=None, headers=None, raises=None):
    """A fake httpx.Response: `raises`, when set, is the exception
    `raise_for_status()` should throw (only exercised for non-429 codes,
    matching real httpx behaviour).
    """
    r = Mock()
    r.status_code = status_code
    r.headers = headers or {}
    r.json = Mock(return_value=json_body if json_body is not None else {})
    r.raise_for_status = Mock(side_effect=raises)
    return r


class TestRpcCallRetry(unittest.TestCase):
    """Confirmed live (2026-09-21): a real getTransfersByAddress pull hit
    429 after ~70 unthrottled requests, with no retry logic to recover.
    These pin the fix: back off and retry on 429, honour Retry-After when
    given, never retry a non-429 error, and fail loud (not forever) if the
    limit is genuinely sustained.
    """

    @patch("time.sleep")
    @patch("httpx.post")
    def test_retries_on_429_then_succeeds(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _http_resp(429),
            _http_resp(200, json_body={"result": {"ok": True}}),
        ]
        body = _rpc_call("getTransfersByAddress", ["owner", {}], api_key="k")
        self.assertEqual(body, {"result": {"ok": True}})
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("time.sleep")
    @patch("httpx.post")
    def test_honours_retry_after_header_over_backoff_guess(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _http_resp(429, headers={"Retry-After": "5"}),
            _http_resp(200, json_body={"result": {}}),
        ]
        _rpc_call("m", [], api_key="k")
        mock_sleep.assert_called_once_with(5.0)

    @patch("time.sleep")
    @patch("httpx.post")
    def test_raises_after_exhausting_retries_on_sustained_429(self, mock_post, mock_sleep):
        mock_post.return_value = _http_resp(429)
        with self.assertRaises(RuntimeError) as ctx:
            _rpc_call("m", [], api_key="k")
        self.assertIn("429", str(ctx.exception))
        self.assertEqual(mock_post.call_count, MAX_RETRIES)

    @patch("httpx.post")
    def test_non_429_http_error_is_not_retried(self, mock_post):
        import httpx
        resp = _http_resp(500, raises=httpx.HTTPStatusError("boom", request=Mock(), response=Mock()))
        mock_post.return_value = resp
        with self.assertRaises(httpx.HTTPStatusError):
            _rpc_call("m", [], api_key="k")
        self.assertEqual(mock_post.call_count, 1)


class TestResolvePoolOwner(unittest.TestCase):
    @patch("tape.scripts.helius_probe._rpc_call")
    def test_happy_path_returns_owner(self, mock_rpc):
        mock_rpc.side_effect = [
            {"result": {"value": [{"address": "vault1", "amount": "999999"}]}},
            {"result": {"value": {"data": {"parsed": {"info": {"owner": "pool_owner_1"}}}}}},
        ]
        owner = resolve_pool_owner(MINT, api_key="k")
        self.assertEqual(owner, "pool_owner_1")
        self.assertEqual(mock_rpc.call_args_list[0].args[0], "getTokenLargestAccounts")
        self.assertEqual(mock_rpc.call_args_list[1].args[0], "getAccountInfo")

    @patch("tape.scripts.helius_probe._rpc_call")
    def test_no_accounts_returns_none_not_a_guess(self, mock_rpc):
        mock_rpc.return_value = {"result": {"value": []}}
        self.assertIsNone(resolve_pool_owner(MINT, api_key="k"))

    @patch("tape.scripts.helius_probe._rpc_call")
    def test_malformed_account_info_returns_none_not_a_crash(self, mock_rpc):
        mock_rpc.side_effect = [
            {"result": {"value": [{"address": "vault1", "amount": "1"}]}},
            {"result": {"value": {"data": {}}}},  # missing parsed.info.owner entirely
        ]
        self.assertIsNone(resolve_pool_owner(MINT, api_key="k"))


class TestFetchHeliusTransfers(unittest.TestCase):
    @patch("tape.scripts.helius_probe._rpc_call")
    def test_single_page_no_pagination_token(self, mock_rpc):
        mock_rpc.return_value = {"result": {"data": [{"signature": "a"}], "paginationToken": None}}
        transfers = fetch_helius_transfers("owner1", MINT, 0, 10_000, api_key="k")
        self.assertEqual(transfers, [{"signature": "a"}])
        self.assertEqual(mock_rpc.call_count, 1)

    @patch("tape.scripts.helius_probe._rpc_call")
    def test_paginates_until_token_is_null(self, mock_rpc):
        mock_rpc.side_effect = [
            {"result": {"data": [{"signature": "a"}], "paginationToken": "tok1"}},
            {"result": {"data": [{"signature": "b"}], "paginationToken": "tok2"}},
            {"result": {"data": [{"signature": "c"}], "paginationToken": None}},
        ]
        transfers = fetch_helius_transfers("owner1", MINT, 0, 10_000, api_key="k")
        self.assertEqual([t["signature"] for t in transfers], ["a", "b", "c"])
        self.assertEqual(mock_rpc.call_count, 3)
        # the second call must carry forward the token the first call returned
        second_call_params = mock_rpc.call_args_list[1].args[1][1]
        self.assertEqual(second_call_params["paginationToken"], "tok1")

    @patch("tape.scripts.helius_probe._rpc_call")
    def test_converts_ms_window_to_seconds_for_blocktime_filter(self, mock_rpc):
        mock_rpc.return_value = {"result": {"data": [], "paginationToken": None}}
        fetch_helius_transfers("owner1", MINT, since_ms=5_000, until_ms=15_000, api_key="k")
        params = mock_rpc.call_args_list[0].args[1][1]
        self.assertEqual(params["filters"]["blockTime"], {"gte": 5, "lt": 15})

    @patch("tape.scripts.helius_probe._rpc_call")
    def test_raises_fast_when_pagination_token_repeats(self, mock_rpc):
        """The real stuck-loop signal is the SAME token twice in a row -- must
        be caught in 2 calls, not by grinding through MAX_PAGES, and must not
        be confused with a busy pool that just has a lot of real data (see
        the next test).
        """
        mock_rpc.return_value = {"result": {"data": [{"signature": "x"}], "paginationToken": "same-token"}}
        with self.assertRaises(RuntimeError) as ctx:
            fetch_helius_transfers("owner1", MINT, 0, 10_000, api_key="k")
        self.assertIn("did not advance", str(ctx.exception))
        self.assertEqual(mock_rpc.call_count, 2)

    @patch("time.sleep")  # 60 real INTER_PAGE_DELAY_S sleeps would work but waste real time
    @patch("tape.scripts.helius_probe._rpc_call")
    def test_a_busy_pool_with_many_advancing_pages_does_not_raise(self, mock_rpc, mock_sleep):
        """Regression for the real bug this project hit: MAX_PAGES=50 tripped
        on a pool D22/D23 already know had 18,160 real transactions in this
        window. A token that keeps ADVANCING, however many pages that takes,
        must never be mistaken for stuck.
        """
        pages = [
            {"result": {"data": [{"signature": f"s{i}"}], "paginationToken": f"tok{i}"}}
            for i in range(1, 61)
        ]
        pages[-1]["result"]["paginationToken"] = None  # last page terminates
        mock_rpc.side_effect = pages
        transfers = fetch_helius_transfers("owner1", MINT, 0, 10_000, api_key="k")
        self.assertEqual(len(transfers), 60)
        self.assertEqual(mock_rpc.call_count, 60)

    @patch("time.sleep")  # otherwise ~1000 real INTER_PAGE_DELAY_S sleeps -- minutes, not a test
    @patch("tape.scripts.helius_probe._rpc_call")
    def test_raises_if_max_pages_exceeded_while_still_advancing(self, mock_rpc, mock_sleep):
        # Every token distinct (always advancing) but never terminates --
        # MAX_PAGES is still the outer safety net for this case.
        mock_rpc.side_effect = (
            {"result": {"data": [{"signature": f"s{i}"}], "paginationToken": f"tok{i}"}}
            for i in range(1, MAX_PAGES + 5)
        )
        with self.assertRaises(RuntimeError) as ctx:
            fetch_helius_transfers("owner1", MINT, 0, 10_000, api_key="k")
        self.assertIn("MAX_PAGES", str(ctx.exception))
        self.assertEqual(mock_rpc.call_count, MAX_PAGES)


if __name__ == "__main__":
    unittest.main()
