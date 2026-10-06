"""D22 (2026-09-21, docs/DECISIONS.md): a THIRD verification path, alongside
Bitquery. Bitquery's `dataset: realtime` cannot reach v3's existing tapes
(D21 -- ~9-12h retention, and `archive`/`combined` are both blocked on this
account). Rather than pay for `archive` sight unseen, this uses Google's
public Solana BigQuery dataset (`solana-data-sandbox.crypto_solana_mainnet_us
.Transactions`, confirmed live/current on 2026-09-21 -- NOT the 30x-stale
dataset some 2025 reports described; verify freshness again before trusting
this on a later date) plus a hand-verified real transaction, WITHOUT writing
a pump.fun/PumpSwap instruction decoder at all:

  * The trader's OWN token-balance delta for the queried mint (`pre_token_
    balances` -> `post_token_balances`, matched by `owner` == the tx signer)
    gives side (increase=buy, decrease=sell) and token amount directly.
  * The trader's OWN native-SOL delta (`balance_changes`, matched by
    `account` == the tx signer's pubkey) gives the SOL amount, net of the
    network fee (NOT net of any AMM trading fee -- expect small discrepancies
    against v3's numbers from that, not a sign of a real bug).

This is DEX-agnostic by construction -- it reads ledger-level balance
changes, not program-specific instruction data, so it works identically
whether the trade happened on the pump.fun bonding curve, PumpSwap, Raydium,
or anything else that moves an SPL token and SOL in the same transaction. It
is also, for exactly that reason, cruder than a real decoder: a transaction
that moves the mint for a non-trade reason (a transfer, a migration) would be
miscounted as a trade. Not handled here -- flag as a discrepancy against v3
if window totals come out systematically high, don't assume it's Bitquery
being wrong.

Usage: run the BigQuery query below in the console (or `bq query`), export
the result as JSON (Zapisz wyniki / Save results -> JSON), and point this at
the downloaded file:

    python -m tape.scripts.parse_bigquery_export \\
        --json path/to/export.json \\
        --mint <mint> --tape ../v3/data_deep/<date>/<mint>.jsonl

The query (fill in $MINT, $SINCE, $UNTIL):

    SELECT
      block_timestamp,
      signature,
      fee,
      ARRAY(SELECT AS STRUCT pubkey, signer FROM UNNEST(accounts)) AS accounts,
      balance_changes,
      pre_token_balances,
      post_token_balances
    FROM `solana-data-sandbox.crypto_solana_mainnet_us.Transactions`
    WHERE block_timestamp BETWEEN TIMESTAMP('$SINCE') AND TIMESTAMP('$UNTIL')
      AND status = 'Success'
      AND EXISTS (
        SELECT 1 FROM UNNEST(pre_token_balances) AS ptb
        WHERE ptb.mint = '$MINT'
      )
    ORDER BY block_timestamp
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# Reuse the exact same tape-loading, windowing and diff machinery `probe.py`
# already has and already tests -- this script only needs a different
# *source* of swaps, not a different comparison.
from .probe import (
    load_tape, bucket_tape_windows, bucket_bitquery_windows, diff_windows,
    summarize, ProbeSwap, WINDOW_MS,
)

SOL_MINT = "So11111111111111111111111111111111111111112"


def _parse_bq_timestamp_ms(raw) -> Optional[int]:
    """BigQuery's console JSON export writes `block_timestamp` as
    `"YYYY-MM-DD HH:MM:SS UTC"` OR `"YYYY-MM-DD HH:MM:SS.ffffff UTC"` --
    fractional seconds are DROPPED ENTIRELY when they're exactly `.000000`,
    not padded/kept, which broke an earlier version of this function that
    assumed a `.` would always be there to split on (18,160/18,160 rows
    silently dropped by that bug -- see D22 follow-up, docs/DECISIONS.md).
    Handles both forms explicitly rather than string-hacking around the
    difference again.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith(" UTC"):
        s = s[: -len(" UTC")]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            continue
    return None


def load_bigquery_export(path: str | Path) -> List[dict]:
    """Accepts either a single JSON array (BigQuery's usual "Save results ->
    JSON" output) or newline-delimited JSON (one row per line) -- the export
    format has varied by console version, so try both rather than guess.
    """
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _signer_pubkey(row: dict) -> Optional[str]:
    for acc in row.get("accounts") or []:
        signer = acc.get("signer")
        if signer is True or signer == "true" or signer == "1":
            return acc.get("pubkey")
    return None


def _native_sol_delta(row: dict, pubkey: str) -> Optional[float]:
    for bc in row.get("balance_changes") or []:
        if bc.get("account") == pubkey:
            try:
                before, after = float(bc["before"]), float(bc["after"])
            except (KeyError, TypeError, ValueError):
                return None
            return (after - before) / 1e9  # lamports -> SOL
    return None


def _token_delta(row: dict, pubkey: str, mint: str) -> Optional[float]:
    """Token balance delta for the account OWNED BY `pubkey` holding `mint`,
    comparing pre_token_balances -> post_token_balances by `account_index`
    (the stable join key -- `owner`/`mint` alone could collide if a wallet
    somehow holds two accounts for the same mint in one tx, vanishingly
    unlikely but free to guard against).

    D22 FOLLOW-UP (confirmed against real data, docs/DECISIONS.md): the SPL
    token account for a given (owner, mint) does not always exist on BOTH
    sides of the transaction:

    * SELL of the ENTIRE balance -> the account gets CLOSED (to reclaim its
      rent), so it is absent from `post_token_balances` -- either omitted
      entirely, or left as an empty `{}` placeholder with no `account_index`
      at all (export-side inconsistency, cause not determined; handled
      either way by the `if "account_index" in b` filters below).
    * FIRST-EVER buy of a mint -> the associated token account is CREATED in
      this transaction, so it is absent from `pre_token_balances`.

    Both are real, common cases, not malformed input -- on Solana, closing a
    token account REQUIRES it to already be empty (a protocol invariant, not
    an inference), and a freshly-created account starts at exactly zero. So
    "absent from pre" reads as pre-balance = 0, and "absent from post" reads
    as post-balance = 0. Treating either as unmeasurable (returning None)
    silently dropped every full-exit sell in this project's first real run:
    12,879/18,160 rows (71%) came back `no_token_delta`, and it systematically
    undercounted sell volume (vendor sell often 0.0 against a real tape sell)
    -- a full exit is exactly the pattern a "only look at what's present"
    check misses.
    """
    pre_by_idx = {b["account_index"]: b for b in (row.get("pre_token_balances") or []) if "account_index" in b}
    post_by_idx = {b["account_index"]: b for b in (row.get("post_token_balances") or []) if "account_index" in b}
    # Union of indices from both sides -- the match for our (owner, mint) can
    # live in either dict depending on which side of the transaction it's
    # missing from (see docstring).
    candidate_entries = list(pre_by_idx.values()) + list(post_by_idx.values())
    matched_idx = None
    for entry in candidate_entries:
        if entry.get("owner") == pubkey and entry.get("mint") == mint:
            matched_idx = entry["account_index"]
            break
    if matched_idx is None:
        return None
    pre = pre_by_idx.get(matched_idx)
    post = post_by_idx.get(matched_idx)
    reference = pre or post  # whichever side exists, for `decimals`
    try:
        decimals = int(reference.get("decimals", 0))
        pre_amt = float(pre["amount"]) / (10 ** decimals) if pre is not None else 0.0
        post_amt = float(post["amount"]) / (10 ** decimals) if post is not None else 0.0
    except (KeyError, TypeError, ValueError):
        return None
    return post_amt - pre_amt


def parse_bigquery_row(row: dict, mint: str) -> Optional[ProbeSwap]:
    """None (never a guess) if the signer can't be found, the mint's token
    balance didn't change for them (not actually a swap of this mint -- e.g.
    they were only the fee payer for someone else's trade in a multi-signer
    tx), or the SOL leg can't be read. A transaction that touches the mint
    without a signer-side balance change is exactly the kind of thing this
    balance-delta approach can misfire on (see module docstring) -- dropping
    it silently here is deliberate, not a bug to fix later.
    """
    ts_ms = _parse_bq_timestamp_ms(row.get("block_timestamp"))
    if ts_ms is None:
        return None
    signer = _signer_pubkey(row)
    if signer is None:
        return None
    token_delta = _token_delta(row, signer, mint)
    if token_delta is None or token_delta == 0:
        return None
    sol_delta = _native_sol_delta(row, signer)
    if sol_delta is None:
        return None
    side = "buy" if token_delta > 0 else "sell"
    # A buyer's SOL delta is negative (they paid); flip sign so sol_amount is
    # always a positive magnitude, matching ProbeSwap's other sources.
    sol_amount = abs(sol_delta)
    return ProbeSwap(ts_ms=ts_ms, side=side, sol_amount=sol_amount, price=None, raw=row)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", required=True, help="path to the BigQuery JSON export")
    ap.add_argument("--mint", required=True)
    ap.add_argument("--tape", required=True, help="path to a v3 data_deep/<date>/<mint>.jsonl file")
    ap.add_argument("--tolerance-pct", type=float, default=15.0)
    args = ap.parse_args(argv)

    rows = load_bigquery_export(args.json)
    print(f"Loaded {len(rows)} rows from {args.json}")
    if not rows:
        print("No rows -- wrong path, empty export, or the query matched nothing.", file=sys.stderr)
        return 2

    tape_rows = load_tape(args.tape)
    tape_windows = bucket_tape_windows(tape_rows)
    if not tape_windows:
        print("Every row in the tape had null buy/sell volume.", file=sys.stderr)
        return 2

    parsed = [parse_bigquery_row(r, args.mint) for r in rows]
    failed = sum(1 for p in parsed if p is None)
    swaps = [p for p in parsed if p is not None]
    print(f"Parsed {len(swaps)}/{len(rows)} rows into swaps ({failed} dropped -- "
          f"see module docstring for why that's expected, not an error).")
    if not swaps:
        print("Nothing parsed.", file=sys.stderr)
        return 1

    vendor_windows = bucket_bitquery_windows(swaps)  # same bucketing fn, source-agnostic
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
