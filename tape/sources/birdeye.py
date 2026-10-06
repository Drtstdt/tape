"""Birdeye adapter -- KEPT FALLBACK, not primary.

Status: D16 made this the primary feed while Bitquery's account auth was
broken. D17 (docs/DECISIONS.md) reverted primary back to Bitquery
(`tape/sources/bitquery.py`) once that auth issue resolved. This file is kept,
not deleted, in case Bitquery's auth ever breaks again -- the same courtesy
D16 extended to `bitquery.py` while it was sidelined. No further effort goes
into this adapter unless Bitquery goes down again; the schema below has never
been `probe`-verified.

Plan if ever reactivated: Starter (8M CUs/mo, 15 RPS) for parsed swaps across
pump.fun, PumpSwap and the major AMMs, paired with Helius Developer ($49/mo)
for pool state and creation events. IMPORTANT: Starter's base $99/mo does NOT
include WebSocket access -- it is a separate toggle, roughly $26/mo more,
making the real cost $125/mo for Birdeye alone and $174/mo total with Helius
(about $24/mo over the original budget range -- was accepted at the time, see
D16). Without the WS toggle, Starter is REST-only (historical() might still
work off it) and has no live per-token trade stream at all -- do not build
`stream()` against the base plan and expect it to receive anything. Also has
no backfill/archive endpoint at any price, unlike Bitquery.

Two subscriptions cover the whole pipeline, confirmed against Birdeye's own
docs (not guessed -- verify again with `probe` before trusting any of it):

  SUBSCRIBE_TOKEN_NEW_LISTING   universe-wide discovery. Takes `sources` (named
                                 sources include "pump_dot_fun" and "pump_amm",
                                 i.e. pump.fun and PumpSwap) and a `min_liquidity`
                                 filter -- so the $13k floor (rails.py) can be
                                 applied AT DISCOVERY, before a socket is ever
                                 opened for a token that would fail the rail
                                 anyway. Still re-check the rail downstream:
                                 never trust a vendor-side filter as the safety
                                 rail itself, only as a cost-saving pre-filter.

  SUBSCRIBE_TXS                 per-token trade stream, address-scoped. THIS IS
                                 THE ARCHITECTURAL DIFFERENCE FROM BITQUERY:
                                 Bitquery's model was one shared socket for the
                                 whole universe; Birdeye's is one subscription
                                 PER TOKEN ADDRESS. `stream()` below must open
                                 and close per-token subscriptions dynamically
                                 as tokens clear discovery and expire, on one
                                 websocket connection (Starter, WITH the WS
                                 add-on enabled, allows multiple concurrent
                                 subscriptions on one socket -- do not open a
                                 new TCP connection per token, that will
                                 exhaust the plan's connection limit fast).

BEFORE trusting this adapter (do not skip -- a schema you have not verified is
the same risk as a decoder you have not verified, in vendor clothing):

  1. `python -m tape.scripts.probe --mint <a mint you already have tapes for>`
  2. Diff every field against your own decoder's output for that mint.
  3. Confirm the side convention on at least 100 unambiguous trades.
  4. Confirm what a SUBSCRIBE_TXS message actually contains field-by-field --
     Birdeye's own docs do not enumerate this exhaustively; it must be learned
     empirically from a live message, the same way v3 learned pump.fun's
     TradeEvent layout.
  5. Only then run a backfill.
"""

from __future__ import annotations

import os
from typing import AsyncIterator, Iterator

from ..schema import CanonicalSwap
from . import SourceAdapter

WS_URL = "wss://public-api.birdeye.so/socket/solana"
REST_BASE = "https://public-api.birdeye.so"

# Confirmed named sources (docs.birdeye.so/docs/subscribe_token_new_listing).
# "pump_amm" is PumpSwap -- the post-graduation venue the strategy review says
# to actually trade. Extend this list once B_mid/C_deep sources are confirmed
# (Raydium, Orca, Meteora); do not assume they use the same source strings
# without checking a live response.
SOURCE_PUMP_FUN = "pump_dot_fun"
SOURCE_PUMPSWAP = "pump_amm"

DISCOVERY_SUBSCRIBE = {
    "type": "SUBSCRIBE_TOKEN_NEW_LISTING",
    "meme_platform_enabled": True,
    "sources": [SOURCE_PUMP_FUN, SOURCE_PUMPSWAP],
}


def trade_subscribe(mint: str) -> dict:
    return {"type": "SUBSCRIBE_TXS", "data": {"queryType": "simple", "address": mint}}


def trade_unsubscribe(mint: str) -> dict:
    return {"type": "UNSUBSCRIBE_TXS", "data": {"queryType": "simple", "address": mint}}


class BirdeyeSource(SourceAdapter):
    name = "birdeye"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("BIRDEYE_API_KEY")
        if not self.api_key:
            raise RuntimeError("BIRDEYE_API_KEY not set")

    def _ws_url(self) -> str:
        return f"{WS_URL}?x-api-key={self.api_key}"

    def historical(self, mint: str, start_ms: int, end_ms: int) -> Iterator[CanonicalSwap]:
        raise NotImplementedError(
            "REST trade-history endpoint -- confirm the exact path and field "
            "names via `probe` before implementing. Birdeye's public docs do "
            "not enumerate a parsed-trade REST response schema as precisely "
            "as they do the websocket subscribe messages; do not assume the "
            "websocket and REST payloads share field names."
        )

    async def stream(self) -> AsyncIterator[CanonicalSwap]:  # pragma: no cover
        raise NotImplementedError(
            "One websocket connection for the whole universe, not one per "
            "token: on connect, send DISCOVERY_SUBSCRIBE; on each new-listing "
            "event that clears the rails (see policy.Rails, especially "
            "min_liquidity_usd), send trade_subscribe(mint); on a token's "
            "tracking window expiring, send trade_unsubscribe(mint) so the "
            "connection does not accumulate dead subscriptions. Required "
            "headers: Origin, Sec-WebSocket-Protocol: echo-protocol (see "
            "module docstring). Verify actual SUBSCRIBE_TXS payload fields "
            "empirically -- they are not fully documented -- before mapping "
            "them onto CanonicalSwap, especially `side` (get this backwards "
            "and every flow feature in the system silently inverts)."
        )

    def discover(self, start_ms: int, end_ms: int) -> Iterator[dict]:
        raise NotImplementedError(
            "Live discovery comes from the DISCOVERY_SUBSCRIBE stream above, "
            "not a historical REST call -- Birdeye's new-listing feed is "
            "push-only. For BACKFILL discovery (finding what launched in a "
            "past window), use BigQuery's public Solana dataset or Helius's "
            "creation-event history instead; do not try to reconstruct past "
            "listings from Birdeye, which has no documented endpoint for it. "
            "Whichever source is used, select on POOL CREATION TIME, never on "
            "an outcome ('reached $X liquidity', 'top movers') -- that trains "
            "on a universe you cannot identify at entry time."
        )
