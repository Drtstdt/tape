"""D38 (2026-09-22, docs/DECISIONS.md) -- universe DISCOVERY, the actual
thing standing between "50 tokens" and "however many actually traded":
`scripts/backfill.py` only ever pulls swaps for mints this project ALREADY
has a v3 tape filename for. Helius's per-mint `historical()` (D31/D34,
verified) needs a mint to start from -- it cannot hand you a list of mints
it doesn't already know about. Nothing built so far closes that gap.

This does NOT pull swap data. It answers one question, as cheaply as
possible: "which transactions touched the pump.fun or PumpSwap program in
this date range?" -- a list of (signature, block_timestamp) pairs, nothing
else. `tape/sources/helius.py::HeliusSource.from_signatures()` (D38) then
decodes each signature for WHATEVER mint(s) it touches, discovering the
universe organically from real trade activity instead of a pre-existing
list.

WHY THIS QUERY, NOT THE ONE `parse_bigquery_export.py` (D22/D23) ALREADY
HAS: that query already works, but it needs a `$MINT` to filter on --
useless for discovery, and worse, it SELECTs `balance_changes`,
`pre_token_balances`, `post_token_balances` -- exactly the nested columns
D25 flagged as the ones big enough to burn terabytes on an unscoped scan
("one Solana block's full transaction record ... can be large enough that
an unscoped query burns terabytes"). BigQuery bills for every referenced
COLUMN across every ROW in the scanned (date-partitioned) range, regardless
of what the WHERE clause filters out -- `LIMIT` does not reduce the bill,
and neither does a selective filter on a DIFFERENT column. So this query
is deliberately narrow: it selects only `signature` and `block_timestamp`,
and filters on `accounts` -- a column D22's own already-verified query
already reads for signer detection, not a new schema risk -- instead of the
heavy balance-delta columns. Whatever this finds gets its actual balance
data from a `getTransaction` call (already paid for, already verified,
D31/D34), not from a second BigQuery read.

PROGRAM IDS, verified 2026-09-22 against pump.fun's own public docs
repo (`pump-fun/pump-public-docs`, `PUMP_PROGRAM_README.md` /
`PUMP_SWAP_README.md`) and cross-checked against Solscan -- not guessed,
per this project's standing discipline:
  - pump.fun bonding curve: 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
  - PumpSwap AMM:            pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA
These are used ONLY for "does this tx's account list contain this pubkey"
membership -- never for decoding instruction accounts/data, which would
need IDL-verified account ordering this project has not independently
confirmed (flagged, not blocking: membership-only usage doesn't need it).

D25's actual, hard lesson, restated so it cannot be skipped a second time:
**never run a query against this dataset without `--dry_run` first and
`--maximum_bytes_billed` on the real run.** The free 1 TiB/month quota was
already spent once (D25) and this dataset has a documented history of
five-figure surprise bills precisely because of the billing model above.
This script does not run BigQuery itself (no GCP credentials or network
path to bigquery.googleapis.com are available from where this project's
automation runs, docs/DECISIONS.md D37) -- it PRINTS the exact `bq`
commands, dry-run first, for you to run yourself, and loads whatever JSON
you export back in.

Usage:
    python -m tape.scripts.bigquery_discover --since 2026-06-01 --until 2026-09-01 --print-query
    # run the printed `bq query --dry_run ...` first, read the bytes estimate,
    # decide if it's worth it, THEN run the real one with --maximum_bytes_billed set,
    # export the result as JSON, then:
    python -m tape.scripts.bigquery_discover --load path/to/export.json

FRESHNESS WARNING: this table (`solana-data-sandbox.crypto_solana_mainnet
_us.Transactions`) was confirmed live/current for a September 2026 mint as
of 2026-09-21 (D22's own verification run), but public reports elsewhere
describe this exact dataset lagging or stalling for extended periods at
other points in its history. Confirmed-once is not confirmed-forever --
before trusting a big pull, dry-run a query for the most RECENT day first
and sanity-check it returns anything at all before spending quota on a
wide date range that might silently be scanning nothing useful.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from .parse_bigquery_export import load_bigquery_export, _parse_bq_timestamp_ms

TABLE = "solana-data-sandbox.crypto_solana_mainnet_us.Transactions"

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def build_query(since: str, until: str) -> str:
    return f"""
SELECT
  signature,
  block_timestamp
FROM `{TABLE}`
WHERE block_timestamp BETWEEN TIMESTAMP('{since}') AND TIMESTAMP('{until}')
  AND status = 'Success'
  AND EXISTS (
    SELECT 1 FROM UNNEST(accounts) AS a
    WHERE a.pubkey IN ('{PUMPFUN_PROGRAM}', '{PUMPSWAP_PROGRAM}')
  )
ORDER BY block_timestamp
""".strip()


def load_discovered_signatures(path: str) -> List[dict]:
    """[{"sig": ..., "ts_ms": ...}, ...], oldest first, deduped on signature.
    Reuses `parse_bigquery_export.py`'s loader (handles both the JSON-array
    and NDJSON console-export shapes) and its timestamp parser (handles the
    "UTC"-suffixed, sometimes-no-fractional-seconds format) -- both already
    verified against this exact table's real export format (D22)."""
    rows = load_bigquery_export(path)
    out = []
    seen = set()
    for row in rows:
        sig = row.get("signature")
        if not sig or sig in seen:
            continue
        seen.add(sig)
        out.append({"sig": sig, "ts_ms": _parse_bq_timestamp_ms(row.get("block_timestamp"))})
    out.sort(key=lambda r: (r["ts_ms"] is None, r["ts_ms"]))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", help="YYYY-MM-DD, start of the discovery window (inclusive)")
    ap.add_argument("--until", help="YYYY-MM-DD, end of the discovery window (exclusive)")
    ap.add_argument("--print-query", action="store_true",
                     help="print the SQL plus the dry-run/real bq commands, then exit")
    ap.add_argument("--load", help="path to a JSON export of the query's results, "
                                   "to parse into a flat signature list")
    ap.add_argument("--out", default="data/discovered_signatures.txt",
                     help="where to write the flat signature list (one per line, "
                          "oldest first) for scripts/backfill_wide.py to consume")
    ap.add_argument("--sql-out", default="discovery_query.sql",
                     help="where to write the generated SQL file (default: "
                          "discovery_query.sql in the current directory)")
    args = ap.parse_args(argv)

    if args.print_query:
        if not (args.since and args.until):
            print("--since and --until are required with --print-query", file=sys.stderr)
            return 2
        sql = build_query(args.since, args.until)
        # Written to a FILE and fed via stdin redirection, not inlined into the
        # `bq` command as a quoted string -- the query contains both backticks
        # (around the table name) and single quotes (around string literals),
        # either of which breaks naive shell quoting (a backtick inside
        # double quotes triggers command substitution; single quotes inside
        # single quotes just end the string early). A file sidesteps both.
        with open(args.sql_out, "w") as f:
            f.write(sql + "\n")
        print(f"SQL written to {args.sql_out}.\n")
        print("-- Run the DRY RUN first. It costs nothing and reports bytes scanned\n"
              "-- WITHOUT running the query -- decide whether it's worth it before\n"
              "-- spending any quota (D25: this table's per-byte billing model\n"
              "-- means a wide, careless date range can be very expensive).\n")
        print(f"bq query --use_legacy_sql=false --dry_run < {args.sql_out}\n")
        print("-- If the estimate looks reasonable, run for real WITH a hard cap --\n"
              "-- never again without --maximum_bytes_billed (D25's own words):\n")
        print(f"bq query --use_legacy_sql=false --maximum_bytes_billed=100000000000 \\\n"
              f"    --format=json < {args.sql_out} > discovery_export.json\n")
        print("-- Or paste the SQL from that file into the BigQuery console, run it,\n"
              "-- and use 'Save results -> JSON' -- either export format is accepted\n"
              "-- by --load.\n")
        print("--- SQL (also written to the file above) ---")
        print(sql)
        return 0

    if args.load:
        rows = load_discovered_signatures(args.load)
        if not rows:
            print(f"No signatures parsed from {args.load} -- wrong path, empty export, "
                  f"or the query matched nothing in this window.", file=sys.stderr)
            return 2
        with open(args.out, "w") as f:
            for r in rows:
                f.write(r["sig"] + "\n")
        first_ts = next((r["ts_ms"] for r in rows if r["ts_ms"] is not None), None)
        last_ts = next((r["ts_ms"] for r in reversed(rows) if r["ts_ms"] is not None), None)
        print(f"{len(rows)} unique signature(s) written to {args.out}"
              + (f", spanning {first_ts} .. {last_ts} ms" if first_ts and last_ts else ""))
        print(f"\nNext: python scripts/backfill_wide.py --signatures {args.out}")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
