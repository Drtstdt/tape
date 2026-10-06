#!/usr/bin/env python3
"""PROOF OF CONCEPT, not production code -- same status as
poc_idl_classify.py / bitquery_archive_smoketest.py.

Question this answers, live, on the Helius plan actually paid for (D41,
D42, docs/DECISIONS.md: upgraded to Developer, $49/mo -- confirmed via
Helius's own pricing page to include full archival history, unlike the
Free tier this project ran on before): does Helius's `getTransactionsForAddress`
(gTFA) method actually reach back further than Bitquery `dataset: realtime`'s
~9h wall (D21), and can it be pointed at a PROGRAM address (PUMPFUN_PROGRAM /
PUMPSWAP_PROGRAM) rather than one wallet at a time?

Why this matters right now: `information_audit.py`'s 2026-09-22 run only had
a ~3.3 HOUR chronological window of token-launch history to split into
screen/validation/final_test (94 tokens total) -- that ceiling traces back to
`backfill_bitquery.py` using `dataset: realtime` for BOTH discovery and swap
history (D43: the free BigQuery-discovery half of the alternative pipeline is
quota-blocked; Bitquery's `archive` add-on was confirmed NOT to cover Solana
`DEXTradeByTokens` at all, D40). Helius Developer has no such wall per its own
docs, but this project has never called gTFA before (D41 flagged it
"well-documented by Helius, not yet verified live here") and has never used
Helius for DISCOVERY (only for decoding swaps once a mint is already known)
-- discovering many mints AND their history from one program-level call,
if it works, removes the discovery bottleneck entirely, not just the
history-depth one.

This script does NOT attempt classification (buy/sell/create), does NOT
write anything to the Store, and does not build a new backfill pipeline --
it only answers "does the reach and the request shape work as documented",
the same narrow, live-first question `bitquery_archive_smoketest.py` asked
of `dataset: archive` before anyone trusted it.

    python scripts/poc_gtfa_reach.py --hours-back 6 12 24 48 72 168
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.env import load_project_dotenv
from tape.sources.bitquery import PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM
from tape.sources.helius import _rpc_call
import os


def probe_reach(address: str, target_ts: int, api_key: str) -> dict:
    """Ask for the OLDEST transaction AT OR AFTER `target_ts` (unix seconds).

    If real reach extends to `target_ts`, the returned transaction's
    `blockTime` should land close to it (a program this busy has transactions
    every few seconds). If reach does NOT extend that far, this either errors
    outright, comes back empty, or (worth checking for, not assuming) returns
    something with a blockTime far LATER than requested -- i.e. the earliest
    thing the API is willing to hand back is much more recent than asked.
    Mirrors the bisection D21 already used for Bitquery's realtime wall.
    """
    params = [
        address,
        {
            "sortOrder": "asc",
            "limit": 1,
            "transactionDetails": "signatures",
            "filters": {"blockTime": {"gte": target_ts}},
        },
    ]
    return _rpc_call("getTransactionsForAddress", params, api_key)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours-back", type=float, nargs="+",
                     default=[1, 3, 6, 9, 12, 24, 48, 72, 168],
                     help="offsets to bisect reach at (168h = 1 week)")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--program", choices=["pumpfun", "pumpswap"], default="pumpfun")
    args = ap.parse_args()

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("HELIUS_API_KEY")
    if not api_key:
        print("HELIUS_API_KEY not set.", file=sys.stderr)
        return 2

    address = PUMPFUN_PROGRAM if args.program == "pumpfun" else PUMPSWAP_PROGRAM
    now_ts = int(time.time())

    print(f"Probing gTFA reach against {args.program} ({address})")
    print(f"{'hours_back':>10s} {'target_ts':>12s} {'got_blockTime':>14s} "
          f"{'delta_s':>10s} {'signature':>12s}")
    print("-" * 70)

    first_failure = None
    for hb in sorted(args.hours_back):
        target_ts = now_ts - int(hb * 3600)
        try:
            body = probe_reach(address, target_ts, api_key)
        except Exception as e:
            print(f"{hb:10.1f} {target_ts:12d}  ERROR: {e}")
            if first_failure is None:
                first_failure = hb
            continue
        result = body.get("result")
        if isinstance(result, list):   # response shape not yet verified -- handle both
            txs = result
            result = {}
        else:
            result = result or {}
            txs = result.get("transactions") or result.get("data") or []
        if not txs:
            print(f"{hb:10.1f} {target_ts:12d} {'(empty)':>14s} {'':>10s} {'':>12s}")
            print(f"    raw result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")
            continue
        tx = txs[0]
        got_bt = tx.get("blockTime")
        sig = tx.get("signature") or tx.get("transactionSignature") or "?"
        delta = (got_bt - target_ts) if isinstance(got_bt, (int, float)) else None
        print(f"{hb:10.1f} {target_ts:12d} {str(got_bt):>14s} "
              f"{(f'{delta:+d}' if delta is not None else '?'):>10s} {sig[:10]:>12s}...")

    print("\nInterpretation:")
    print("  delta_s close to 0  -> reach genuinely extends to that offset")
    print("  delta_s large/positive, or ERROR -> reach does NOT extend that far")
    print("  first ERROR/empty offset marks the real wall, same as D21's bisection")
    if first_failure is not None:
        print(f"\n  first failing/empty offset tested: {first_failure}h")
    print("\nRaw response shape was UNVERIFIED before this run (D41) -- if the field")
    print("names above (result.transactions / .data, blockTime, signature) are wrong,")
    print("the ERROR/empty-result lines will show the ACTUAL response so this can be")
    print("corrected against real evidence rather than guessed a second time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
