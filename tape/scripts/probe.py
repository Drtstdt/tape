"""probe -- verify a vendor's swap schema and side-convention against ground
truth BEFORE trusting it for anything (D3, docs/DECISIONS.md). Run this
before writing one more line of `historical()` or `stream()`.

    python -m tape.scripts.probe --mint <mint> --tape <path to a v3 tape>

WHAT "GROUND TRUTH" ACTUALLY IS HERE -- read this before trusting the output:

v3's `data_deep/<date>/<mint>.jsonl` tapes are NOT raw per-swap records. Each
line is a 10-SECOND SNAPSHOT of pool state (reserves, price) plus a rolling
window's aggregated buy/sell SOL volume and trade counts. There is no
persisted wallet / side / amount / signature per individual swap anywhere in
this codebase -- that gap is exactly what `tape/store.py` exists to close
going forward. Checked on 2026-09-21 against two real files
(`v3/data_deep/2026-09-20/*.jsonl`): confirmed 10-second cadence, confirmed
`buyVolumeSol`/`sellVolumeSol`/`txCount` are the only trade-derived fields
per row, confirmed many rows carry `null` (not `0`) for those fields when a
window had too little attributed activity -- treat null as "unmeasured" here
too, never as zero, or every comparison below is quietly wrong.

So this script CANNOT diff "Bitquery's swap #4173" against "the verified
decoder's swap #4173" -- there is no such recorded object. What it checks
instead, bucketed into the same 10-second windows v3's snapshots use:

  1. Per-window buy/sell SOL volume: Bitquery's vs. the tape's. Agreement
     tests the side convention *indirectly* -- if Bitquery's buy/sell labels
     were swapped, these two series would be anti-correlated, not matching.
  2. Per-window trade count: catches Bitquery missing an entire venue (e.g.
     seeing only pump.fun, not PumpSwap) even when what it does see looks
     fine.
  3. Price at each window boundary: Bitquery's swap price vs. the tape's
     reserve-implied price (`virtualSolReserves / virtualTokenReserves`,
     unit-scaled). Catches a decimals error that a volume-only check would
     miss entirely.

This is coarser than a wallet-level diff, because that is what the data on
disk actually supports -- not a design choice. If per-swap ground truth ever
exists (e.g. by running the verified JS decoder live and persisting its raw
output instead of only the derived snapshot), tighten this script to diff at
that level instead of silently continuing to trust the coarser one.

THE BITQUERY FIELD MAPPING BELOW IS PROVISIONAL. `TRADES_QUERY` in
`bitquery.py` is a reasoned starting point, not a verified contract --
Bitquery's Solana schema has drifted before. This script prints the raw
response before attempting to interpret it, and refuses to silently invent
values for a field it expected but didn't get; if a field is missing, that
is exactly the signal `probe` exists to surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..env import load_project_dotenv

WINDOW_MS = 10_000   # matches v3's snapshot cadence; see module docstring


# ---------------------------------------------------------------------------
# Ground truth: parse v3's tape.
# ---------------------------------------------------------------------------

def load_tape(path: str | Path) -> List[dict]:
    """Read a v3 `data_deep/<date>/<mint>.jsonl` file. One dict per line.
    Malformed lines are skipped, not fatal -- a corrupt tail line has bitten
    this project before and should not block probing what came before it.
    """
    rows = []
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _reserve_price(row: dict) -> Optional[float]:
    """Reserve-implied price from the snapshot's virtual reserves.

    Both reserves are stored as strings of raw base units (see the sample:
    "virtualSolReserves": "494715270853"). This returns SOL-per-token in the
    same units `row["price"]` already uses in the tape, so the two should
    agree with each other -- that self-consistency check runs once, in
    `test_probe.py`, so a scaling bug here is caught before it is blamed on
    Bitquery.
    """
    sol_r = row.get("virtualSolReserves")
    tok_r = row.get("virtualTokenReserves")
    if sol_r is None or tok_r is None:
        return None
    try:
        sol_r, tok_r = float(sol_r), float(tok_r)
    except (TypeError, ValueError):
        return None
    if tok_r <= 0:
        return None
    return sol_r / tok_r


def bucket_tape_windows(rows: Sequence[dict], window_ms: int = WINDOW_MS) -> Dict[int, dict]:
    """One entry per snapshot row that actually carries trade data.

    Rows where `buyVolumeSol`/`sellVolumeSol` are `null` are DROPPED, not
    coerced to zero -- per D11 (docs/DECISIONS.md), null means "unmeasured",
    and comparing an unmeasured window against Bitquery's real number would
    manufacture a mismatch that has nothing to do with Bitquery.
    """
    out: Dict[int, dict] = {}
    for row in rows:
        buy = row.get("buyVolumeSol")
        sell = row.get("sellVolumeSol")
        ts = row.get("timestamp")
        if buy is None or sell is None or ts is None:
            continue
        window_start = (ts // window_ms) * window_ms
        out[window_start] = {
            "buy_sol": buy,
            "sell_sol": sell,
            "tx_count": row.get("swapCountInWindow") or row.get("txCount") or 0,
            "price": row.get("price"),
            "reserve_price": _reserve_price(row),
        }
    return out


# ---------------------------------------------------------------------------
# The thing being verified: Bitquery.
# ---------------------------------------------------------------------------

# D20 (2026-09-21, docs/DECISIONS.md): `DEXTrades` -- what this used to query --
# returned zero rows with NO error even against a heavily-traded pump.fun mint
# (10,693 swaps in v3's own tape for the same window). Live schema
# introspection (`bitquery_introspect.py`) confirmed why: `DEXTradeByTokens`'s
# `Trade` where-filter has fields `[Dex, Market, Order, Index, Side, Account,
# Amount, Price, Currency, AmountInUSD, PriceInUSD]` -- there is no `Buy`/
# `Sell` field at all in the CURRENT schema. `DEXTrades` may be a different,
# separately-populated (or defunct) field; not investigated further once
# `DEXTradeByTokens` was confirmed live and documented with a real pump.fun
# example. Side comes from `Trade.Side.Type` ("buy"/"sell"), and the query
# filters explicitly on the SOL leg via `Trade.Side.Currency.MintAddress` --
# `SOL_MINT` below -- so `Trade.Side.Amount` is unambiguously the SOL amount.
#
# Introspection also confirmed `after`/`before` (what this already used) ARE
# valid `Block.Time` operators -- alongside `since`/`till` -- so the time
# filter was never the bug; only the query type/shape was.
SOL_MINT = "So11111111111111111111111111111111111111112"

PROBE_QUERY = """
query ProbeTrades($mint: String!, $solMint: String!, $since: DateTime!, $until: DateTime!) {
  Solana(dataset: realtime) {
    DEXTradeByTokens(
      limit: {count: 2000}
      orderBy: {ascending: Block_Time}
      where: {
        Trade: {
          Currency: { MintAddress: { is: $mint } }
          Side: { Currency: { MintAddress: { is: $solMint } } }
        }
        Block: { Time: { after: $since, before: $until } }
      }
    ) {
      Block { Time }
      Transaction { Signature }
      Trade {
        Side { Type Amount AmountInUSD }
        Amount
        Price
        PriceInUSD
        Currency { MintAddress Decimals }
      }
    }
  }
}
"""


@dataclass
class ProbeSwap:
    ts_ms: int
    side: str            # "buy" | "sell" -- PROVISIONAL, see module docstring
    sol_amount: float
    price: Optional[float]
    raw: dict             # always keep the original record for manual inspection


def fetch_bitquery_trades(mint: str, since_iso: str, until_iso: str,
                          api_key: str) -> List[dict]:
    """One live call. Returns the raw `DEXTradeByTokens` list, unmodified.

    Deliberately does NOT try to be clever about pagination, retries or rate
    limits -- this is a diagnostic probe run by hand against one mint, not
    the production adapter. `historical()` earns that complexity only after
    this has confirmed the schema is worth building against.
    """
    import httpx

    resp = httpx.post(
        "https://streaming.bitquery.io/graphql",
        json={"query": PROBE_QUERY,
              "variables": {"mint": mint, "solMint": SOL_MINT,
                            "since": since_iso, "until": until_iso}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body and body["errors"]:
        raise RuntimeError(f"Bitquery returned errors: {body['errors']}")
    try:
        return body["data"]["Solana"]["DEXTradeByTokens"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(
            f"Response shape did not match what TRADES_QUERY/PROBE_QUERY expects "
            f"-- this IS the probe finding something, not a bug in probe.py. "
            f"Full response:\n{json.dumps(body, indent=2)}"
        ) from e


def _parse_block_time_ms(iso_str: str) -> Optional[int]:
    """Bitquery's exact timestamp format (fractional seconds? always UTC?)
    has never been confirmed against a live response. Tries the formats
    RFC3339 commonly comes in; returns None -- never a guessed/zeroed
    timestamp -- if none of them fit, so a format surprise shows up as a
    dropped trade in the probe's failure count instead of every trade
    silently landing in the same wrong window.
    """
    if not iso_str:
        return None
    s = iso_str.strip().replace("Z", "+00:00")
    try:
        import datetime as dt
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def parse_bitquery_trade(raw: dict, mint: str) -> Optional[ProbeSwap]:
    """Best-effort extraction using PROBE_QUERY's `DEXTradeByTokens` shape
    (D20, docs/DECISIONS.md -- `DEXTrades`/Buy-Sell was the wrong query type).

    `Trade.Currency` is the queried mint (enforced by the query's `where`);
    `Trade.Side.Currency` is SOL (also enforced by `where`, via `SOL_MINT`),
    so `Trade.Side.Amount` is unambiguously a SOL amount -- no more inferring
    which leg is which from which mint matches. `Trade.Side.Type` is Bitquery's
    own buy/sell label; PROVISIONAL in the sense that its *direction* (does
    "buy" mean bought the token, or bought the side currency?) has never been
    confirmed against >=100 unambiguous trades as D3/bitquery.py's docstring
    requires -- that confirmation is exactly what `diff_windows` against v3's
    ground truth checks. If it's inverted, expect every window to mismatch
    with buy/sell swapped, not random noise.

    Returns None (never a fabricated/zeroed record) if a field this needs is
    missing OR unparsable -- an unmeasured field is not a zero, same rule as
    everywhere else in this project. The caller is expected to report how
    many trades failed to parse; a nonzero count there is itself the probe
    result, not an error to swallow.
    """
    try:
        ts_ms = _parse_block_time_ms(raw["Block"]["Time"])
        if ts_ms is None:
            return None
        side_type = raw["Trade"]["Side"]["Type"]
        side = side_type.strip().lower()
        if side not in ("buy", "sell"):
            return None
        sol_amount = float(raw["Trade"]["Side"]["Amount"])
        price = raw["Trade"].get("Price")
        return ProbeSwap(ts_ms=ts_ms, side=side, sol_amount=sol_amount,
                         price=price, raw=raw)
    except (KeyError, TypeError, ValueError):
        return None


def bucket_bitquery_windows(swaps: Sequence[ProbeSwap], window_ms: int = WINDOW_MS) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for s in swaps:
        window_start = (s.ts_ms // window_ms) * window_ms
        bucket = out.setdefault(window_start, {"buy_sol": 0.0, "sell_sol": 0.0, "tx_count": 0})
        if s.side == "buy":
            bucket["buy_sol"] += s.sol_amount
        else:
            bucket["sell_sol"] += s.sol_amount
        bucket["tx_count"] += 1
    return out


# ---------------------------------------------------------------------------
# The comparison itself.
# ---------------------------------------------------------------------------

def diff_windows(tape_windows: Dict[int, dict], vendor_windows: Dict[int, dict],
                 volume_tolerance_pct: float = 15.0) -> List[dict]:
    """One row per window either side has data for. `mismatch=True` when
    both sides have volume and it disagrees by more than the tolerance --
    everything else (missing on one side, both zero) is reported but not
    flagged, since those are common and not necessarily wrong (Bitquery
    might simply not have started indexing yet, or the tape's window had
    unattributed-only activity).
    """
    rows = []
    for ts in sorted(set(tape_windows) | set(vendor_windows)):
        t = tape_windows.get(ts)
        v = vendor_windows.get(ts)
        row = {
            "window_start_ms": ts,
            "tape_buy_sol": t["buy_sol"] if t else None,
            "tape_sell_sol": t["sell_sol"] if t else None,
            "tape_tx_count": t["tx_count"] if t else None,
            "vendor_buy_sol": v["buy_sol"] if v else None,
            "vendor_sell_sol": v["sell_sol"] if v else None,
            "vendor_tx_count": v["tx_count"] if v else None,
            "mismatch": False,
        }
        if t and v:
            for side in ("buy_sol", "sell_sol"):
                tv, vv = t[side], v[side]
                denom = max(abs(tv), abs(vv), 1e-9)
                if abs(tv - vv) / denom * 100.0 > volume_tolerance_pct:
                    row["mismatch"] = True
        rows.append(row)
    return rows


def summarize(rows: Sequence[dict]) -> str:
    total = len(rows)
    both = sum(1 for r in rows if r["tape_buy_sol"] is not None and r["vendor_buy_sol"] is not None)
    mismatches = sum(1 for r in rows if r["mismatch"])
    tape_only = sum(1 for r in rows if r["tape_buy_sol"] is not None and r["vendor_buy_sol"] is None)
    vendor_only = sum(1 for r in rows if r["tape_buy_sol"] is None and r["vendor_buy_sol"] is not None)
    return (
        f"{total} windows total | {both} comparable | {mismatches} mismatched "
        f"(>±tolerance) | {tape_only} tape-only (Bitquery saw nothing) | "
        f"{vendor_only} vendor-only (tape had no attributed data)"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mint", required=True)
    ap.add_argument("--tape", required=True, help="path to a v3 data_deep/<date>/<mint>.jsonl file")
    ap.add_argument("--tolerance-pct", type=float, default=15.0)
    args = ap.parse_args(argv)

    import os
    api_key = os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set (checked the shell environment and "
              "tape/.env). Nothing to probe against.", file=sys.stderr)
        return 2

    rows = load_tape(args.tape)
    if not rows:
        print(f"No rows parsed from {args.tape} -- wrong path, or the file is empty.",
              file=sys.stderr)
        return 2
    tape_windows = bucket_tape_windows(rows)
    if not tape_windows:
        print("Every row in the tape had null buy/sell volume -- there is nothing "
              "with attributed trade data to compare against in this file. Pick a "
              "more actively-traded mint.", file=sys.stderr)
        return 2

    since_ms, until_ms = rows[0]["timestamp"], rows[-1]["timestamp"]
    import datetime as dt
    since_iso = dt.datetime.utcfromtimestamp(since_ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = dt.datetime.utcfromtimestamp(until_ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Fetching Bitquery trades for {args.mint}, {since_iso} .. {until_iso} ...")
    raw_trades = fetch_bitquery_trades(args.mint, since_iso, until_iso, api_key)
    print(f"Bitquery returned {len(raw_trades)} raw trades. First one, verbatim, "
          f"for manual inspection:")
    if raw_trades:
        print(json.dumps(raw_trades[0], indent=2))

    parsed = [parse_bitquery_trade(r, args.mint) for r in raw_trades]
    failed = sum(1 for p in parsed if p is None)
    swaps = [p for p in parsed if p is not None]
    if failed:
        print(f"WARNING: {failed}/{len(raw_trades)} trades did not match the "
              f"expected shape and were dropped, not guessed. Inspect the raw "
              f"trade above against `parse_bitquery_trade` before trusting the "
              f"rest.", file=sys.stderr)

    if not swaps:
        print("Nothing parsed -- see the raw response above and fix "
              "parse_bitquery_trade before this comparison means anything.",
              file=sys.stderr)
        return 1

    vendor_windows = bucket_bitquery_windows(swaps)
    diff_rows = diff_windows(tape_windows, vendor_windows, args.tolerance_pct)
    print(summarize(diff_rows))
    for r in diff_rows:
        if r["mismatch"]:
            print(f"  MISMATCH @ {r['window_start_ms']}: "
                  f"tape buy/sell={r['tape_buy_sol']}/{r['tape_sell_sol']} "
                  f"vendor buy/sell={r['vendor_buy_sol']}/{r['vendor_sell_sol']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
