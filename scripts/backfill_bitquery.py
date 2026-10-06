#!/usr/bin/env python3
"""D39 (2026-09-22, docs/DECISIONS.md) -- the paid, wide backfill this
project's plan always intended (`docs/PLAN.md` §4, `docs/DATA.md` §1):
Bitquery's `archive` add-on ($100/mo, now purchased) gives a vendor-parsed,
one-schema feed across pump.fun/PumpSwap/every major Solana venue, so this
does NOT need Helius's slower per-signature reconstruction as its primary
path the way `scripts/backfill.py`/`backfill_wide.py` did while archive was
still declined (D25 onward).

DO NOT RUN THIS before `tape/scripts/bitquery_archive_smoketest.py` has
confirmed, live, (1) how far back `dataset: archive` actually reaches on
`DEXTradeByTokens`, and (2) that `discover()`'s program-id filter actually
returns real rows. Both are unverified assumptions until that script has
been run once and its findings logged (D39/D40, docs/DECISIONS.md) -- the
same discipline that caught D20 (silent-empty `DEXTrades` query) and D32
(the getSignaturesForAddress time-filter bug) before they cost real money
or real time.

Two phases, each resumable on its own:

  1. DISCOVER -- `BitquerySource.discover()` finds every mint that traded
     against the pump.fun/PumpSwap program ids in [--since, --until],
     writing each one to --mints-file AS FOUND (so an interrupted discovery
     run loses nothing -- same reasoning as `backfill_wide.py`'s signature
     checkpoint, D38).
  2. BACKFILL -- for each discovered mint (skipping ones the Store already
     has, same idempotent check `backfill.py` uses), pulls the full
     [--since, --until] window via `historical(dataset='archive')` and
     writes to the Store.

OPTIONAL FREE CORROBORATION (`--corroborate-every N`): every Nth backfilled
mint also gets pulled via the already-verified `HeliusSource` (D31/D34) and
diffed with `tape.sources.corroborate()` -- exactly the data-integrity
check `docs/PLAN.md` §2.3 always wanted Helius for, now actually run at
scale instead of the single hand-verified mint from D34. Off by default
(0) because Helius's free tier is real-rate-limited (a 429 after ~70
unthrottled calls, per `tape/sources/helius.py`) and this is meant to be
a light spot-check, not a second full backfill.

    python -m tape.scripts.bitquery_archive_smoketest --mint <known mint> --days-ago 1,7,30,90
    # ... confirm archive's real reach and discover()'s filter, log D40 ...
    python scripts/backfill_bitquery.py --since 2026-03-01 --until 2026-09-01 \\
        --mints-file data/bitquery_discovered_mints.txt --limit 200 --corroborate-every 20
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.sources import corroborate
from tape.sources.bitquery import (
    DATASET_ARCHIVE, DATASET_REALTIME, KNOWN_QUOTE_MINTS, VALID_DATASETS, BitquerySource,
)
from tape.sources.helius import HeliusSource
from tape.store import Store


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}min"
    return f"{minutes / 60:.1f}h"


def _ms(date_str: str) -> int:
    return int(dt.datetime.strptime(date_str, "%Y-%m-%d")
                .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _existing_swap_count(store: Store, mint: str) -> int:
    """Same real-query idempotency check as scripts/backfill.py's
    (D36) -- never a filename or in-memory cache."""
    df = store.sql(f"SELECT count(*) AS n FROM swaps WHERE mint = '{mint}'")
    return int(df["n"][0])


def _discover_phase(source: BitquerySource, since_ms: int, until_ms: int, mints_file: Path) -> None:
    done = set()
    if mints_file.exists():
        done = {line.split("\t")[0] for line in mints_file.read_text().splitlines() if line.strip()}
        print(f"Resuming discovery: {len(done)} mint(s) already in {mints_file}")
    mints_file.parent.mkdir(parents=True, exist_ok=True)
    new_count = 0
    t0 = time.time()
    with open(mints_file, "a") as f:
        for entry in source.discover(since_ms, until_ms):
            if entry["mint"] in done:
                continue
            done.add(entry["mint"])
            new_count += 1
            f.write(f"{entry['mint']}\t{entry['first_seen_ts_ms']}\n")
            f.flush()
            if new_count % 50 == 0:
                print(f"  ... {new_count} new mint(s) discovered so far "
                      f"({_fmt_duration(time.time() - t0)} elapsed)")
    print(f"Discovery done: {new_count} new mint(s), {len(done)} total in {mints_file}.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", default=None,
                     help="YYYY-MM-DD. Date-only -- for dataset=realtime use "
                          "--last-hours instead (D45, docs/DECISIONS.md): a date has "
                          "no time-of-day, so '--since <today>' can already be many "
                          "hours stale by the time this runs, well past realtime's "
                          "~9h reach (D21).")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD, see --since")
    ap.add_argument("--last-hours", type=float, default=None,
                     help="alternative to --since/--until: window is "
                          "[now - N hours, now], computed at run time to the second. "
                          "REQUIRED precision for dataset=realtime (D21/D45) -- "
                          "--since/--until's date-only granularity cannot safely "
                          "express a window under a day. Not meaningful for "
                          "dataset=archive, which has no such reach limit (D39/D40).")
    ap.add_argument("--data", default="data", help="Store root (default: data)")
    ap.add_argument("--mints-file", default="data/bitquery_discovered_mints.txt",
                     help="where discovered mints are written/read (tab-separated "
                          "mint, first_seen_ts_ms)")
    ap.add_argument("--skip-discover", action="store_true",
                     help="use --mints-file as-is, don't run discover() again "
                          "(e.g. a second pass over an already-discovered universe)")
    ap.add_argument("--limit", type=int, default=200,
                     help="max mints to BACKFILL this run (default 200 -- discovery "
                          "runs to completion regardless, since it's cheap and "
                          "checkpointed; the historical() pull per mint is the "
                          "expensive part)")
    ap.add_argument("--client-id", default=None)
    ap.add_argument("--client-secret", default=None)
    ap.add_argument("--corroborate-every", type=int, default=0,
                     help="also cross-check every Nth backfilled mint against Helius "
                          "(0 = off, the default -- see module docstring)")
    ap.add_argument("--dataset", default=DATASET_ARCHIVE, choices=VALID_DATASETS,
                     help="default 'archive' -- the whole point of this script. Pass "
                          "'realtime' ONLY as a fallback while archive access is being "
                          "sorted out with Bitquery (D40, docs/DECISIONS.md): realtime "
                          "can't reach back more than ~9-12h (D21), so --since/--until "
                          "must both fall inside that window or historical() will raise.")
    args = ap.parse_args()

    if args.last_hours is not None:
        until_ms = int(time.time() * 1000)
        since_ms = until_ms - int(args.last_hours * 3600 * 1000)
        print(f"--last-hours {args.last_hours}: window is "
              f"[{dt.datetime.utcfromtimestamp(since_ms / 1000)}, "
              f"{dt.datetime.utcfromtimestamp(until_ms / 1000)}] UTC")
    elif args.since and args.until:
        since_ms, until_ms = _ms(args.since), _ms(args.until)
    else:
        print("Either --last-hours, or both --since and --until, are required.",
              file=sys.stderr)
        return 2
    mints_file = Path(args.mints_file)

    try:
        bq = BitquerySource(dataset=args.dataset,
                             client_id=args.client_id, client_secret=args.client_secret)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    if not args.skip_discover:
        print("=" * 70)
        print(f"PHASE 1: discovering mints active on pump.fun/PumpSwap between "
              f"{args.since} and {args.until} (dataset={args.dataset}) ...\n")
        _discover_phase(bq, since_ms, until_ms, mints_file)
    elif not mints_file.exists():
        print(f"--skip-discover given but {mints_file} doesn't exist.", file=sys.stderr)
        return 2

    mints = [line.split("\t")[0] for line in mints_file.read_text().splitlines() if line.strip()]
    mints = mints[: args.limit]
    print(f"\n{'=' * 70}\nPHASE 2: backfilling {len(mints)} mint(s) via "
          f"dataset={args.dataset!r} ...\n")

    store = Store(args.data)
    helius = None
    if args.corroborate_every > 0:
        try:
            helius = HeliusSource()
        except RuntimeError as e:
            print(f"--corroborate-every given but Helius isn't configured: {e}", file=sys.stderr)
            print("Continuing WITHOUT corroboration.", file=sys.stderr)

    t0 = time.time()
    attempt_elapsed = []
    succeeded, failed, skipped, too_thin = [], [], [], []
    total_swaps = 0
    for i, mint in enumerate(mints, start=1):
        if mint in KNOWN_QUOTE_MINTS:
            # D46/D52 (docs/DECISIONS.md): CONFIRMED LIVE -- discover() could
            # yield a known quote currency (SOL, then USDC too) itself as a
            # "discovered mint" (fixed at the source, tape/sources/bitquery.py),
            # but an already-checkpointed --mints-file from before either fix
            # can still have one. historical(mint=<quote currency>, ...) either
            # collapses onto a nonsensical same-currency filter or onto a
            # real, extremely liquid, totally unrelated pair (a real run spent
            # 400+ pages / 300k+ rows on USDC before being caught). Skip
            # defensively here too rather than requiring every existing
            # mints-file to be hand-cleaned first.
            print(f"[{i}/{len(mints)}] {mint}: this is a known quote currency, "
                  f"not a real token -- skipping (D52)")
            skipped.append(mint)
            continue
        existing = _existing_swap_count(store, mint)
        if existing > 0:
            print(f"[{i}/{len(mints)}] {mint}: already have {existing} swaps, skipping")
            skipped.append(mint)
            continue
        mt0 = time.time()
        try:
            swaps = list(bq.historical(mint, since_ms, until_ms))
        except RuntimeError as e:
            elapsed = time.time() - mt0
            print(f"[{i}/{len(mints)}] {mint}: FAILED after {elapsed:.0f}s -- {e}")
            failed.append((mint, str(e)))
            attempt_elapsed.append(elapsed)
            continue
        elapsed = time.time() - mt0
        attempt_elapsed.append(elapsed)
        if len(swaps) < 50:
            too_thin.append(mint)
        written = store.write_swaps(swaps)
        total_swaps += len(swaps)
        succeeded.append(mint)
        print(f"[{i}/{len(mints)}] {mint}: {len(swaps)} swaps written to "
              f"{len(written)} file(s) in {elapsed:.0f}s")

        if helius is not None and args.corroborate_every > 0 and i % args.corroborate_every == 0:
            try:
                helius_swaps = list(helius.historical(mint, since_ms, until_ms))
                stats = corroborate(swaps, helius_swaps)
                print(f"  [corroborate vs Helius] {stats}")
            except RuntimeError as e:
                print(f"  [corroborate vs Helius] FAILED, not fatal to the batch: {e}")

        if attempt_elapsed:
            avg = sum(attempt_elapsed) / len(attempt_elapsed)
            remaining = len(mints) - i
            print(f"  pace: avg {avg:.0f}s/mint, ~{remaining} left, "
                  f"ETA ~{_fmt_duration(avg * remaining)}")

    print("\n" + "=" * 70)
    print(f"Done in {_fmt_duration(time.time() - t0)}. {len(succeeded)}/{len(mints)} "
          f"mints backfilled, {total_swaps} swaps written.")
    if skipped:
        print(f"  {len(skipped)} already in the store, skipped.")
    if too_thin:
        print(f"  {len(too_thin)} had < 50 swaps in this window.")
    if failed:
        print(f"  {len(failed)} FAILED:")
        for mint, reason in failed:
            print(f"    {mint}: {reason}")
    print(f"\nNext: python scripts/information_audit.py --data {args.data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
