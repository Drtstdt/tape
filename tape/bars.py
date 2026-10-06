"""Swap stream -> bar stream.

Why not time bars
-----------------
Memecoin activity is violently non-uniform: the same ten seconds can hold two
hundred trades or zero. Time bars therefore oversample dead periods and
undersample exactly the moments that matter -- which is mechanically why v1's
analysis found a median time-to-peak of 0 seconds.

A dollar bar closes when a fixed amount of quote volume has traded. Bar returns
come out far closer to IID and much less heteroskedastic (which is what every
downstream statistic assumes), a quiet token stops contributing thousands of
near-identical rows, and "three bars of confirmation" becomes a statement about
how much trading has happened rather than about the wall clock.

Time bars are still built in parallel for anything genuinely clock-shaped
(liquidity decay, age, holder counts).

The builder is a pure state machine over a swap iterator, so the live engine and
the replay engine get byte-identical bars from the same swaps.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional

from .schema import BUY, Bar, CanonicalSwap


class BarBuilder:
    """Accumulates swaps and emits closed bars.

    Parameters
    ----------
    kind      : "dollar" | "volume" | "time"
    threshold : quote volume per bar (dollar), base volume per bar (volume),
                or milliseconds per bar (time).
    creator   : creator wallet, so creator selling is measured at bar level
                rather than re-derived later from a trade list nobody kept.
    insiders  : cluster wallets treated as one actor.
    """

    __slots__ = ("kind", "threshold", "creator", "insiders", "_cur", "_buyers", "_sellers")

    def __init__(self, kind: str = "dollar", threshold: float = 1.0,
                 creator: Optional[str] = None, insiders: Optional[set] = None) -> None:
        if kind not in ("dollar", "volume", "time"):
            raise ValueError(f"unknown bar kind {kind!r}")
        if threshold <= 0:
            raise ValueError("threshold must be positive")
        self.kind = kind
        self.threshold = float(threshold)
        self.creator = creator
        self.insiders = insiders or set()
        self._cur: Optional[dict] = None
        self._buyers: dict = {}
        self._sellers: set = set()

    # -- public ---------------------------------------------------------

    def push(self, swap: CanonicalSwap) -> Optional[Bar]:
        """Feed one swap. Returns a closed Bar, or None.

        A time bar closes on the FIRST swap at or past its boundary, and that
        swap starts the next bar. It is never emitted by the clock alone -- a
        bar that closes with no trade in it carries no information and would
        make `trade_count == 0` rows, which every consumer then has to special
        case. `flush_time_bars` exists for the live engine, which does need the
        clock to advance state.
        """
        if self._cur is None:
            self._open(swap)
            self._accumulate(swap)
            return self._maybe_close(swap.ts_ms)

        if self.kind == "time" and swap.ts_ms >= self._cur["open_ts_ms"] + self.threshold:
            closed = self._close(self._cur["last_ts"])
            self._open(swap)
            self._accumulate(swap)
            return closed

        self._accumulate(swap)
        return self._maybe_close(swap.ts_ms)

    def feed(self, swaps: Iterable[CanonicalSwap]) -> Iterator[Bar]:
        for s in swaps:
            bar = self.push(s)
            if bar is not None:
                yield bar

    def flush(self) -> Optional[Bar]:
        """Close whatever is open. Use at end of tape, or at session shutdown.

        A partial bar is real data, but it is NOT comparable to a full one --
        it crossed less volume. Consumers must check `trade_count` before
        treating a flushed bar as a normal observation.
        """
        if self._cur is None:
            return None
        return self._close(self._cur["last_ts"])

    # -- internals ------------------------------------------------------

    def _open(self, swap: CanonicalSwap) -> None:
        self._cur = {
            "mint": swap.mint,
            "venue": swap.venue,
            "open_ts_ms": swap.ts_ms,
            "last_ts": swap.ts_ms,
            "open": swap.price,
            "high": swap.price,
            "low": swap.price,
            "close": swap.price,
            "buy_quote": 0.0,
            "sell_quote": 0.0,
            "buy_count": 0,
            "sell_count": 0,
            "trade_count": 0,
            "attributed_count": 0,
            "base_vol": 0.0,
            "quote_reserve_open": swap.quote_reserve_after,
            "quote_reserve_close": swap.quote_reserve_after,
            "base_reserve_close": swap.base_reserve_after,
            "creator_sold_quote": 0.0,
            "insider_sold_quote": 0.0,
        }
        self._buyers = {}
        self._sellers = set()

    def _accumulate(self, swap: CanonicalSwap) -> None:
        c = self._cur
        assert c is not None
        c["last_ts"] = swap.ts_ms
        c["close"] = swap.price
        if swap.price > c["high"]:
            c["high"] = swap.price
        if swap.price < c["low"]:
            c["low"] = swap.price
        c["trade_count"] += 1
        c["base_vol"] += swap.base_amount
        if swap.quote_reserve_after is not None:
            c["quote_reserve_close"] = swap.quote_reserve_after
            if c["quote_reserve_open"] is None:
                c["quote_reserve_open"] = swap.quote_reserve_after
        if swap.base_reserve_after is not None:
            c["base_reserve_close"] = swap.base_reserve_after

        known = swap.wallet is not None
        if known:
            c["attributed_count"] += 1

        if swap.side == BUY:
            c["buy_count"] += 1
            c["buy_quote"] += swap.quote_amount
            if known:
                self._buyers[swap.wallet] = self._buyers.get(swap.wallet, 0.0) + swap.quote_amount
        else:
            c["sell_count"] += 1
            c["sell_quote"] += swap.quote_amount
            if known:
                self._sellers.add(swap.wallet)
                if swap.wallet == self.creator:
                    c["creator_sold_quote"] += swap.quote_amount
                if swap.wallet in self.insiders:
                    c["insider_sold_quote"] += swap.quote_amount

    def _progress(self) -> float:
        c = self._cur
        assert c is not None
        if self.kind == "dollar":
            return c["buy_quote"] + c["sell_quote"]
        if self.kind == "volume":
            return c["base_vol"]
        return float(c["last_ts"] - c["open_ts_ms"])

    def _maybe_close(self, now_ms: int) -> Optional[Bar]:
        if self.kind == "time":
            return None
        if self._progress() >= self.threshold:
            return self._close(now_ms)
        return None

    def _close(self, close_ts: int) -> Bar:
        c = self._cur
        assert c is not None

        # Wallet-shaped fields need at least one ATTRIBUTED trade before they
        # mean anything. With zero, "how many buyers" is not a measured zero --
        # it is unmeasured, and must stay null so the breadth rail fails closed
        # rather than reading a fabricated 0 as "perfectly broad buying".
        has_attr = c["attributed_count"] > 0
        attributed_buy = sum(self._buyers.values())
        if has_attr and attributed_buy > 0:
            largest = max(self._buyers.values()) / attributed_buy
            hhi = sum((v / attributed_buy) ** 2 for v in self._buyers.values())
        else:
            largest = None
            hhi = None

        bar = Bar(
            mint=c["mint"],
            venue=c["venue"],
            kind=self.kind,
            open_ts_ms=c["open_ts_ms"],
            close_ts_ms=max(close_ts, c["last_ts"]),
            open=c["open"], high=c["high"], low=c["low"], close=c["close"],
            buy_quote=c["buy_quote"], sell_quote=c["sell_quote"],
            buy_count=c["buy_count"], sell_count=c["sell_count"],
            trade_count=c["trade_count"], attributed_count=c["attributed_count"],
            unique_buyers=len(self._buyers) if has_attr else None,
            unique_sellers=len(self._sellers) if has_attr else None,
            largest_buyer_share=largest,
            buy_hhi=hhi,
            quote_reserve_close=c["quote_reserve_close"],
            base_reserve_close=c["base_reserve_close"],
            quote_reserve_open=c["quote_reserve_open"],
            creator_sold_quote=c["creator_sold_quote"],
            insider_sold_quote=c["insider_sold_quote"],
        )
        self._cur = None
        self._buyers = {}
        self._sellers = set()
        return bar


def band_bar_threshold(quote_liquidity: float, fraction: float = 0.01,
                       floor: float = 0.05) -> float:
    """Bar size as a fraction of pool depth.

    One bar should be a comparable ECONOMIC event in a $13k pool and a $500k
    one, otherwise a deep pool produces one bar an hour and a thin one produces
    a bar a second, and every "N bars" rule means something different per band.
    """
    return max(floor, fraction * max(quote_liquidity, 0.0))
