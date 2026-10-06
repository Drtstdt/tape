#!/usr/bin/env python3
"""D63 follow-up: backfill real swap history for a CHRONOLOGICALLY-SPREAD
SAMPLE of the mints `scripts/discover_pumpfun_launches.py` found via
on-chain `create_v2` events (D60), using the already-verified `HeliusSource`
(D25/D31/D34/D42) -- the same adapter `scripts/backfill.py` uses, not
Bitquery: D40 already confirmed the purchased `archive` add-on does NOT
cover Solana `DEXTradeByTokens` either (the same "plan restriction" pattern
D62 just found for `Instructions`), so Helius, with its full archival reach
and no retention wall, is the only viable path for this.

WHY A SAMPLE, NOT ALL 9,926: most freshly-launched pump.fun tokens get
minimal trading and will never clear `store.mints()`'s `min_swaps=50`
floor regardless of backfill effort (same reasoning `backfill.py`'s
`--min-tape-bytes` skip already uses for tape files) -- paying for a full
Helius pull on every one of them would spend a lot of real API calls on
tokens that were always going to be excluded. Taking every Nth discovered
mint (by creation time, not by hand-picking "good-looking" ones -- picking
by outcome would be exactly the kind of look-ahead bias D18 already warned
against) keeps the CALENDAR spread D56 was missing while bounding cost.

WINDOW CHOSEN PER MINT: `[real_created_ts_ms, real_created_ts_ms +
--window-hours]` (default 6h, matching D55's `--max-observed-age-hours`
default -- "clearly still launch-day territory"), capped at `now` for
very recently discovered mints. This is an early-life window, not each
mint's full trading history -- deliberately, since `information_audit.py`
only ever looks at early-life dynamics (D53's whole design), and pulling
a token's entire lifetime would cost far more for no benefit to this gate.

Idempotent (same real-query check as `backfill.py`/`backfill_bitquery.py`,
D36): skips a mint already in the Store unless `--refetch`. Safe to
re-run repeatedly, raising `--limit` each time, exactly like `backfill.py`.

    python scripts/backfill_discovered_launches.py --data data --target-count 300 --limit 50
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.sources.helius import HeliusSource
from tape.store import Store

SOURCE_TAG = "bitquery_create_v2"  # matches discover_pumpfun_launches.py


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}min"
    return f"{minutes / 60:.1f}h"


def _existing_swap_count(store: Store, mint: str) -> int:
    """Same real-query idempotency check as backfill.py/backfill_bitquery.py
    (D36) -- never a filename or in-memory cache."""
    df = store.sql(f"SELECT count(*) AS n FROM swaps WHERE mint = '{mint}'")
    return int(df["n"][0])


def load_discovered_mints(cache_path: Path) -> List[Tuple[str, int]]:
    """(mint, real_created_ts_ms) pairs, `source == "bitquery_create_v2"`
    only (D63) -- the older pump.fun-frontend-API-sourced entries (D55)
    are a DIFFERENT population (already-in-Store survivor tokens being
    aged-verified, not new discoveries to backfill) and are deliberately
    excluded here, not merged in by accident."""
    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    out = []
    for mint, rec in cache.items():
        if rec.get("source") == SOURCE_TAG and rec.get("status") == "ok" \
                and rec.get("real_created_ts_ms") is not None:
            out.append((mint, int(rec["real_created_ts_ms"])))
    out.sort(key=lambda t: t[1])
    return out


def spread_sample(mints: List[Tuple[str, int]], target_count: int) -> List[Tuple[str, int]]:
    """Every Nth mint BY CREATION TIME (never by outcome -- D18), to keep
    calendar spread while bounding how many expensive Helius pulls this
    costs. Returns all of them if there are already <= target_count."""
    n = len(mints)
    if n <= target_count or target_count <= 0:
        return list(mints)
    stride = n / target_count
    indices = sorted({int(i * stride) for i in range(target_count)})
    return [mints[i] for i in indices]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--cache-file", default=None,
                     help="path to discover_pumpfun_launches.py's output "
                          "(default: <--data>/real_creation_times.json)")
    ap.add_argument("--target-count", type=int, default=300,
                     help="how many mints to sample, spread across the full "
                          "discovered calendar range (default 300)")
    ap.add_argument("--window-hours", type=float, default=6.0,
                     help="early-life window pulled per mint, starting at its "
                          "real creation time (default 6h, matches D55's "
                          "--max-observed-age-hours default)")
    ap.add_argument("--limit", type=int, default=20,
                     help="max NEW mints to actually fetch this run (kept low "
                          "by default -- raise once a small run looks right, "
                          "same convention as backfill.py)")
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    cache_path = Path(args.cache_file) if args.cache_file else Path(args.data) / "real_creation_times.json"
    if not cache_path.exists():
        print(f"{cache_path} does not exist -- run scripts/discover_pumpfun_launches.py first.",
              file=sys.stderr)
        return 2

    all_discovered = load_discovered_mints(cache_path)
    print(f"{len(all_discovered)} mint(s) in the cache with source={SOURCE_TAG!r}")
    if not all_discovered:
        print("Nothing to backfill.", file=sys.stderr)
        return 1

    sample = spread_sample(all_discovered, args.target_count)
    print(f"sampled {len(sample)} mint(s), spread across "
          f"[{sample[0][1]}, {sample[-1][1]}] "
          f"({(sample[-1][1] - sample[0][1]) / 3_600_000:.2f}h span)")

    try:
        source = HeliusSource()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    store = Store(args.data)

    now_ms = int(time.time() * 1000)
    run_start = time.time()
    total_swaps = 0
    succeeded, failed, too_thin, skipped = [], [], [], []
    attempt_elapsed: list = []
    fetched_this_run = 0

    for i, (mint, created_ms) in enumerate(sample, start=1):
        if fetched_this_run >= args.limit:
            print(f"\nReached --limit {args.limit} new fetches this run -- stopping "
                  f"(re-run to continue with the rest; already-fetched mints are skipped).")
            break
        if not args.refetch:
            existing = _existing_swap_count(store, mint)
            if existing > 0:
                print(f"[{i}/{len(sample)}] {mint}: already have {existing} swaps, skipping")
                skipped.append(mint)
                continue

        since_ms = created_ms
        until_ms = min(now_ms, created_ms + int(args.window_hours * 3_600_000))
        if until_ms <= since_ms:
            print(f"[{i}/{len(sample)}] {mint}: window is empty (created after 'now'?), skipping")
            failed.append((mint, "empty window"))
            continue

        print(f"[{i}/{len(sample)}] {mint}: window {(until_ms - since_ms) / 1000:.0f}s "
              f"from creation, fetching from Helius ...")
        t0 = time.time()
        try:
            swaps = list(source.historical(mint, since_ms, until_ms))
        except RuntimeError as e:
            elapsed = time.time() - t0
            print(f"  FAILED after {elapsed:.0f}s: {e}")
            failed.append((mint, str(e)))
            attempt_elapsed.append(elapsed)
            fetched_this_run += 1
            continue
        elapsed = time.time() - t0
        attempt_elapsed.append(elapsed)
        fetched_this_run += 1
        if len(swaps) < 50:
            print(f"  only {len(swaps)} swaps in {elapsed:.0f}s -- below the min_swaps=50 "
                  f"floor, won't count toward the audit's universe, writing anyway (cheap)")
            too_thin.append(mint)
        written = store.write_swaps(swaps)
        total_swaps += len(swaps)
        succeeded.append(mint)
        print(f"  {len(swaps)} swaps written to {len(written)} file(s) in {elapsed:.0f}s")
        if attempt_elapsed:
            avg = sum(attempt_elapsed) / len(attempt_elapsed)
            remaining = max(min(args.limit - fetched_this_run, len(sample) - i), 0)
            print(f"  pace: avg {avg:.0f}s/mint -> ~{remaining} left this run, "
                  f"ETA ~{_fmt_duration(avg * remaining)}")

    print("\n" + "=" * 70)
    print(f"Done in {_fmt_duration(time.time() - run_start)} wall time. "
          f"{len(succeeded)} succeeded, {total_swaps} swaps written total.")
    if skipped:
        print(f"  {len(skipped)} already in store, skipped")
    if too_thin:
        print(f"  {len(too_thin)} had < 50 swaps (won't count toward the audit's universe)")
    if failed:
        print(f"  {len(failed)} FAILED:")
        for mint, reason in failed[:10]:
            print(f"    {mint}: {reason}")
        if len(failed) > 10:
            print(f"    ... and {len(failed) - 10} more")
    remaining_unsampled = len(sample) - len(succeeded) - len(skipped) - len(failed)
    print(f"\n{remaining_unsampled} mint(s) from this sample not yet attempted this run "
          f"(--limit {args.limit}) -- re-run with the same flags to continue; already-done "
          f"mints are skipped automatically.")
    print(f"\nNext: python scripts/information_audit.py --data {args.data} "
          f"--real-creation-file {cache_path}")
    return 0 if (succeeded or skipped) else 1


if __name__ == "__main__":
    raise SystemExit(main())
