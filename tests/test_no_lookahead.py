"""The test that makes the no-lookahead guarantee real.

A look-ahead leak does not raise, does not fail a type check and does not look
wrong in a diff. It shows up as a backtest better than reality -- the failure
mode that gets ACTED ON instead of investigated. The only reliable defence is
making the future physically absent from the function's scope, and then proving
it with this.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from tape.bars import BarBuilder
from tape.features import TokenState
from tape.schema import CanonicalSwap


def tape(n=60, seed=7):
    """Deterministic pseudo-random tape. No numpy, so the test has no deps."""
    swaps, price, state, res = [], 1.0, seed, 200.0
    for i in range(n):
        state = (1103515245 * state + 12345) % (1 << 31)
        r = state / (1 << 31)
        price *= (1.0 + (r - 0.45) * 0.12)
        side = "buy" if r > 0.42 else "sell"
        qty = 0.4 + r * 2.0
        res += qty if side == "buy" else -qty
        swaps.append(CanonicalSwap(
            mint="M", venue="v", pool="p", ts_ms=1_000 + i * 900, slot=i,
            sig=f"sig{i}", side=side, base_amount=qty / price, quote_amount=qty,
            quote_mint="SOL", price=price, wallet=f"w{i % 9}",
            quote_reserve_after=res, base_reserve_after=res / price))
    return swaps


def feature_rows(swaps):
    st = TokenState("M", created_ts_ms=0, total_supply=1e9)
    bb = BarBuilder("dollar", threshold=6.0)
    rows = []
    for s in swaps:
        bar = bb.push(s)
        if bar is not None:
            st.update(bar)
            rows.append(dict(st.features()))
    return rows


class TestNoLookahead(unittest.TestCase):
    def test_truncating_the_future_does_not_change_the_past(self):
        full = tape(60)
        rows_full = feature_rows(full)
        for cut in (15, 30, 45):
            rows_cut = feature_rows(full[:cut])
            self.assertLessEqual(len(rows_cut), len(rows_full))
            for i, row in enumerate(rows_cut):
                # Bit-identical, key by key. Not "close enough".
                self.assertEqual(row, rows_full[i],
                                 f"feature row {i} changed when the tape was cut at {cut}: "
                                 "something read forward")

    def test_state_is_order_dependent_not_set_dependent(self):
        """Sanity check on the test itself: if shuffling the tape produced the
        same features, the features would not be reading the tape at all and
        the test above would pass vacuously."""
        full = tape(40)
        a = feature_rows(full)
        b = feature_rows(list(reversed(full)))
        self.assertNotEqual(a[-1], b[-1])


if __name__ == "__main__":
    unittest.main()
