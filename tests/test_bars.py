import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from tape.bars import BarBuilder
from tape.schema import CanonicalSwap


def swap(ts, price, qty, side="buy", wallet="w1", res=100.0):
    return CanonicalSwap(mint="M", venue="v", pool="p", ts_ms=ts, slot=ts, sig=f"s{ts}{side}{wallet}",
                         side=side, base_amount=qty / price, quote_amount=qty,
                         quote_mint="SOL", price=price, wallet=wallet,
                         quote_reserve_after=res, base_reserve_after=res / price)


class TestBars(unittest.TestCase):
    def test_dollar_bar_closes_on_volume_not_time(self):
        b = BarBuilder("dollar", threshold=10.0)
        out = [b.push(swap(1000 + i * 999_999, 1.0, 3.0)) for i in range(3)]
        self.assertEqual([o for o in out if o], [])          # 9 < 10, no close
        bar = b.push(swap(9_000_000, 1.0, 3.0))
        self.assertIsNotNone(bar)
        self.assertAlmostEqual(bar.buy_quote, 12.0)

    def test_unattributed_trades_count_for_volume_but_not_breadth(self):
        """The invariant that keeps the breadth rail honest: an anonymous trade
        is real volume and an unmeasured wallet. Never both, never neither."""
        b = BarBuilder("dollar", threshold=10.0)
        b.push(swap(1, 1.0, 6.0, wallet=None))
        bar = b.push(swap(2, 1.0, 6.0, wallet=None))
        self.assertIsNotNone(bar)
        self.assertAlmostEqual(bar.buy_quote, 12.0)          # volume counted in full
        self.assertIsNone(bar.unique_buyers)                 # breadth UNMEASURED, not 0
        self.assertIsNone(bar.largest_buyer_share)           # not 0 -- 0 reads as "broad"
        self.assertFalse(bar.attribution_complete)

    def test_mixed_attribution_shares_over_attributed_volume_only(self):
        b = BarBuilder("dollar", threshold=10.0)
        b.push(swap(1, 1.0, 4.0, wallet="a"))
        b.push(swap(2, 1.0, 4.0, wallet=None))
        bar = b.push(swap(3, 1.0, 4.0, wallet="b"))
        self.assertIsNotNone(bar)
        self.assertEqual(bar.unique_buyers, 2)
        # a and b each did 4 of the 8 ATTRIBUTED sol; the anonymous 4 must not
        # dilute a real wallet's measured share of the buying it actually did.
        self.assertAlmostEqual(bar.largest_buyer_share, 0.5)

    def test_creator_selling_is_measured_at_bar_level(self):
        b = BarBuilder("dollar", threshold=5.0, creator="dev")
        b.push(swap(1, 1.0, 2.0, side="sell", wallet="dev"))
        bar = b.push(swap(2, 1.0, 4.0, side="sell", wallet="other"))
        self.assertIsNotNone(bar)
        self.assertAlmostEqual(bar.creator_sold_quote, 2.0)
        # A MAGNITUDE, not a boolean. v3 exited fully on any creator dust sale:
        # 428 fires, -14.8%, 18% of the book's basis.
        self.assertGreater(bar.sell_quote, bar.creator_sold_quote)

    def test_time_bar_closes_on_the_first_swap_past_the_boundary(self):
        b = BarBuilder("time", threshold=1000)
        self.assertIsNone(b.push(swap(0, 1.0, 1.0)))
        self.assertIsNone(b.push(swap(500, 1.0, 1.0)))
        bar = b.push(swap(1500, 1.0, 1.0))
        self.assertIsNotNone(bar)
        self.assertEqual(bar.trade_count, 2)

    def test_ohlc(self):
        b = BarBuilder("dollar", threshold=3.0)
        b.push(swap(1, 1.0, 1.0)); b.push(swap(2, 3.0, 1.0))
        bar = b.push(swap(3, 2.0, 1.0))
        self.assertEqual((bar.open, bar.high, bar.low, bar.close), (1.0, 3.0, 1.0, 2.0))


if __name__ == "__main__":
    unittest.main()
