"""Source adapters. Venue specifics die at this boundary.

An adapter may only emit `CanonicalSwap`. Nothing downstream ever branches on
where a swap came from -- that single rule is what lets you add Raydium,
Meteora or a new launchpad by writing one file and changing nothing else.

Before trusting ANY adapter, verify its side convention against transactions
you already have decoded. `side` is from the base token's perspective;
getting it backwards silently inverts every flow feature in the system and
nothing will raise. v3 confirmed its own convention on 367 of 367 unambiguous
recorded pairs before shipping it -- do the same here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Iterator, Optional

from ..schema import CanonicalSwap


class SourceAdapter(ABC):
    name: str = "abstract"

    @abstractmethod
    def historical(self, mint: str, start_ms: int, end_ms: int) -> Iterator[CanonicalSwap]:
        """Swaps for one mint, ascending by ts_ms. Must be deterministic:
        calling twice returns identical rows in identical order."""

    @abstractmethod
    async def stream(self) -> AsyncIterator[CanonicalSwap]:
        """Live swaps, as they land."""

    def discover(self, start_ms: int, end_ms: int) -> Iterator[dict]:
        """New pools/tokens by POINT-IN-TIME event (creation, migration).

        Never by an outcome. See Store.mints() for why this matters more than
        it looks like it does.
        """
        raise NotImplementedError


class ReplaySource(SourceAdapter):
    """The offline adapter -- and the one that matters most, because it is what
    makes the backtester and the live bot the same program."""

    name = "replay"

    def __init__(self, store, mint: Optional[str] = None) -> None:
        self.store = store
        self.mint = mint

    def historical(self, mint: str, start_ms: int, end_ms: int) -> Iterator[CanonicalSwap]:
        for s in self.store.iter_swaps(mint):
            if start_ms <= s.ts_ms <= end_ms:
                yield s

    async def stream(self):  # pragma: no cover - replay has no live mode
        raise NotImplementedError("ReplaySource is offline only")


def corroborate(primary: list, secondary: list, tol: float = 1e-6) -> dict:
    """Compare two sources over the same pool.

    Agreement is cheap insurance; disagreement is an alarm. v3's worst run was
    caused by a decoder silently returning nothing for an entire session, and
    NOTHING said so. A second source that sees the same pool turns that from a
    silent void into a loud mismatch.

    Returns counts, not a verdict -- the caller decides what an acceptable
    mismatch rate is, and logs it.
    """
    by_key = {s.dedup_key: s for s in primary}
    agree = side_mismatch = amount_mismatch = only_secondary = 0
    for s in secondary:
        p = by_key.get(s.dedup_key)
        if p is None:
            by_sig = [x for x in primary if x.sig == s.sig and x.mint == s.mint]
            if not by_sig:
                only_secondary += 1
                continue
            p = by_sig[0]
        if p.side != s.side:
            side_mismatch += 1
        elif abs(p.quote_amount - s.quote_amount) > tol * max(1.0, p.quote_amount):
            amount_mismatch += 1
        else:
            agree += 1
    return {
        "agree": agree,
        "side_mismatch": side_mismatch,
        "amount_mismatch": amount_mismatch,
        "only_in_secondary": only_secondary,
        "only_in_primary": max(0, len(primary) - agree - side_mismatch - amount_mismatch),
    }
