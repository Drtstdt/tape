"""Decisive pump.fun coverage test for `Trading.Trades`, after
`bitquery_trading_protocol_check.py` came back inconclusive (2026-09-21,
docs/DECISIONS.md D28/D29): `Pair.Market.Protocol`/`ProtocolFamily` are
plain `String` fields, not GraphQL enums, so the schema itself can't tell
us whether "pump"/"pumpswap" is a value that ever appears there. The only
way left to answer this is a live trade.

The problem with picking a "live pump.fun mint" by hand: it's a moving
target. Whatever's freshly launched today is graduated or dead by the time
this gets reviewed, so a hardcoded mint address goes stale immediately.
Instead this script FINDS one at run time, using the cube this project has
already verified end-to-end (`DEXTradeByTokens`, `dataset: realtime`,
confirmed ~9-12h retention -- D21, and the exact query shape production
`tape/sources/bitquery.py` uses, `Dex { ProtocolName ProtocolFamily
ProgramAddress }` included):

  Step 1: pull the most recent SOL-quoted trades across ALL mints (no mint
  filter -- that's the point, we don't know which mint yet), and keep the
  ones whose MintAddress ends in "pump" -- the vanity-mining suffix
  pump.fun mints are known to use (`BITQUERY_EXAMPLE_MINT` itself ends in
  "pump"). This also prints what `DEXTradeByTokens` itself calls these
  trades' `Dex.ProtocolName`/`ProtocolFamily` -- a second, independent data
  point on Bitquery's own protocol-naming convention for pump.fun, from a
  cube already known to see it.

  Step 2: take the freshest such mint and immediately query
  `Trading.Trades` (reusing `bitquery_trading_smoketest`'s already-tested
  `_run`/query, not a new hand-rolled one) for a "last 24h" window. If
  pump.fun trades for a mint that JUST traded on `DEXTradeByTokens` show
  up here too, that's real, live, decisive evidence of coverage. If this
  comes back empty for a mint we can PROVE traded seconds ago, that's
  equally decisive the other way -- no more ambiguity about "maybe the
  mint had already graduated" (D28's caveat) because we picked one trading
  right now.

    python -m tape.scripts.bitquery_pumpfun_cross_check
    python -m tape.scripts.bitquery_pumpfun_cross_check --scan 200  # widen discovery pool
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..env import load_project_dotenv
from ..sources.bitquery import SOL_MINT
from .bitquery_smoketest import ENDPOINT
from .bitquery_trading_smoketest import _extract_trades, _protocols, _run as _run_trading

DISCOVERY_QUERY = """
query Discover($solMint: String!, $limit: Int!) {
  Solana(dataset: realtime) {
    DEXTradeByTokens(
      limit: {count: $limit}
      orderBy: {descending: Block_Time}
      where: {
        Trade: { Side: { Currency: { MintAddress: { is: $solMint } } } }
      }
    ) {
      Block { Time }
      Trade {
        Currency { MintAddress Symbol }
        Dex { ProtocolName ProtocolFamily ProgramAddress }
        Side { Type Amount }
        Amount
        Price
      }
    }
  }
}
"""


def _run_discovery(limit: int, api_key: str) -> dict:
    import httpx

    resp = httpx.post(
        ENDPOINT,
        json={"query": DISCOVERY_QUERY, "variables": {"solMint": SOL_MINT, "limit": limit}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    load_project_dotenv()
    api_key = os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", type=int, default=100,
                     help="how many recent DEXTradeByTokens rows to scan for pump.fun mints")
    args = ap.parse_args()

    print(f"Step 1: scanning the {args.scan} most recent SOL-quoted DEXTradeByTokens "
          f"trades (dataset: realtime, all mints) for pump.fun-style ('...pump') "
          f"mint addresses\n")
    try:
        body = _run_discovery(args.scan, api_key)
    except Exception as e:  # noqa: BLE001
        print(f"  request failed: {e}")
        return 1
    if body.get("errors"):
        print(f"  GraphQL errors: {json.dumps(body['errors'], indent=2)}")
        return 1
    rows = body.get("data", {}).get("Solana", {}).get("DEXTradeByTokens")
    if rows is None:
        print(f"  unexpected response shape: {json.dumps(body, indent=2)[:2000]}")
        return 1
    print(f"  {len(rows)} rows scanned")

    candidates = []  # (block_time, mint, protocol_name, protocol_family)
    for row in rows:
        trade = row.get("Trade") or {}
        currency = trade.get("Currency") or {}
        mint = currency.get("MintAddress") or ""
        if not mint.endswith("pump"):
            continue
        dex = trade.get("Dex") or {}
        candidates.append((
            (row.get("Block") or {}).get("Time"),
            mint,
            dex.get("ProtocolName"),
            dex.get("ProtocolFamily"),
        ))

    if not candidates:
        print("\n  No '...pump'-suffixed mints found in this scan window -- try "
              "--scan with a larger number, or pump.fun activity is quiet right now.")
        return 0

    # Freshest first (rows already came back ordered by Block_Time descending,
    # but dedupe by mint while preserving that order).
    seen = set()
    deduped = []
    for entry in candidates:
        if entry[1] in seen:
            continue
        seen.add(entry[1])
        deduped.append(entry)

    print(f"\n  Found {len(deduped)} distinct '...pump' mint(s) trading right now. "
          f"DEXTradeByTokens' own Dex.ProtocolName/ProtocolFamily for these:")
    for block_time, mint, proto_name, proto_family in deduped[:5]:
        print(f"    {block_time}  {mint}  ProtocolName={proto_name!r}  ProtocolFamily={proto_family!r}")

    print("\nStep 2: cross-checking the freshest of these against Trading.Trades "
          "(last 24h window) -- does the OTHER cube see the SAME mint trading?\n")
    for block_time, mint, proto_name, proto_family in deduped[:3]:
        print(f"  mint {mint} (last seen trading on DEXTradeByTokens at {block_time}):")
        try:
            trading_body = _run_trading(mint, before_days=0, after_days=1, api_key=api_key)
        except Exception as e:  # noqa: BLE001
            print(f"    request failed: {e}")
            continue
        if trading_body.get("errors"):
            print(f"    GraphQL errors: {trading_body['errors']}")
            continue
        trades = _extract_trades(trading_body)
        if trades is None:
            print(f"    unexpected response shape: {json.dumps(trading_body, indent=2)[:1000]}")
            continue
        if not trades:
            print(f"    0 trades in Trading.Trades for this mint in the last 24h "
                  f"-- despite it trading on DEXTradeByTokens moments ago.")
        else:
            print(f"    {len(trades)} trades found! protocols seen: {sorted(_protocols(trades))}")
            print(f"    first: {json.dumps(trades[0], indent=2)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
