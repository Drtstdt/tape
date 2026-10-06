#!/usr/bin/env python3
"""D67 diagnostic (real evidence, not a guess) -- 8/300 mints discovered by
`discover_pumpfun_launches.py` failed `HeliusSource.historical()` with:

    Helius RPC error calling getTokenLargestAccounts: {'code': -32602,
    'message': 'Invalid param: not a Token mint'}

That error means the address `extract_mint()` (in discover_pumpfun_launches.py)
picked out as "mint" for these 8 `create_v2` events is NOT actually a valid
SPL token mint account, according to a real Solana RPC node -- i.e. either
Bitquery's `AccountNames` decode put "mint" at the wrong Accounts index for
some instruction variant, or these particular `create_v2` events have a
genuinely different account layout (D60 already found pump.fun added
non-SOL quote-currency support, which could plausibly add/reorder accounts).

This script re-queries Bitquery for the EXACT signature of each failing
event (looked up from the cache's own recorded "sig" field, D63/D66) and
prints EVERY (AccountName, Address, IsWritable) triple Bitquery decoded for
that instruction -- not just whichever one extract_mint() picked -- so we
can see with real data what "mint" actually pointed to, and whether the
real base-token mint is sitting under some other name.

    python scripts/diag_failed_mint_accounts.py --data data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.env import load_project_dotenv
from tape.sources.bitquery import PUMPFUN_PROGRAM

ENDPOINT = "https://streaming.bitquery.io/graphql"

# The 8 mints that failed in the user's real backfill_discovered_launches.py
# run (2026-09-22) with "not a Token mint" from Helius's getTokenLargestAccounts.
FAILED_MINTS = [
    "WmAwsvpXMY8BgvysB92bRwzMdZe3oS5vZWRkZLvNpEm",
    "4CjNgbK8zCLUnzNyfsWR2mmiS4AR8T9zuuAMDenV5Chw",
    "2dKKbAeLvPGk6b83rC8g4Dj3Ktjx7Ko962Q3nKVEpump",
    "CxSNd9om8QTAKHwvC9HnpZM4rjiG9NKQyAqWhZRupump",
    "7HVDrb8gpEVp8XVkMaiHi8cSHAvzacpj95RmzMPtpump",
    "3wxg7Ub4DPMZAx7tbu6q1B9nW9aB7YjJqPEpTiXzpump",
    "HS1Lqw1XH8H67WGMtjKF1ffdz3JaFd2EsCfbv1h8pump",
    "F55b65CNfCJbPQ1mN9URiGcjf5ybhEV1jTcfgtbHpump",
]

BY_SIG_QUERY = """
query BySig($sig: String!, $program: String!) {
  Solana(dataset: realtime) {
    Instructions(
      limit: {count: 10}
      where: {
        Transaction: { Signature: {is: $sig} }
        Instruction: { Program: { Address: {is: $program} } }
      }
    ) {
      Block { Time }
      Instruction {
        Accounts { Address IsWritable }
        Program { Method Name AccountNames }
      }
    }
  }
}
"""


def _post(query: str, variables: dict, api_key: str) -> dict:
    resp = httpx.post(
        ENDPOINT,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:2000]!r}")
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"Bitquery returned errors: {body['errors']}")
    return body["data"]


def load_sigs(cache_path: Path, mints: List[str]) -> dict:
    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    out = {}
    for m in mints:
        rec = cache.get(m)
        if rec is None:
            print(f"  {m}: NOT in cache at all (unexpected)", file=sys.stderr)
            continue
        sig = rec.get("sig")
        if sig is None:
            print(f"  {m}: cache entry has no 'sig' field: {rec}", file=sys.stderr)
            continue
        out[m] = sig
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    cache_path = Path(args.data) / "real_creation_times.json"
    print(f"Looking up cached sig for {len(FAILED_MINTS)} failing mint(s) in {cache_path} ...")
    sigs = load_sigs(cache_path, FAILED_MINTS)
    print(f"{len(sigs)}/{len(FAILED_MINTS)} had a recorded sig.\n")

    for mint, sig in sigs.items():
        print("=" * 78)
        print(f"mint (as extracted): {mint}")
        print(f"sig: {sig}")
        try:
            data = _post(BY_SIG_QUERY, {"sig": sig, "program": PUMPFUN_PROGRAM}, api_key)
        except RuntimeError as e:
            print(f"  QUERY FAILED: {e}")
            continue
        rows = data["Solana"]["Instructions"]
        if not rows:
            print("  NO instruction rows found for this signature+program (unexpected -- "
                  "the sig came from a row that matched this exact filter originally).")
            continue
        for i, row in enumerate(rows):
            instr = row["Instruction"]
            program = instr["Program"]
            accounts = instr["Accounts"]
            names = program["AccountNames"]
            print(f"  -- instruction #{i}: Method={program.get('Method')!r} "
                  f"Name={program.get('Name')!r}, {len(accounts)} account(s), "
                  f"{len(names)} name(s)")
            n = max(len(accounts), len(names))
            for j in range(n):
                name = names[j] if j < len(names) else "<MISSING NAME>"
                addr = accounts[j]["Address"] if j < len(accounts) else "<MISSING ACCOUNT>"
                writable = accounts[j].get("IsWritable") if j < len(accounts) else None
                marker = "  <-- extract_mint() picked THIS one" if addr == mint else ""
                print(f"    [{j:2d}] {name!r:30s} = {addr}  (writable={writable}){marker}")
        print()

    print("=" * 78)
    print("Compare the account printed at whatever index literally named 'mint' "
          "(marked above) against the OTHER accounts in the same instruction -- "
          "if pump.fun's create_v2 has a genuinely different layout for some "
          "variant (e.g. a non-SOL quote currency, D60), the REAL base-token "
          "mint may be sitting under a different name here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
