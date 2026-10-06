"""helius_verify -- D3's mandatory check before `tape/sources/helius.py`
(D31, docs/DECISIONS.md) is trusted for a real backfill: run it against a
mint you already have a v3 tape for, and diff against ground truth, the
same way `probe.py` verified Bitquery (D19-D23) and
`parse_bigquery_export.py` verified the BigQuery balance-delta approach
(D22/D23) before either was trusted.

Reuses `probe.py`'s comparison machinery wholesale (`bucket_tape_windows`,
`bucket_bitquery_windows`, `diff_windows`, `summarize`, `ProbeSwap`) rather
than reimplementing it -- it is source-agnostic by construction (buckets
anything with `ts_ms`/`side`/`sol_amount` into 10-second windows and diffs
against the tape), which is exactly why `parse_bigquery_export.py` reused
it for BigQuery instead of writing a second comparison. `HeliusSource`'s
`CanonicalSwap.quote_amount` IS the SOL amount here (its `quote_mint` is
always `SOL_MINT` by construction, see `tape/sources/helius.py`), so
wrapping each into a `ProbeSwap` is a direct field rename, not a
conversion.

WHAT THIS CANNOT CHECK (same limitation `probe.py`'s own docstring states):
v3's tapes are 10-second POOL-STATE snapshots with aggregated buy/sell
volume, not a per-swap ground truth -- so this checks per-window volume
agreement (an INDIRECT side-convention check: swapped buy/sell would show
up as anti-correlated series, not just "some mismatches") and per-window
trade count (catches Helius silently missing an entire venue), not a
swap-by-swap diff.

WRITES TO THE STORE BY DEFAULT (`--data`, default "data"). Found the hard
way, 2026-09-21: a verify run against a busy pool paid the full cost of an
~18k-signature pull (tens of minutes, real API calls) purely to compute a
correlation number and then threw the fetched swaps away -- and
`scripts/backfill.py`'s first run picked up the SAME mint and paid for the
identical pull a second time. A mint this thoroughly verified is exactly
the kind of real, checked data the store should have anyway, so this now
persists what it fetches (`--no-write` to skip, e.g. for a quick sanity
check on a mint you don't want in the corpus yet).

    python -m tape.scripts.helius_verify \\
        --mint 3tUn8dGmDceJ9pCwFLUVo2jkD6FLWaZ78YEpAa3bpump \\
        --tape ../v3/data_deep/2026-09-20/3tUn8dGmDceJ9pCwFLUVo2jkD6FLWaZ78YEpAa3bpump.jsonl

Needs `HELIUS_API_KEY` in `tape/.env` or the shell environment.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from ..env import load_project_dotenv
from ..sources.helius import HeliusSource
from ..store import Store
from .probe import (
    ProbeSwap, bucket_bitquery_windows, bucket_tape_windows, diff_windows,
    load_tape, summarize,
)


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Plain Pearson correlation, no numpy dependency for this one number --
    None (not 0.0, not a crash) if there's too little data or no variance
    to compute one meaningfully. Reported the same way D23 reported
    `corr(tape_buy, vendor_buy)` for the BigQuery balance-delta check --
    the same diagnostic, now for this adapter."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mint", required=True)
    ap.add_argument("--tape", required=True, help="path to a v3 data_deep/<date>/<mint>.jsonl file")
    ap.add_argument("--tolerance-pct", type=float, default=15.0)
    ap.add_argument("--minutes", type=float, default=None,
                     help="only test the FIRST N minutes of the tape's window, not the "
                          "whole thing -- a busy pool can have 10,000+ signatures over a "
                          "few hours, and this adapter is one getTransaction call per "
                          "signature (no batching), so the full window can take tens of "
                          "minutes. Use this for a fast first pass; drop it once that "
                          "passes to test the full window.")
    ap.add_argument("--data", default="data", help="Store root to write verified swaps "
                                                     "into (default: data)")
    ap.add_argument("--no-write", action="store_true",
                     help="don't persist fetched swaps to the store -- for a quick "
                          "sanity check on a mint you don't want in the corpus")
    args = ap.parse_args(argv)

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
    if args.minutes is not None:
        until_ms = min(until_ms, since_ms + int(args.minutes * 60_000))
    print(f"Window from tape: {since_ms} .. {until_ms} ms "
          f"({(until_ms - since_ms) / 1000:.0f}s"
          + (f", clipped to first {args.minutes} min via --minutes" if args.minutes is not None else "")
          + ")\n")

    try:
        source = HeliusSource()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"Fetching Helius swaps for {args.mint} (this resolves the pool owner, "
          f"pulls signatures, then calls getTransaction per signature -- can take "
          f"a while for a busy pool; the adapter prints its own progress to "
          f"stderr as it goes, so this should never look stuck)...")
    try:
        swaps = list(source.historical(args.mint, since_ms, until_ms))
    except RuntimeError as e:
        print(f"historical() raised: {e}", file=sys.stderr)
        return 1
    print(f"Got {len(swaps)} swaps from Helius.\n")
    if not swaps:
        print("Nothing parsed -- either this pool had no activity in this window, "
              "or something upstream is wrong. Do not conclude the adapter is "
              "broken from this alone; try a narrower, more recent window on a "
              "mint you know traded heavily first.", file=sys.stderr)
        return 1

    if args.no_write:
        print("(--no-write: not persisting these swaps to the store)\n")
    else:
        # This pull is exactly as expensive whether or not the swaps get kept
        # -- found live, 2026-09-21: a verify run's ~18k-signature pull was
        # thrown away, then scripts/backfill.py paid for the identical pull
        # again for the same mint the next run. A verified mint's swaps are
        # real, checked data; there is no reason not to keep them.
        store = Store(args.data)
        written = store.write_swaps(swaps)
        print(f"Wrote {len(swaps)} swaps to {len(written)} file(s) under "
              f"{args.data}/swaps/ (--no-write to skip this).\n")

    print("First swap, verbatim, for manual inspection:")
    print(json.dumps({
        "ts_ms": swaps[0].ts_ms, "side": swaps[0].side, "base_amount": swaps[0].base_amount,
        "quote_amount": swaps[0].quote_amount, "price": swaps[0].price,
        "wallet": swaps[0].wallet, "venue": swaps[0].venue, "sig": swaps[0].sig,
    }, indent=2))
    print()

    probe_swaps = [ProbeSwap(ts_ms=s.ts_ms, side=s.side, sol_amount=s.quote_amount,
                              price=s.price, raw=None) for s in swaps]
    vendor_windows = bucket_bitquery_windows(probe_swaps)
    diff_rows = diff_windows(tape_windows, vendor_windows, args.tolerance_pct)
    print(summarize(diff_rows))
    for r in diff_rows:
        if r["mismatch"]:
            print(f"  MISMATCH @ {r['window_start_ms']}: "
                  f"tape buy/sell={r['tape_buy_sol']}/{r['tape_sell_sol']} "
                  f"vendor buy/sell={r['vendor_buy_sol']}/{r['vendor_sell_sol']}")

    both = [r for r in diff_rows if r["tape_buy_sol"] is not None and r["vendor_buy_sol"] is not None]
    if both:
        corr_buy = _pearson([r["tape_buy_sol"] for r in both], [r["vendor_buy_sol"] for r in both])
        corr_sell = _pearson([r["tape_sell_sol"] for r in both], [r["vendor_sell_sol"] for r in both])
        print(f"\ncorr(tape_buy, vendor_buy) = {corr_buy}")
        print(f"corr(tape_sell, vendor_sell) = {corr_sell}")
        print("(D23 found ~0.80/0.65 for the BigQuery balance-delta approach on the "
              "same kind of ledger data -- similar-magnitude positive correlation "
              "here is what 'the side convention is right' looks like. A NEGATIVE "
              "correlation on either would mean buy/sell are swapped.)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
