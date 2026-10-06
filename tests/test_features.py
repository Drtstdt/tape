"""Tests for tape/features.py's D71 additions -- the two v3-inspired
features (netflow_reversal_{k}, unique_sellers_10/buyer_seller_breadth_10)
added so `information_audit.py` can test TradingRPC/v3's core accumulation/
flow-reversal thesis as real, causal, individually-scored features instead
of trusting v3's own never-actually-run implementation (docs/DECISIONS.md
D69-D71).

Bars are constructed directly (not via BarBuilder) for precise control over
net_flow / unique_buyers / unique_sellers per bar.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from tape.features import PRICE_AFTER_OFFSETS_S, TokenState
from tape.schema import Bar


def make_bar(i, buy_quote, sell_quote, unique_buyers=1, unique_sellers=1,
             close=1.0, high=None, low=None, quote_reserve_close=100.0):
    return Bar(
        mint="M", venue="v", kind="dollar",
        open_ts_ms=i * 1000, close_ts_ms=i * 1000 + 900,
        open=close, high=high if high is not None else close,
        low=low if low is not None else close, close=close,
        buy_quote=buy_quote, sell_quote=sell_quote,
        buy_count=1, sell_count=1, trade_count=2, attributed_count=2,
        unique_buyers=unique_buyers, unique_sellers=unique_sellers,
        largest_buyer_share=0.5, buy_hhi=0.5,
        quote_reserve_close=quote_reserve_close,
    )


class TestNetflowReversal(unittest.TestCase):
    def test_none_until_two_full_windows_exist(self):
        st = TokenState("M", created_ts_ms=0)
        # k=3: need 6 bars total before netflow_reversal_3 is non-None.
        for i in range(5):
            st.update(make_bar(i, buy_quote=10.0, sell_quote=0.0))
        feats = st.features()
        self.assertIsNone(feats["netflow_reversal_3"])

    def test_positive_when_flow_accelerating(self):
        st = TokenState("M", created_ts_ms=0)
        # Prior 3 bars: net_flow=+1 each (buy=1,sell=0). Recent 3: net_flow=+5 each.
        for _ in range(3):
            st.update(make_bar(0, buy_quote=1.0, sell_quote=0.0))
        for _ in range(3):
            st.update(make_bar(0, buy_quote=5.0, sell_quote=0.0))
        feats = st.features()
        # recent sum = 15, prior sum = 3 -> reversal = +12
        self.assertAlmostEqual(feats["netflow_reversal_3"], 12.0)

    def test_negative_when_flow_reverses_from_positive_to_negative(self):
        """The literal v3 thesis: accumulation (positive flow), then
        reversal (flow turns negative) -- STRATEGY.md section 6's
        flow_reversal exit."""
        st = TokenState("M", created_ts_ms=0)
        for _ in range(3):
            st.update(make_bar(0, buy_quote=10.0, sell_quote=0.0))   # prior: net +10 each
        for _ in range(3):
            st.update(make_bar(0, buy_quote=0.0, sell_quote=8.0))    # recent: net -8 each
        feats = st.features()
        # recent sum = -24, prior sum = +30 -> reversal = -54 (sharply negative)
        self.assertAlmostEqual(feats["netflow_reversal_3"], -54.0)
        self.assertLess(feats["netflow_reversal_3"], 0.0)

    def test_is_a_change_not_a_level_differs_from_netflow_k(self):
        """netflow_reversal_k must not just duplicate netflow_k -- confirms
        it is genuinely a different, new signal."""
        st = TokenState("M", created_ts_ms=0)
        for _ in range(3):
            st.update(make_bar(0, buy_quote=10.0, sell_quote=0.0))
        for _ in range(3):
            st.update(make_bar(0, buy_quote=10.0, sell_quote=0.0))
        feats = st.features()
        # Flow is flat (not reversing): netflow_3 is +30 (a level), but
        # netflow_reversal_3 is 0 (no change from the prior window).
        self.assertAlmostEqual(feats["netflow_3"], 30.0)
        self.assertAlmostEqual(feats["netflow_reversal_3"], 0.0)


class TestBuyerSellerBreadth(unittest.TestCase):
    def test_positive_when_buyers_dominate(self):
        st = TokenState("M", created_ts_ms=0)
        for _ in range(5):
            st.update(make_bar(0, 1.0, 1.0, unique_buyers=3, unique_sellers=1))
        feats = st.features()
        self.assertEqual(feats["unique_buyers_10"], 15.0)
        self.assertEqual(feats["unique_sellers_10"], 5.0)
        self.assertAlmostEqual(feats["buyer_seller_breadth_10"], (15 - 5) / (15 + 5))
        self.assertGreater(feats["buyer_seller_breadth_10"], 0)

    def test_negative_when_sellers_dominate(self):
        st = TokenState("M", created_ts_ms=0)
        for _ in range(5):
            st.update(make_bar(0, 1.0, 1.0, unique_buyers=1, unique_sellers=4))
        feats = st.features()
        self.assertLess(feats["buyer_seller_breadth_10"], 0)

    def test_none_when_unmeasured_never_reads_as_zero(self):
        """D3 discipline: missing attribution must fail closed as None, not
        be silently read as 'zero sellers' (which would look like pure
        buying pressure and inflate the breadth score)."""
        st = TokenState("M", created_ts_ms=0)
        for _ in range(5):
            st.update(make_bar(0, 1.0, 1.0, unique_buyers=None, unique_sellers=None))
        feats = st.features()
        self.assertIsNone(feats["unique_buyers_10"])
        self.assertIsNone(feats["unique_sellers_10"])
        self.assertIsNone(feats["buyer_seller_breadth_10"])


def raw_bar(open_ts_ms, close_ts_ms, open_, close_):
    """A Bar with explicit timestamps -- make_bar()'s i*1000/i*1000+900
    convention doesn't give the fine control price_after_Ns tests need over
    exactly where a bar sits relative to a 1s/2s/5s/10s/30s threshold."""
    return Bar(
        mint="M", venue="v", kind="dollar",
        open_ts_ms=open_ts_ms, close_ts_ms=close_ts_ms,
        open=open_, high=max(open_, close_), low=min(open_, close_), close=close_,
        buy_quote=1.0, sell_quote=0.0,
        buy_count=1, sell_count=0, trade_count=1, attributed_count=1,
        unique_buyers=1, unique_sellers=0,
        largest_buyer_share=1.0, buy_hhi=1.0,
        quote_reserve_close=100.0,
    )


class TestPriceAfter(unittest.TestCase):
    """D82 (docs/DECISIONS.md): price_after_Ns as a real, causal feature --
    the path token_dna_report.py's D77 descriptive rho=+0.47..+0.52 finding
    was explicitly promoted to, per the user's choice, instead of trusting
    the raw correlation."""

    def test_none_without_created_ts_ms(self):
        """No launch-time reference -> no offset is even definable. Fails
        closed, same discipline as age_ms."""
        st = TokenState("M")  # created_ts_ms defaults to None
        st.update(raw_bar(0, 500, 1.0, 1.2))
        feats = st.features()
        for o in PRICE_AFTER_OFFSETS_S:
            self.assertIsNone(feats[f"price_after_{o}s"])

    def test_none_while_offset_not_yet_passed(self):
        """A bar that closes AT OR BEFORE the threshold cannot yet answer
        'what was price after N seconds' -- real time hasn't reached N
        seconds yet. Must be None, not a premature guess."""
        st = TokenState("M", created_ts_ms=0)
        st.update(raw_bar(0, 500, 1.0, 1.2))  # closes at 500ms, well before 1000ms
        feats = st.features()
        self.assertIsNone(feats["price_after_1s"])
        self.assertIsNone(feats["price_after_30s"])

    def test_finalizes_on_previous_bars_close_once_next_bar_starts_after_threshold(self):
        st = TokenState("M", created_ts_ms=0)
        st.update(raw_bar(0, 500, 1.0, 1.2))       # fully before 1s threshold
        st.update(raw_bar(1500, 2000, 1.5, 1.8))   # starts (1500ms) after 1s threshold (1000ms)
        feats = st.features()
        # entry_price = first bar's open = 1.0; frozen value = prior bar's close = 1.2
        self.assertAlmostEqual(feats["price_after_1s"], 1.2 / 1.0 - 1.0)

    def test_uses_bar_open_when_the_bar_straddles_the_threshold(self):
        st = TokenState("M", created_ts_ms=0)
        st.update(raw_bar(0, 500, 1.0, 1.1))        # fully before 1s threshold
        st.update(raw_bar(800, 1500, 1.3, 2.0))     # straddles: open(800)<=1000<close(1500)
        feats = st.features()
        # open (1.3) is a real trade known to have happened at 800ms <= 1000ms threshold.
        self.assertAlmostEqual(feats["price_after_1s"], 1.3 / 1.0 - 1.0)

    def test_frozen_after_finalization_ignores_later_bars(self):
        st = TokenState("M", created_ts_ms=0)
        st.update(raw_bar(0, 500, 1.0, 1.2))
        st.update(raw_bar(1500, 2000, 1.5, 1.8))  # finalizes price_after_1s at 1.2
        before = st.features()["price_after_1s"]
        st.update(raw_bar(2000, 2500, 50.0, 100.0))  # wild later move must not change it
        after = st.features()["price_after_1s"]
        self.assertAlmostEqual(before, after)
        self.assertAlmostEqual(after, 1.2 / 1.0 - 1.0)

    def test_first_bar_spanning_past_threshold_uses_entry_price_itself(self):
        """No sub-bar information exists at all -- the only known price at or
        before a small early offset is the entry price (open), giving a
        return of exactly 0.0, same as token_dna_report.py's default before
        any later swap is observed."""
        st = TokenState("M", created_ts_ms=0)
        st.update(raw_bar(0, 5000, 1.0, 3.0))  # spans straight through 1s and 2s
        feats = st.features()
        self.assertAlmostEqual(feats["price_after_1s"], 0.0)
        self.assertAlmostEqual(feats["price_after_2s"], 0.0)
        # 5s threshold == this bar's close_ts_ms exactly (5000<=5000) -> still
        # "at or before", so it keeps updating rather than finalizing yet.
        self.assertIsNone(feats["price_after_5s"])

    def test_each_offset_finalizes_independently(self):
        st = TokenState("M", created_ts_ms=0)
        st.update(raw_bar(0, 900, 1.0, 1.0))
        st.update(raw_bar(900, 1800, 1.0, 2.0))     # crosses 1s (1000ms) threshold
        feats = st.features()
        self.assertIsNotNone(feats["price_after_1s"])
        self.assertIsNone(feats["price_after_2s"])   # 2s threshold not yet crossed
        self.assertIsNone(feats["price_after_30s"])


if __name__ == "__main__":
    unittest.main()
