"""Minimal smoke test, no v3 tape required: does `dataset: realtime` +
`DEXTradeByTokens` return ANYTHING for ANY mint on this account, using the
exact query shape `probe.py`/`bitquery.py` now use?

Tests against a mint Bitquery's OWN docs use as their worked example
(`CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump`), over a wide recent window,
with no `Side.Currency` filter at all -- the loosest possible version of the
query. If THIS returns zero, the problem is account-level (entitlement,
dataset access, plan) or query-level, not "this particular mint is too old /
uncovered". If it returns nonzero, the problem is narrower than that.

    python -m tape.scripts.bitquery_smoketest
    python -m tape.scripts.bitquery_smoketest --mint <any other mint>
    python -m tape.scripts.bitquery_smoketest --hours 168   # wider window

`--bisect` mode: 40 hours back had zero trades for a mint Bitquery actively
tracks (confirmed manually); 24 hours back also zero. Rather than keep doing
this one absolute window per round-trip, `--bisect` tries several offsets in
ONE run (1h/2h/4h/6h/9h/12h/18h/24h ago, each a 1-hour window) against
`dataset: realtime` only, and prints where the count drops to zero -- that
boundary is `realtime`'s actual retention, whatever the docs claimed.

    python -m tape.scripts.bitquery_smoketest --bisect
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from ..env import load_project_dotenv

ENDPOINT = "https://streaming.bitquery.io/graphql"
BITQUERY_EXAMPLE_MINT = "CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump"

# Deliberately the loosest possible where-clause: only the mint, no
# Side.Currency filter, no dataset argument at all (let the schema default
# decide) -- if this still comes back empty, nothing narrower will help.
LOOSE_QUERY = """
query Loose($mint: String!, $since: DateTime!, $until: DateTime!) {
  Solana {
    DEXTradeByTokens(
      limit: {count: 10}
      orderBy: {descending: Block_Time}
      where: {
        Trade: { Currency: { MintAddress: { is: $mint } } }
        Block: { Time: { after: $since, before: $until } }
      }
    ) {
      Block { Time }
      Trade { Side { Type Amount } Amount Price Currency { MintAddress } }
    }
  }
}
"""

# Same, but with the explicit `dataset: realtime` argument this project's
# other queries use -- isolates whether THAT argument is what's zeroing
# results out.
REALTIME_QUERY = LOOSE_QUERY.replace("Solana {", "Solana(dataset: realtime) {")

# And with `dataset: combined`, in case `realtime` alone is the narrower of
# the two despite D19's reasoning (worth re-checking empirically now that
# something else has already been wrong twice).
COMBINED_QUERY = LOOSE_QUERY.replace("Solana {", "Solana(dataset: combined) {")


def _run(query: str, mint: str, since_iso: str, until_iso: str, api_key: str) -> dict:
    import httpx

    resp = httpx.post(
        ENDPOINT,
        json={"query": query, "variables": {"mint": mint, "since": since_iso, "until": until_iso}},
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
    ap.add_argument("--mint", default=BITQUERY_EXAMPLE_MINT)
    ap.add_argument("--hours", type=float, default=48.0,
                     help="ignored if --since/--until given; window is (now - hours) .. now")
    ap.add_argument("--since", default=None, help="absolute ISO8601 UTC, e.g. 2026-09-20T00:00:00Z")
    ap.add_argument("--until", default=None, help="absolute ISO8601 UTC")
    ap.add_argument("--bisect", action="store_true",
                     help="try several hours-ago offsets in one run to find the retention boundary")
    args = ap.parse_args()

    if args.bisect:
        now = dt.datetime.utcnow()
        print(f"Bisecting retention boundary for mint {args.mint}, now={now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")
        for hours_ago in [1, 2, 4, 6, 9, 12, 18, 24, 30, 36]:
            window_end = now - dt.timedelta(hours=hours_ago)
            window_start = window_end - dt.timedelta(hours=1)
            since_iso = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            until_iso = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                body = _run(REALTIME_QUERY, args.mint, since_iso, until_iso, api_key)
            except Exception as e:  # noqa: BLE001
                print(f"  {hours_ago:>3}h ago ({since_iso}..{until_iso}): request failed: {e}")
                continue
            if body.get("errors"):
                print(f"  {hours_ago:>3}h ago: GraphQL errors: {body['errors']}")
                continue
            trades = body.get("data", {}).get("Solana", {}).get("DEXTradeByTokens") or []
            print(f"  {hours_ago:>3}h ago ({since_iso}..{until_iso}): {len(trades)} trades")
        return 0

    if args.since and args.until:
        # Absolute window -- use this to test whether `realtime`'s retention
        # reaches a SPECIFIC point in the past, not just "N hours before
        # whenever I happen to run this". A relative --hours window always
        # includes "now" at its tail, so if the mint trades continuously,
        # `orderBy: descending` returns fresh trades regardless of how far
        # back retention actually goes -- it never tests the boundary.
        since_iso, until_iso = args.since, args.until
    else:
        until = dt.datetime.utcnow()
        since = until - dt.timedelta(hours=args.hours)
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        until_iso = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Window: {since_iso} .. {until_iso}  |  mint: {args.mint}\n")

    for label, query in [("no dataset arg (schema default)", LOOSE_QUERY),
                          ("dataset: realtime", REALTIME_QUERY),
                          ("dataset: combined", COMBINED_QUERY)]:
        print(f"--- {label} ---")
        try:
            body = _run(query, args.mint, since_iso, until_iso, api_key)
        except Exception as e:  # noqa: BLE001 -- diagnostic script, show everything
            print(f"  request failed: {e}")
            continue
        if body.get("errors"):
            print(f"  GraphQL errors: {json.dumps(body['errors'], indent=2)}")
            continue
        trades = body.get("data", {}).get("Solana", {}).get("DEXTradeByTokens")
        if trades is None:
            print(f"  unexpected response shape: {json.dumps(body, indent=2)[:2000]}")
            continue
        print(f"  {len(trades)} trades")
        if trades:
            print(f"  first: {json.dumps(trades[0], indent=2)}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
