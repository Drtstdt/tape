"""Bitquery adapter -- the primary feed.

Primary again as of D17 (docs/DECISIONS.md): briefly swapped for Birdeye
(D16) over an account authentication failure; reverted once that auth issue
was resolved. `tape/sources/birdeye.py` is kept as the reasoned-through
fallback if this ever breaks again.

Plan: Pro ($79/mo annual) for parsed DEX trades across every Solana venue over
GraphQL and websocket. The live dataset is a ~30-day rolling window; anything
older needs `dataset: archive`, which is a separate add-on (from $100/mo -- confirmed live, not the $70 originally estimated). Buy the
add-on for the months you backfill, then drop back to Pro.

What Bitquery gives you per trade: wallet address (buyer/seller account and
transaction signer), side, base/quote amounts, market address, USD price,
signature, slot. What it does NOT give inline: pool reserves. Depth comes from
Helius account subscriptions live and the Pools API or BigQuery offline -- keep
them as separate fields and NEVER infer one from the other.

D19 (2026-09-21, docs/DECISIONS.md): the `/eap` endpoint used here originally
is Bitquery's LEGACY access point ("Early Access Program" -- confirmed via
Bitquery's own docs: existing EAP customers keep working, new accounts get
403 Forbidden). Endpoint is now `/graphql`, the current v2 path. `dataset`
changed `combined` -> `realtime` for the same reason: Bitquery's docs say
`combined` "adds nothing" on Solana (it is an EVM-trace distinction there);
`realtime` is the free tier, `archive` is the paid add-on for >30-day history.

D20 (2026-09-21, docs/DECISIONS.md): `DEXTrades` (the query type below used to
use) returned zero rows with no error even against a mint with 10,693 swaps
in the same window per v3's own tape -- a silent-empty failure, not a loud
one. Live schema introspection (`python -m tape.scripts.bitquery_introspect`)
showed the CURRENT schema's `DEXTradeByTokens` `Trade` where-filter has no
`Buy`/`Sell` fields at all -- only `Side`/`Currency`/`Amount`/`Price`/etc.
Switched to `DEXTradeByTokens`, filtering `Trade.Currency` = the queried mint
and `Trade.Side.Currency` = `SOL_MINT`, so `Trade.Side.Amount` is
unambiguously the SOL leg and `Trade.Side.Type` is Bitquery's buy/sell label.

D23 (2026-09-21, docs/DECISIONS.md): side convention CONFIRMED, independent
of Bitquery entirely. `tape/scripts/parse_bigquery_export.py` reconstructed
real swaps for a live mint straight from Solana ledger balance deltas (no
vendor, no decoder) and compared against v3's own tape: `corr(tape_buy,
vendor_buy) = 0.804`, `corr(tape_sell, vendor_sell) = 0.653` -- both clearly
the right pairing, not swapped. `Trade.Side.Type == "buy"` means the QUERIED
MINT (`Trade.Currency`) was bought -- satisfies D3's side-convention
requirement. (That same run also found v3's own historical tapes likely
undercount real volume by ~57% from their 10-second-snapshot collection
method -- a caveat about v3's OLD data, not about this adapter.)

ALSO IMPORTANT, not yet handled by this adapter: v2 auth tokens are OAuth
access tokens (`ory_at_...`), not static API keys, and they EXPIRE (~5 hours
per Bitquery's docs). A token pasted into `.env` today will start failing
auth partway through a long backfill or a live `stream()` run. Before
`historical()`/`stream()` go into real use, add a client_id/client_secret
refresh flow (POST to `https://oauth2.bitquery.io/oauth2/token`) rather than
relying on a hand-copied token outliving the run. Not blocking for `probe`,
which runs in seconds -- blocking for anything unattended.

BEFORE trusting this adapter (do not skip -- a schema you have not verified is
the same risk as a decoder you have not verified, in vendor clothing):

  1. `python -m tape.scripts.probe --mint <a mint you already have tapes for>`
  2. Diff every field against your own decoder's output for that mint.
  3. Confirm the side convention on at least 100 unambiguous trades.
  4. Only then run a backfill.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time
from typing import AsyncIterator, Iterator, Optional

from ..env import load_project_dotenv
from ..schema import CanonicalSwap
from . import SourceAdapter

ENDPOINT = "https://streaming.bitquery.io/graphql"
WS_ENDPOINT = "wss://streaming.bitquery.io/graphql"

# D21's empirical bisection found the boundary between 9h and 12h ago (10
# trades/window at 9h, 0 at 12h). Use the conservative (smaller) end so a
# request right at the edge fails loud with a clear message instead of
# silently returning zero rows the way the raw API does.
REALTIME_RETENTION_HOURS = 9
PAGE_LIMIT = 1000

# D44 (2026-09-22, docs/DECISIONS.md): CONFIRMED LIVE -- neither
# `historical()` nor `discover()` had any rate-limit handling at all before
# this. A real run of `discover()` over a full day's worth of pump.fun/
# PumpSwap activity paged fast enough (no inter-page delay, network-latency
# bound only) to trip Bitquery's own per-minute rate limit within seconds:
# `HTTP 429 ... "access restricted by rate limit: too many requests per
# minute"`. Two mitigations, same reasoning as Helius's `_rpc_call` 429
# handling (`tape/sources/helius.py`) -- retry-with-backoff as the actual
# fix (never crash the whole discovery/backfill run over a transient rate
# limit), plus a small inter-page delay as a first line of defense so the
# common case doesn't need to retry at all. Both numbers below are
# deliberately conservative starting points, not measured ceilings -- there
# is no visibility yet into Bitquery's actual requests-per-minute allowance
# for this plan; tune once a real run reports how often retries actually
# fire.
BITQUERY_MAX_RETRIES = 6
BITQUERY_RETRY_BACKOFF_CAP_S = 60.0  # the limit is per-MINUTE, so backoff
                                      # must be able to reach a full minute
INTER_PAGE_DELAY_S = 0.5

# The native SOL mint -- filtering `Trade.Side.Currency` to exactly this
# makes `Trade.Side.Amount` unambiguously a SOL amount instead of "whatever
# the counter-currency happened to be". A quote-currency other than SOL
# (USDC pairs, say) needs a second query with a different `solMint`/quote
# value -- not handled here, out of scope until the universe actually
# includes non-SOL-quoted pools.
SOL_MINT = "So11111111111111111111111111111111111111112"

# D52 (2026-09-22, docs/DECISIONS.md): CONFIRMED LIVE -- D46 fixed
# discover() yielding SOL_MINT itself as a "discovered mint" (a real
# backfill run hung on it), but the same failure mode recurred with USDC:
# a real Phase 2 run spent 400+ pages (300k+ "swaps") on
# EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v before being caught. Both
# addresses verified against Solana's own explorer + Solscan (2026-09-22),
# not guessed from memory -- same sourcing bar as PUMPFUN_PROGRAM/
# PUMPSWAP_PROGRAM above. Generalized from a single SOL_MINT check to a
# small set of known settlement/quote currencies: any of these ending up
# as `Trade.Currency` in a discover() row means the SAME thing SOL_MINT
# leaking there meant -- it's the quote leg of someone else's trade, not a
# pump.fun-launched token, and `historical(mint=<this>, ...)` against it
# means BOTH sides of the trade filter effectively collapse onto a single
# real, extremely liquid pair (USDC/SOL, USDT/SOL) instead of a real
# pump.fun token -- slow and pointless, not a hang exactly, but a huge
# waste of real page-fetch time and Bitquery quota for data this project
# doesn't want. This list is deliberately NOT exhaustive (any other
# stablecoin/quote currency could leak the same way) -- revisit if a
# future run burns pages on some OTHER well-known non-pump.fun mint.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
KNOWN_QUOTE_MINTS = frozenset({SOL_MINT, USDC_MINT, USDT_MINT})

# D39 (2026-09-22, docs/DECISIONS.md): the `archive` add-on is now PURCHASED
# ($100/mo, user-confirmed) -- `dataset` is a real constructor parameter as
# of this change, not hardcoded `realtime` any more. Still defaults to
# `realtime` for backward compatibility with every existing call site and
# test that predates the purchase. IMPORTANT, NOT YET VERIFIED: nobody has
# run a real query with `dataset: archive` against `DEXTradeByTokens` in
# this project -- D21's ~9-12h retention floor was measured against
# `realtime` only, and D26-D30's "archive reaches further back" evidence
# was on `Trading.Trades`, a DIFFERENT cube that D30 separately proved does
# NOT cover pump.fun/PumpSwap at all. Whether `archive` on `DEXTradeByTokens`
# specifically reaches back further, and how far, is a real open question
# -- run `tape/scripts/bitquery_archive_smoketest.py` (new) FIRST, before
# trusting this for a real backfill. The realtime retention-floor guard
# below is therefore only enforced for `dataset: realtime` -- `archive`
# has no hardcoded floor because none has been empirically established yet
# (an unenforced floor is honest; a wrong guessed one is not).
#
# `DEXTradeByTokens`, not `DEXTrades` -- see D20. Side comes from
# `Trade.Side.Type`, not a `Buy`/`Sell` nesting that this schema version
# doesn't have on this query type.
DATASET_REALTIME = "realtime"
DATASET_ARCHIVE = "archive"
VALID_DATASETS = (DATASET_REALTIME, DATASET_ARCHIVE)

# Verified 2026-09-22 against pump.fun's own public docs repo
# (`pump-fun/pump-public-docs`), cross-checked against Solscan -- same
# source and same verification this project already used for
# `tape/scripts/bigquery_discover.py` (D38). Used here for `discover()`'s
# `Trade.Dex.ProgramAddress` filter -- membership only, never for decoding
# instruction accounts/data.
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

# Bitquery's documented OAuth2 client-credentials endpoint for v2 access
# tokens (bitquery.py's own module docstring flagged this as needed
# "before historical()/stream() go into real use" -- D39 is that point,
# now that a real unattended archive backfill is actually being planned).
# NOT YET VERIFIED against a live response -- this project has only ever
# run on a hand-pasted static token until now. Only used if BOTH
# `client_id` and `client_secret` are supplied; a bare `api_key` behaves
# exactly as before (no refresh attempted), so this is additive, not a
# behaviour change for existing callers.
OAUTH_TOKEN_URL = "https://oauth2.bitquery.io/oauth2/token"
TOKEN_REFRESH_MARGIN_S = 60.0


def _parse_bq_iso_ms(iso_str: str) -> Optional[int]:
    """`Block.Time` -> epoch ms, or None (never a guess) if the format
    doesn't parse. Mirrors `tape/scripts/probe.py::_parse_block_time_ms`
    exactly -- kept as a separate local copy rather than imported, since
    `sources/` is the production module and `scripts/` depends on it, not
    the other way around (same reasoning as `_to_canonical` below).
    """
    if not iso_str:
        return None
    s = iso_str.strip().replace("Z", "+00:00")
    try:
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def _ms_to_bq_iso(ms: int) -> str:
    """Inverse of `_parse_bq_iso_ms` -- epoch ms -> the `Z`-suffixed
    RFC3339 string Bitquery's `DateTime!` variables expect. Bitquery's
    `Block.Time` has only ever been seen at whole-second resolution (see
    `historical()`'s pagination docstring), but this keeps millisecond
    precision on the way out regardless -- truncating here would just move
    the same-second-collision problem earlier for no benefit.
    """
    d = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


# `dataset: __DATASET__` is a plain string substitution, not a GraphQL
# $variable -- `dataset` is written as a bare (unquoted) enum-like
# identifier in Bitquery's schema, the same way the original hardcoded
# `dataset: realtime` always was, and this project has never introspected
# a `$variable`-typed form of it to know its declared type. Substitution
# is safe here specifically because the value is always one of
# `VALID_DATASETS`, validated in `__init__` -- never free-form input.
TRADES_QUERY_TEMPLATE = """
query TokenTrades($mint: String!, $solMint: String!, $since: DateTime!, $until: DateTime!, $limit: Int!) {
  Solana(dataset: __DATASET__) {
    DEXTradeByTokens(
      limit: {count: $limit}
      orderBy: {ascending: Block_Time}
      where: {
        Trade: {
          Currency: {MintAddress: {is: $mint}}
          Side: {Currency: {MintAddress: {is: $solMint}}}
        }
        Block: {Time: {after: $since, before: $until}}
      }
    ) {
      Block { Time Slot }
      Transaction { Signature Signer }
      Trade {
        Dex { ProtocolName ProtocolFamily ProgramAddress }
        Market { MarketAddress }
        Account { Address }
        Side { Type Amount AmountInUSD Account { Address } }
        Amount
        Price
        PriceInUSD
        Currency { MintAddress Decimals }
      }
    }
  }
}
"""

# D39 -- discover() (new): broad, no-mint scan filtered on the verified
# program ids above instead of a specific mint. NOT YET VERIFIED that
# `Trade.Dex.ProgramAddress` is actually FILTERABLE this way -- the
# existing TRADES_QUERY only ever read it as an OUTPUT field (verified,
# D19-23); using it in a `where` clause is an unverified extension of
# that, flagged the same way the OAuth flow above is.
DISCOVER_QUERY_TEMPLATE = """
query DiscoverPumpTrades($programs: [String!], $since: DateTime!, $until: DateTime!, $limit: Int!) {
  Solana(dataset: __DATASET__) {
    DEXTradeByTokens(
      limit: {count: $limit}
      orderBy: {ascending: Block_Time}
      where: {
        Trade: { Dex: { ProgramAddress: { in: $programs } } }
        Block: { Time: { after: $since, before: $until } }
      }
    ) {
      Block { Time Slot }
      Transaction { Signature }
      Trade {
        Currency { MintAddress }
        Market { MarketAddress }
      }
    }
  }
}
"""


def _raise_for_status_with_body(resp) -> None:
    """`httpx`'s own `raise_for_status()` gives you the HTTP status and
    nothing else -- a bare '403 Forbidden' with no indication of WHICH of
    several possible causes (expired token, wrong dataset entitlement,
    wrong product purchased entirely) produced it. Bitquery, like most
    GraphQL APIs, puts the actual reason in the response BODY even on a
    4xx -- surface it instead of throwing it away. Real motivating case,
    2026-09-22: a `dataset: archive` call 403'd with a valid, long-lived
    token that worked fine on `dataset: realtime` moments earlier -- the
    body is the only thing that can distinguish 'archive isn't entitled on
    this key' from 'this account's archive purchase is a different
    product (OHLCV/Transfers archive, not DEXTradeByTokens) that doesn't
    apply to this query at all' from a dozen other explanations a bare
    status code can't tell apart."""
    if resp.status_code < 400:
        return
    body_preview = resp.text[:2000]
    raise RuntimeError(
        f"Bitquery HTTP {resp.status_code} for {resp.request.url}. "
        f"Response body (first 2000 chars): {body_preview!r}"
    )


def _post_with_retry(url: str, payload: dict, headers: dict, timeout: float = 30.0):
    """POST with 429 retry/backoff (D44, docs/DECISIONS.md) -- confirmed
    live necessary: a real `discover()` run tripped Bitquery's per-minute
    rate limit within seconds of unthrottled paging. Mirrors
    `tape/sources/helius.py::_rpc_call`'s pattern (Retry-After header if
    present, else exponential backoff capped at BITQUERY_RETRY_BACKOFF_CAP_S)
    -- kept as a separate copy rather than shared code, same reasoning as
    every other per-adapter helper in this project (sources/ modules don't
    depend on each other). Returns the response UNCHECKED for non-429
    errors -- caller still calls `_raise_for_status_with_body` itself, so a
    real 4xx/5xx (wrong dataset entitlement, bad query, etc.) still surfaces
    its body instead of being swallowed here."""
    import httpx

    for attempt in range(1, BITQUERY_MAX_RETRIES + 1):
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait_s = float(retry_after) if retry_after else min(2 ** attempt, BITQUERY_RETRY_BACKOFF_CAP_S)
            print(f"[bitquery] 429 rate limited, waiting {wait_s:.0f}s "
                  f"(retry {attempt}/{BITQUERY_MAX_RETRIES})", file=sys.stderr)
            time.sleep(wait_s)
            continue
        return resp
    raise RuntimeError(
        f"Still getting 429 from {url} after {BITQUERY_MAX_RETRIES} retries "
        f"with backoff -- a real, sustained rate limit, not a transient blip."
    )


def _fetch_oauth_token(client_id: str, client_secret: str) -> tuple:
    """(access_token, expires_at_epoch_s). Standard OAuth2 client_credentials
    shape per Bitquery's own docs -- NOT YET VERIFIED against a live
    response (D39, docs/DECISIONS.md): this project has only ever run on a
    hand-pasted static token until now, exactly the gap `bitquery.py`'s own
    module docstring flagged. A KeyError/shape mismatch here on the first
    real run means Bitquery's actual response shape differs from this
    assumption -- treat that as new information to log, not a bug to
    silently work around."""
    import httpx

    resp = httpx.post(OAUTH_TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=30.0)
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]
    expires_in = float(body.get("expires_in", 3600))
    return token, time.time() + expires_in


class BitquerySource(SourceAdapter):
    name = "bitquery"

    def __init__(self, api_key: str | None = None, quote_mint: str | None = None,
                 dataset: str = DATASET_REALTIME,
                 client_id: str | None = None, client_secret: str | None = None) -> None:
        load_project_dotenv()   # picks up tape/.env if the shell doesn't have it
        self.api_key = api_key or os.environ.get("BITQUERY_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "BITQUERY_API_KEY not set. Put `BITQUERY_API_KEY=...` (no quotes "
                "needed, but they're stripped if you use them) on its own line in "
                "tape/.env, or set it as a real environment variable."
            )
        if dataset not in VALID_DATASETS:
            raise ValueError(f"dataset must be one of {VALID_DATASETS}, got {dataset!r}")
        self.dataset = dataset
        self.quote_mint = quote_mint
        # OAuth refresh (D39) is opt-in: only attempted if BOTH client_id and
        # client_secret are supplied (directly or via env) -- a bare
        # api_key behaves exactly as before, no behaviour change for any
        # existing caller or test.
        self.client_id = client_id or os.environ.get("BITQUERY_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("BITQUERY_CLIENT_SECRET")
        self._token_expires_at: Optional[float] = None

    def _ensure_fresh_token(self) -> None:
        """No-op in static-token mode (no client_id/secret). Otherwise
        refreshes `self.api_key` in place once it's within
        TOKEN_REFRESH_MARGIN_S of expiry -- called before every HTTP
        request in `historical()`/`discover()` so a multi-hour archive
        backfill survives past the ~5h token lifetime `bitquery.py`'s
        module docstring has flagged since D19, instead of dying partway
        through with an auth failure."""
        if self.client_id is None or self.client_secret is None:
            return
        if self._token_expires_at is not None and \
                time.time() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S:
            return
        self.api_key, self._token_expires_at = _fetch_oauth_token(self.client_id, self.client_secret)

    def historical(self, mint: str, start_ms: int, end_ms: int) -> Iterator[CanonicalSwap]:
        """Swaps for `mint` in `[start_ms, end_ms]`, ascending by ts_ms.

        Checks the request against D21's ~9h empirical `realtime` retention
        boundary FIRST and fails loud if it's out of reach -- the raw API
        would otherwise return an empty, error-free result indistinguishable
        from "this mint genuinely has no trades" (exactly the D20 failure
        mode this project already got burned by once).

        Pagination note: Bitquery's `Block.Time` has only whole-second
        resolution in every sample seen so far, and a busy pool can have many
        trades in one second -- more than fit in one page. Naively advancing
        the cursor to "the last row's timestamp" and requerying `after` that
        exact second risks silently dropping same-second trades ordered
        after the page boundary. This re-requests FROM the last seen second
        (not after it) and relies on `sig`-based dedup to skip repeats,
        trading a little redundant work for no silent gaps. If a single
        second ever has >= PAGE_LIMIT trades, this raises rather than
        silently truncating -- has not happened in any window inspected so
        far, but "has not happened yet" is not the same as "cannot happen"
        on a busy enough pool.
        """
        import httpx

        if self.dataset == DATASET_REALTIME:
            now_ms = int(dt.datetime.utcnow().timestamp() * 1000)
            retention_floor_ms = now_ms - REALTIME_RETENTION_HOURS * 3600 * 1000
            if start_ms < retention_floor_ms:
                raise RuntimeError(
                    f"Requested start_ms={start_ms} is older than `dataset: realtime`'s "
                    f"~{REALTIME_RETENTION_HOURS}h empirical reach (D21, docs/DECISIONS.md). "
                    "This would silently return zero rows, not an error, if not caught here. "
                    "Construct BitquerySource(dataset='archive', ...) for anything older "
                    "-- but verify its real reach first (bitquery_archive_smoketest.py, D39)."
                )
        # dataset == "archive": no hardcoded floor -- its real reach is not
        # yet empirically established (D39). Silence here is deliberate,
        # not an oversight: a wrong guessed floor is worse than none.

        query = TRADES_QUERY_TEMPLATE.replace("__DATASET__", self.dataset)
        since_ms, seen_sigs = start_ms, set()
        stall_guard_last_since = None
        page_num = 0
        while since_ms <= end_ms:
            page_num += 1
            self._ensure_fresh_token()
            since_iso = _ms_to_bq_iso(since_ms)
            until_iso = _ms_to_bq_iso(end_ms + 1)  # +1ms: `before` -- make end_ms itself inclusive
            resp = _post_with_retry(
                ENDPOINT,
                {"query": query,
                 "variables": {"mint": mint, "solMint": SOL_MINT,
                               "since": since_iso, "until": until_iso,
                               "limit": PAGE_LIMIT}},
                {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )
            _raise_for_status_with_body(resp)
            body = resp.json()
            if body.get("errors"):
                raise RuntimeError(f"Bitquery returned errors: {body['errors']}")
            try:
                trades = body["data"]["Solana"]["DEXTradeByTokens"]
            except (KeyError, TypeError) as e:
                raise RuntimeError(
                    f"Response shape did not match TRADES_QUERY's expected fields -- "
                    f"schema may have drifted again. Full response:\n{body}"
                ) from e

            if not trades:
                break

            new_this_page = 0
            max_ts_this_page = since_ms
            for raw in trades:
                swap = self._to_canonical(raw, mint)
                if swap is None:
                    continue
                max_ts_this_page = max(max_ts_this_page, swap.ts_ms)
                if swap.sig in seen_sigs:
                    continue
                seen_sigs.add(swap.sig)
                new_this_page += 1
                yield swap

            if page_num % 5 == 0 or len(trades) < PAGE_LIMIT:
                # D51 (2026-09-22, docs/DECISIONS.md): CONFIRMED LIVE --
                # historical() had ZERO progress printing per page, unlike
                # every other long-running loop in this project (Helius's
                # _fetch_signatures, discover()'s own caller in
                # backfill_bitquery.py). A single busy mint with many pages
                # in its window produced total silence for minutes, visually
                # indistinguishable from a hang -- exactly the failure mode
                # this project's own standing rule ("never go silent on a
                # loop that can run this long", tape/sources/helius.py)
                # already exists to prevent elsewhere. User killed a real,
                # in-progress (not stuck) backfill run over this.
                print(f"[bitquery] {mint}: ... page {page_num}, "
                      f"{len(seen_sigs)} unique swap(s) so far", file=sys.stderr)

            if len(trades) < PAGE_LIMIT:
                break  # last page

            if new_this_page == 0:
                if stall_guard_last_since == since_ms:
                    raise RuntimeError(
                        f"Pagination stalled at {since_iso}: a full page of "
                        f"{PAGE_LIMIT} trades, all already seen, at the same second. "
                        "This means a single second has >= PAGE_LIMIT trades for this "
                        "mint -- raise PAGE_LIMIT rather than risk silently truncating."
                    )
                stall_guard_last_since = since_ms
            since_ms = max_ts_this_page  # re-include this second next page; see docstring
            time.sleep(INTER_PAGE_DELAY_S)  # D44: avoid re-tripping the rate limit

    async def stream(self) -> AsyncIterator[CanonicalSwap]:  # pragma: no cover
        """Live swaps over `WS_ENDPOINT`, one shared socket for the whole
        universe -- per-mint sockets will exhaust the stream-minute allowance.

        UNVERIFIED (unlike `historical()`): D19-D23 verified the HTTP query
        path (`DEXTradeByTokens` over `/graphql`) end to end against real
        data. The WebSocket *subscription* shape below is written by analogy
        -- same field names, `subscription` instead of `query`, no `dataset`
        argument (a live stream has no historical dataset to pick) -- but has
        NOT been introspected or run against a real socket in this session.
        Auth over WS also differs from HTTP: Bitquery's docs say the token
        goes in the URL (`wss://.../graphql?token=...`), not an Authorization
        header, because WebSocket handshakes don't carry custom headers the
        same way. None of this is confirmed live. Before this is trusted:

          1. Connect and confirm the connection_ack / subscription confirms.
          2. Compare a few minutes of live output against `probe`'s tape-vs-
             vendor windowing for a currently-trading mint (extend
             `bucket_bitquery_windows` to accept a live iterator instead of
             a fixed list, or capture N minutes to a list first).
          3. Only then treat this as verified the way `historical()` is.
        """
        raise NotImplementedError(
            "Query shape is drafted (see docstring) but not yet run against a real "
            "socket. Needs a `websockets`-based implementation subscribing to "
            "`DEXTradeByTokens` (same where-clause as TRADES_QUERY, no `dataset` "
            "argument, no `limit`/`orderBy`) at "
            f"`{WS_ENDPOINT}?token=<api_key>`, converting each pushed trade with "
            "the same `_to_canonical` this adapter's `historical()` uses, and "
            "verifying per the docstring above before real use."
        )

    def _to_canonical(self, raw: dict, mint: str) -> Optional[CanonicalSwap]:
        """One `DEXTradeByTokens` row -> `CanonicalSwap`, or None if a field
        this needs is missing/unparsable. Never guesses a value -- a dropped
        row is a visible gap (caller can count `None`s), a fabricated one is
        an invisible lie. Mirrors `tape/scripts/probe.py::parse_bitquery_trade`
        (kept separate rather than imported, since a script depending on this
        production module is the right direction, not the other way around).
        """
        try:
            ts_ms = _parse_bq_iso_ms(raw["Block"]["Time"])
            slot = int(raw["Block"]["Slot"])
            sig = raw["Transaction"]["Signature"]
            side_type = raw["Trade"]["Side"]["Type"]
            side = side_type.strip().lower()
            if side not in ("buy", "sell"):
                return None
            quote_amount = float(raw["Trade"]["Side"]["Amount"])
            base_amount = float(raw["Trade"]["Amount"])
            price = float(raw["Trade"]["Price"])
            pool = raw["Trade"].get("Market", {}).get("MarketAddress") or ""
            dex = raw["Trade"].get("Dex") or {}
            venue = dex.get("ProtocolName") or dex.get("ProtocolFamily") or "unknown"
            wallet = (raw["Trade"].get("Account") or {}).get("Address") or None
        except (KeyError, TypeError, ValueError):
            return None
        if ts_ms is None:
            return None
        return CanonicalSwap(
            mint=mint,
            venue=str(venue).lower(),
            pool=pool,
            ts_ms=ts_ms,
            slot=slot,
            sig=sig,
            side=side,
            base_amount=base_amount,
            quote_amount=quote_amount,
            quote_mint=SOL_MINT,
            price=price,
            wallet=wallet,
            source=self.name,
        )

    def discover(self, start_ms: int, end_ms: int, programs: Optional[list] = None) -> Iterator[dict]:
        """D39 (2026-09-22, docs/DECISIONS.md): the actual universe-scaling
        step, now that the `archive` add-on is paid for -- a broad,
        no-mint-filter scan of `DEXTradeByTokens`, filtered to real,
        primary-sourced pump.fun/PumpSwap program ids (`PUMPFUN_PROGRAM`,
        `PUMPSWAP_PROGRAM` above) instead of a specific mint. Yields each
        mint the FIRST time it's seen trading against one of `programs` in
        [start_ms, end_ms] -- a point-in-time INCLUSION event (D18: never
        select on an outcome like 'reached $X liquidity'), not a claim
        about the mint's actual on-chain creation time; a mint already
        trading before `start_ms` will show up with whatever its first
        trade INSIDE this window happens to be.

        NOT YET VERIFIED (same discipline as everything else in this
        project before a real backfill): whether `Trade.Dex.ProgramAddress`
        is filterable in a `where` clause at all (it has only ever been
        read as an output field, D19-23) -- run
        `tape/scripts/bitquery_archive_smoketest.py`'s discovery check
        first.
        """
        programs = programs or [PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM]
        import httpx

        query = DISCOVER_QUERY_TEMPLATE.replace("__DATASET__", self.dataset)
        since_ms, seen_sigs, seen_mints = start_ms, set(), set()
        stall_guard_last_since = None
        while since_ms <= end_ms:
            self._ensure_fresh_token()
            since_iso = _ms_to_bq_iso(since_ms)
            until_iso = _ms_to_bq_iso(end_ms + 1)
            resp = _post_with_retry(
                ENDPOINT,
                {"query": query,
                 "variables": {"programs": programs, "since": since_iso,
                               "until": until_iso, "limit": PAGE_LIMIT}},
                {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )
            _raise_for_status_with_body(resp)
            body = resp.json()
            if body.get("errors"):
                raise RuntimeError(f"Bitquery returned errors: {body['errors']}")
            try:
                trades = body["data"]["Solana"]["DEXTradeByTokens"]
            except (KeyError, TypeError) as e:
                raise RuntimeError(
                    f"Response shape did not match DISCOVER_QUERY_TEMPLATE's expected "
                    f"fields -- schema may have drifted, or ProgramAddress isn't "
                    f"filterable this way after all. Full response:\n{body}"
                ) from e

            if not trades:
                break
            new_this_page = 0
            max_ts_this_page = since_ms
            for raw in trades:
                ts_ms = _parse_bq_iso_ms((raw.get("Block") or {}).get("Time"))
                sig = (raw.get("Transaction") or {}).get("Signature")
                trade = raw.get("Trade") or {}
                mint = (trade.get("Currency") or {}).get("MintAddress")
                pool = (trade.get("Market") or {}).get("MarketAddress")
                if ts_ms is None or sig is None or mint is None:
                    continue
                max_ts_this_page = max(max_ts_this_page, ts_ms)
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                new_this_page += 1
                # D46/D52 (docs/DECISIONS.md): CONFIRMED LIVE -- some
                # DEXTradeByTokens rows report `Trade.Currency.MintAddress`
                # as a known QUOTE currency itself (SOL, then separately
                # confirmed live for USDC too) rather than a token launched
                # on pump.fun/PumpSwap. `historical(mint=<quote currency>,
                # ...)` against one of these either collapses onto a
                # nonsensical same-currency filter (SOL/SOL) or onto a
                # real, extremely liquid, totally unrelated pair (USDC/SOL,
                # 300k+ swaps and counting on a real run) -- never yield
                # any of `KNOWN_QUOTE_MINTS` as a discovered base asset.
                if mint in KNOWN_QUOTE_MINTS:
                    continue
                if mint not in seen_mints:
                    seen_mints.add(mint)
                    yield {"mint": mint, "first_seen_ts_ms": ts_ms, "pool": pool, "sig": sig}

            if len(trades) < PAGE_LIMIT:
                break
            if new_this_page == 0:
                if stall_guard_last_since == since_ms:
                    raise RuntimeError(
                        f"Discovery pagination stalled at {since_iso}: a full page of "
                        f"{PAGE_LIMIT} trades, all already seen, at the same second."
                    )
                stall_guard_last_since = since_ms
            since_ms = max_ts_this_page
            time.sleep(INTER_PAGE_DELAY_S)  # D44: avoid re-tripping the rate limit
