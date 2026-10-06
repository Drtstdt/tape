"""Real trades query against Bitquery's `Trading.Trades` cube -- the part
`bitquery_trading_introspect.py` deliberately did NOT do (see its own
docstring). That script only confirmed the FIELD NAMES exist; this script
asks the two questions D26 (docs/DECISIONS.md) flagged as still open:

  1. RETENTION: for a SPECIFIC mint (not just "Solana network" generically,
     which is all `tet.py`'s test proved), how far back does `Trading.Trades`
     actually have data? `tet.py` only showed the cube has SOMETHING at
     25-27 days ago for the whole Solana network -- it never filtered to one
     mint, so it says nothing about whether any single token's history is
     that deep.
  2. COVERAGE: do any of the rows that come back actually show
     `Pair.Market.Protocol`/`ProtocolFamily` values for pump.fun/PumpSwap,
     or does this cube only see larger/more liquid DEX pairs? A cube can
     have 90 days of retention and still be useless for this project if it
     never saw a bonding-curve trade.

Uses the field names confirmed live via introspection, 2026-09-21:
  mint filter   -> Pair.Token.Address  (OLAP_String, `is`/`like`/`in`)
  network       -> Pair.Market.Network (proven to accept "Solana" in tet.py)
  venue         -> Pair.Market.Protocol / ProtocolFamily / Program
  pool          -> Pair.Pool.Address
  quote token   -> Pair.QuoteToken.Address / IsNative
  signature     -> TransactionHeader.Hash
  wallet        -> Trader.Address
  side/amounts  -> Side, Amounts{Base Quote}, AmountsInUsd{Base Quote}, Price
  timestamp     -> Block.Date, Block.Time

Reuses `BITQUERY_EXAMPLE_MINT` from `bitquery_smoketest.py` -- it's a
pump.fun-style mint Bitquery's own docs use, and the same one D19-D21 already
exercised against `DEXTradeByTokens`, so a result here is directly
comparable to that cube's confirmed ~9-12h retention instead of testing an
unrelated token.

    python -m tape.scripts.bitquery_trading_smoketest --bisect
    python -m tape.scripts.bitquery_trading_smoketest --mint <other mint> --bisect
    python -m tape.scripts.bitquery_trading_smoketest --days 30   # single window, now-30d..now
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from ..env import load_project_dotenv
from .bitquery_smoketest import BITQUERY_EXAMPLE_MINT, ENDPOINT

TRADES_QUERY = """
query TradingBisect($mint: String!, $beforeDays: Int!, $afterDays: Int!) {
  Trading {
    Trades(
      limit: {count: 10}
      orderBy: {descending: Block_Time}
      where: {
        Pair: {
          Token: { Address: { is: $mint } }
          Market: { Network: { is: "Solana" } }
        }
        Block: {
          Date: {
            before_relative: { days_ago: $beforeDays }
            after_relative: { days_ago: $afterDays }
          }
        }
      }
    ) {
      Block { Date Time }
      Side
      Amounts { Base Quote }
      AmountsInUsd { Base Quote }
      Price
      Pair {
        Token { Address }
        QuoteToken { Address IsNative }
        Market { Protocol ProtocolFamily Program Network }
        Pool { Address }
      }
      TransactionHeader { Hash }
      Trader { Address }
    }
  }
}
"""


def _run(mint: str, before_days: int, after_days: int, api_key: str) -> dict:
    import httpx

    resp = httpx.post(
        ENDPOINT,
        json={
            "query": TRADES_QUERY,
            "variables": {"mint": mint, "beforeDays": before_days, "afterDays": after_days},
        },
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_trades(body: dict) -> list[dict] | None:
    return body.get("data", {}).get("Trading", {}).get("Trades")


def _protocols(trades: list[dict]) -> set[str]:
    out = set()
    for t in trades:
        market = ((t.get("Pair") or {}).get("Market") or {})
        proto = market.get("Protocol") or market.get("ProtocolFamily") or "?"
        out.add(proto)
    return out


def main() -> int:
    load_project_dotenv()
    api_key = os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("--mint", default=BITQUERY_EXAMPLE_MINT)
    ap.add_argument("--bisect", action="store_true",
                     help="try several days-ago window pairs in one run to find "
                          "how far back THIS mint's data actually goes")
    ap.add_argument("--days", type=float, default=1.0,
                     help="single-window mode: window is (now - days) .. now")
    args = ap.parse_args()

    if args.bisect:
        # Widening 1-2 day windows further and further back. If a mint's
        # data runs out at, say, 10 days, everything at or beyond that
        # offset should come back empty while everything before it has
        # trades -- same bisection idea as bitquery_smoketest.py's
        # `--bisect`, just walking days instead of hours since this cube's
        # relative filter (`days_ago`) only has day granularity.
        offsets = [
            (0, 1), (1, 2), (2, 3), (3, 5), (5, 7), (7, 10),
            (10, 14), (14, 21), (21, 30), (30, 45), (45, 60), (60, 90),
        ]
        print(f"Bisecting Trading.Trades retention for mint {args.mint}\n")
        for before_days, after_days in offsets:
            try:
                body = _run(args.mint, before_days, after_days, api_key)
            except Exception as e:  # noqa: BLE001
                print(f"  {before_days:>3}-{after_days:<3}d ago: request failed: {e}")
                continue
            if body.get("errors"):
                print(f"  {before_days:>3}-{after_days:<3}d ago: GraphQL errors: {body['errors']}")
                continue
            trades = _extract_trades(body)
            if trades is None:
                print(f"  {before_days:>3}-{after_days:<3}d ago: unexpected shape: "
                      f"{json.dumps(body, indent=2)[:1000]}")
                continue
            protos = _protocols(trades) if trades else set()
            print(f"  {before_days:>3}-{after_days:<3}d ago: {len(trades)} trades"
                  + (f"  protocols seen: {sorted(protos)}" if trades else ""))
        return 0

    before_days = int(args.days)
    after_days = 0
    print(f"Window: {before_days}d ago .. now  |  mint: {args.mint}\n")
    try:
        body = _run(args.mint, before_days, after_days, api_key)
    except Exception as e:  # noqa: BLE001
        print(f"  request failed: {e}")
        return 1
    if body.get("errors"):
        print(f"  GraphQL errors: {json.dumps(body['errors'], indent=2)}")
        return 1
    trades = _extract_trades(body)
    if trades is None:
        print(f"  unexpected response shape: {json.dumps(body, indent=2)[:2000]}")
        return 1
    print(f"  {len(trades)} trades")
    if trades:
        print(f"  protocols seen: {sorted(_protocols(trades))}")
        print(f"  first: {json.dumps(trades[0], indent=2)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
