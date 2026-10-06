# Data — what to buy, what to collect, how to store it

Researched 2026-09-20, revised 2026-09-20 twice: once to swap to Birdeye over a
Bitquery account auth failure (D16), once to swap back once that auth issue was
resolved (D17, docs/DECISIONS.md). Prices change; re-check before committing.

## 1. The purchase, in one table

| vendor | plan | price | what it gives you | why it is on the list |
|---|---|---:|---|---|
| **Bitquery** | Pro (annual) | **$79/mo** | parsed DEX trades over GraphQL and websocket — wallet, side, base/quote amounts, market address, USD price, signature, slot, across every Solana venue in one schema | the wide universe you asked for, one shared socket for the whole universe (not per-token), without writing one decoder per program |
| **Helius** | Developer | **$49/mo** | 10M credits, 50 RPS, WSS subscriptions, preprocessed transactions at 0.1 credit/message, historical access | pool account state, creation events, and your already-verified pump.fun decoder as a cross-check |

**Steady state: $128/mo.** Inside budget. Add the archive dataset
(from $100/mo, see below) only for the months you're actively backfilling, then
drop back to Pro.

### Why Bitquery over the alternatives

- **Birdeye** — the temporary fallback while Bitquery's account auth was
  broken (D16). Kept, not deleted (`tape/sources/birdeye.py`), in case
  Bitquery's auth ever breaks again: it's a reasoned-through, if unverified,
  adapter with real websocket details already worked out. Not used as primary
  because it costs more once you count the WebSocket toggle Birdeye doesn't
  include in its advertised Starter price ($125/mo just for Birdeye, vs
  $79/mo for Bitquery Pro) and has no backfill/archive endpoint at all — its
  new-listing feed is push-only, so backfill would have meant the BigQuery +
  Solana Tracker workaround below instead of one paid archive call.
- **Architectural note, now moot but worth remembering if D16 is ever
  re-triggered:** Bitquery's model is one shared websocket for the whole
  universe; Birdeye's `SUBSCRIBE_TXS` is one subscription **per token
  address**, multiplexed over a single connection — a materially different
  `stream()` implementation, not just a different URL.
- **Solana Tracker** — €50 Advanced is 200K REST calls/month with **no
  websocket at all**; Datastream only unlocks at €397 Premium, well outside
  budget. Keep as a REST-only backfill option for specific pools once
  discovered (`GET /trades/{token}/{pool}`), not as the primary feed.
- **Dune** — as of September 2026 the free tier is view-only and paid starts at
  $399/mo. Priced out. (It was the natural SQL-over-history answer before that.)
- **Flipside** — the pricing domain now redirects elsewhere; treat the service
  as unavailable until confirmed otherwise.
- **Yellowstone gRPC** (Helius Business $499, Shyft $199+) — buys sub-second
  latency and *still hands you raw transactions*, so you would pay for speed you
  cannot use and then write the decoders anyway. Excluded on both counts.

### Backfill, via Bitquery's archive add-on

**Corrected 2026-09-21 (D21, docs/DECISIONS.md) — do not trust the "~30-day
rolling window" figure below for planning; it is what Bitquery's docs say,
not what this account's `dataset: realtime` actually does.** Empirically
bisected via `tape/scripts/bitquery_smoketest.py --bisect` against a mint
Bitquery's own docs use as a worked example (confirmed continuously active):
10 trades/hour-window at 1h/2h/4h/6h/9h ago, **zero at 12h and every wider
offset tested (18h/24h/30h/36h)**. `realtime`'s actual reach on this plan is
roughly **9–12 hours**, not 30 days. `dataset: combined` is not merely
"redundant" as the docs imply — it returns a flat 403 Forbidden on this
account, confirmed directly.

Practical effect: `archive` is required for anything older than about half a
day, not just anything older than a month. A bot that goes offline overnight
needs `archive` to catch up on that gap, not just for deep historical
backfill — budget for that when deciding whether to keep `archive` off
between backfill runs (§1 above).

Bitquery's live dataset is documented as a ~30-day rolling window; anything
older needs `dataset: archive`, a separate add-on (from $100/mo, confirmed live on Bitquery's pricing page 2026-09-21 -- select just Solana, not all 17 chains). Buy it for the months you
backfill, then drop back to Pro. This is what turns "wait two weeks for tapes"
into "have six months of history by Tuesday" — the one thing Birdeye could not
do at all, at any price, since its new-listing feed has no documented history
endpoint.

Keep Google BigQuery's public Solana dataset (free, below) wired up anyway, as
an independent cross-check on vendor data rather than as the primary backfill
path — disagreement on a safety-critical field is itself a signal.

### Free sources worth wiring

- **Google BigQuery public Solana dataset** (`bigquery-public-data`, Solana
  mainnet). Raw — you decode instructions yourself — but date-partitioned and
  1 TB/month of query is free, so targeted historical pulls cost nothing. Use
  it as an independent cross-check on Bitquery's archive dataset (§1), not as
  the primary backfill path now that the archive add-on covers that.
  Disagreement on a safety-critical field is itself a signal.
- **RugCheck** (`api.rugcheck.xyz`) — keep exactly v3's usage: the *named*
  risks, `lpLockedPct`, and `topHolders`. Never the aggregate danger flag; v1
  measured `hasDangerRisk=True` tokens as having a *higher* win rate and
  *negative* total PnL, which is why v1's `volatilityHarvester` ended net short.
- **Your own tapes.** 477 tokens/day already, and the creator/wallet graph
  built from them is the one input no vendor sells.

## 2. Coverage matrix — what you need each source for

| signal | Bitquery | Helius | BigQuery | own decoder |
|---|:--:|:--:|:--:|:--:|
| per-swap side / amount / **wallet** | ✅ primary (needs field-level verification — §5 `probe`) | ⚠️ pump.fun only | ✅ (decode) | ⚠️ pump.fun + PumpSwap |
| new-token discovery, live | ✅ | ✅ creation events | — | ✅ |
| pool reserves / liquidity depth | ❌ not inline | ✅ account sub | ✅ | ✅ |
| token creation events + creator | ⚠️ | ✅ primary | ✅ | ✅ |
| mint/freeze authority, LP lock | — | ✅ | ✅ | — |
| named rug risks, top holders | — | — | — | RugCheck |
| historical backfill | ✅ `dataset: archive` add-on | ⚠️ | ✅ free | — |

Note: Bitquery does not give pool reserves inline. Treat depth as unverified
until `probe` confirms otherwise, and assume it does not — depth comes from
Helius account subscriptions live, and from BigQuery offline. Keep them as
separate fields in the canonical schema — never infer one from the other.

## 3. Canonical schema — the only thing downstream sees

Every adapter converts to this and nothing else. Venue specifics die at the
adapter boundary.

```python
CanonicalSwap:
    mint: str              # base token mint
    venue: str             # "pumpfun" | "pumpswap" | "raydium_v4" | ...
    pool: str              # market/pool address
    ts_ms: int             # block time, ms
    slot: int
    sig: str               # transaction signature — the dedup key
    side: str              # "buy" | "sell", from the BASE token's perspective
    base_amount: float     # base token units (decimals applied)
    quote_amount: float    # quote units, SOL or USDC
    quote_mint: str
    price: float           # quote per base, computed once, here
    wallet: str | None     # None means UNATTRIBUTED — never a fabricated id
    base_reserve_after:  float | None
    quote_reserve_after: float | None
    source: str            # which adapter produced this row
```

Two invariants carried forward from v3 because they were learned expensively:

1. **`wallet=None` is not a wallet.** A vault-derived or unattributed trade
   counts in full for every volume-shaped field and contributes *nothing* to
   unique-buyer counts or largest-buyer share. Giving anonymous trades synthetic
   ids reports perfectly broad buying and opens the breadth gate; giving them
   one shared id reports a single whale and rejects everything. Both are worse
   than the gap.
2. **`None` ≠ `0`.** A zero net flow says "we watched and nothing happened". A
   null says "we were not watching". Collapsing them is how a fail-closed gate
   silently becomes an open door — this exact bug voided a live run.

## 4. Storage

```
data/
  swaps/venue=<v>/dt=<YYYY-MM-DD>/part-*.parquet      # CanonicalSwap
  bars/kind=<dollar|volume|time>/dt=<…>/part-*.parquet
  pools/dt=<…>/part-*.parquet                          # reserve snapshots
  meta/tokens.parquet                                  # mint -> creator, launchpad, created_ts
  graph/creator_stats.parquet, wallet_stats.parquet    # rebuilt, never appended
  market/regime.parquet                                # chain-wide aggregates, 1/min
tape.duckdb                                            # views over the above
```

Why not JSONL: today's 20 MB/day becomes tens of GB once the universe widens,
and you will re-read the whole corpus thousands of times while iterating on
features. DuckDB over partitioned Parquet gives you predicate pushdown, columnar
reads and SQL over hundreds of millions of rows on a laptop with no server. The
difference is minutes versus hours per iteration, multiplied by every iteration
you will ever run.

**Dedup on `sig` + `mint` + `side`.** Running two sources simultaneously is the
point (corroboration), so duplicates are expected and must be idempotent.

## 5. Backfill plan

1. `probe` one mint you already have tapes for, against Bitquery, and diff
   every field against your own decoder's output. Do not skip this — v3's
   worst run was caused by a decoder silently returning nothing, and a schema
   you have not verified is the same risk in vendor clothing.
2. Discover the universe **on a point-in-time event**: pool creation, migration,
   first-seen-in-block-range. Never on anything only knowable later ("reached
   $X liquidity", "top movers") — that trains on a universe you cannot identify
   at entry time and produces a beautiful backtest and a losing bot.
3. Pull 3–6 months of swaps for every discovered pool. Store raw, then build
   bars, then features, each stage to disk and re-runnable alone.
4. Rebuild the creator/wallet graph from the full backfill **with strict
   as-of-time semantics**: a creator's rug rate on day T may only use creations
   before day T. This is the easiest place in the whole project to leak the
   future, and the leak looks like a fantastic feature.
