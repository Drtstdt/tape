#!/usr/bin/env python3
"""Real backfill: pull real swaps for real mints into the Store, via the
verified `HeliusSource` (D31/D34, docs/DECISIONS.md -- side convention
confirmed live, corr(tape_buy, vendor_buy)=0.804 / corr(tape_sell,
vendor_sell)=0.653, matching D23's BigQuery balance-delta numbers almost
exactly).

WHY THIS IS THE NEXT STEP, not more adapter work: `scripts/information_audit.py`
(Stage 1's information audit, pre-registered in docs/PLAN.md) already
exists, complete -- bars, features, labels, purged-by-token CV, per-feature
AUC, the whole gate. It needs nothing new written. Its ONLY blocker is
`store.mints()` returning at least 30 mints with >= 50 swaps each. This
script is the one thing standing between "adapter verified" and "run the
actual Stage 1 gate".

Uses the ~50 mints this project already has v3 tapes for (v3/data_deep/<date>/)
as the initial universe -- not a real point-in-time discovery process
(`discover()` is unimplemented on every adapter, docs/DECISIONS.md), but a
fast way to get real trading history into the Store NOW and find out
whether Stage 1 has anything to say, before building a proper creation-time
universe discovery pipeline. Revisit the universe-selection question before
trusting any RESULT this produces for real trading decisions (D18's
warning: never select on an outcome) -- this is about exercising the
pipeline end to end, not about the final universe.

Each mint is O(signatures) `getTransaction` calls (no batching yet) -- a
busy pool can take tens of minutes. `--limit` defaults low on purpose so a
first run finishes in a reasonable time and proves the pipeline works
before committing to the full set overnight.

SKIPS A MINT ALREADY IN THE STORE by default (checked via a real query, not
a filename cache) -- found the hard way, 2026-09-21: `helius_verify.py`
had already pulled ~18k signatures for one busy mint the night before to
verify the adapter, but never persisted them (it only holds swaps in
memory for the correlation check, then discards them), and this script's
first run picked the SAME mint up again and paid for the full ~18k-signature
pull a second time. Two real fixes for that, not one: (1) this script now
checks the store for existing swaps before fetching and skips (`--refetch`
to force), and (2) `helius_verify.py` now writes what it fetches to the
store too (`--data`), so a verify pass is never wasted work again.

    python scripts/backfill.py --tapes-dir ../v3/data_deep/2026-09-20 --limit 5
    python scripts/backfill.py --tapes-dir ../v3/data_deep/2026-09-20 --limit 999   # the rest, once the first pass looks right
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.scripts.probe import load_tape
from tape.sources.helius import HeliusSource
from tape.store import Store


def _fmt_duration(seconds: float) -> str:
    """Human-scale duration -- seconds while short, then minutes, then hours.
    An ETA in raw seconds for a multi-hour batch is unreadable."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}min"
    return f"{minutes / 60:.1f}h"


def _print_pace(attempt_elapsed: list, done: int, total: int) -> None:
    """Running-average seconds/mint over mints actually FETCHED so far
    (skipped mints are instant and would make the average meaningless), plus
    a projected time and local wall-clock finish for what's left in this
    batch. Requested directly by the user ('so I can see progress and est.')
    after being left watching a backfill with no sense of whether it was
    stuck or just slow (the earlier getSignaturesForAddress bug, D32, made
    that distinction impossible for a different reason -- this is the same
    worry, for a script that's otherwise working fine)."""
    if not attempt_elapsed:
        return
    avg = sum(attempt_elapsed) / len(attempt_elapsed)
    remaining = max(total - done, 0)
    eta_s = avg * remaining
    finish_local = time.strftime("%H:%M", time.localtime(time.time() + eta_s))
    print(f"  pace: avg {avg:.0f}s/mint over {len(attempt_elapsed)} fetched so far "
          f"-> ~{remaining} mint(s) left in this batch, ETA ~{_fmt_duration(eta_s)} "
          f"(around {finish_local} local time if the rest behave similarly; "
          f"skips are instant and not counted in this average)")


def _existing_swap_count(store: Store, mint: str) -> int:
    """A real query against whatever's already on disk, not a filename or
    in-memory cache -- the only thing that can't drift out of sync with
    what actually got written. Safe on a totally fresh/empty store: `swaps`
    is a zero-row typed view in that case (see store.py's `_empty_view_sql`),
    not a missing table, so this returns 0 rather than raising."""
    df = store.sql(f"SELECT count(*) AS n FROM swaps WHERE mint = '{mint}'")
    return int(df["n"][0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tapes-dir", required=True,
                     help="a v3 data_deep/<date> directory of <mint>.jsonl tapes")
    ap.add_argument("--data", default="data", help="Store root (default: data)")
    ap.add_argument("--limit", type=int, default=5,
                     help="max number of tapes to process this run (default 5 -- "
                          "raise once a small run looks right)")
    ap.add_argument("--min-tape-bytes", type=int, default=20_000,
                     help="skip tapes smaller than this -- a tiny tape (a few KB) "
                          "means almost no attributed trading, not worth an "
                          "expensive per-signature pull for a mint that will fail "
                          "store.mints()'s min_swaps=50 filter anyway")
    ap.add_argument("--refetch", action="store_true",
                     help="re-fetch a mint even if the store already has swaps for "
                          "it (default: skip -- see module docstring for why this "
                          "default exists)")
    args = ap.parse_args()

    tapes_dir = Path(args.tapes_dir)
    tape_paths = sorted(
        p for p in tapes_dir.glob("*.jsonl") if p.stat().st_size >= args.min_tape_bytes
    )
    if not tape_paths:
        print(f"No tapes >= {args.min_tape_bytes} bytes found in {tapes_dir}.", file=sys.stderr)
        return 2
    tape_paths = tape_paths[: args.limit]
    print(f"Processing {len(tape_paths)} tapes from {tapes_dir} "
          f"(of the ones >= {args.min_tape_bytes} bytes).\n")

    try:
        source = HeliusSource()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    store = Store(args.data)

    run_start = time.time()
    total_swaps = 0
    succeeded, failed, too_thin, skipped = [], [], [], []
    attempt_elapsed: list = []  # elapsed seconds per mint actually FETCHED (not skipped) -- feeds _print_pace
    for i, tape_path in enumerate(tape_paths, start=1):
        mint = tape_path.stem
        if not args.refetch:
            existing = _existing_swap_count(store, mint)
            if existing > 0:
                print(f"[{i}/{len(tape_paths)}] {mint}: already have {existing} swaps "
                      f"in the store, skipping (--refetch to force)")
                skipped.append(mint)
                continue
        rows = load_tape(tape_path)
        if not rows:
            print(f"[{i}/{len(tape_paths)}] {mint}: no rows in tape, skipping")
            failed.append((mint, "empty tape"))
            continue
        since_ms, until_ms = rows[0]["timestamp"], rows[-1]["timestamp"]
        print(f"[{i}/{len(tape_paths)}] {mint}: window {(until_ms - since_ms) / 1000:.0f}s, "
              f"fetching from Helius ...")
        t0 = time.time()
        try:
            swaps = list(source.historical(mint, since_ms, until_ms))
        except RuntimeError as e:
            elapsed = time.time() - t0
            print(f"  FAILED after {elapsed:.0f}s: {e}")
            failed.append((mint, str(e)))
            attempt_elapsed.append(elapsed)
            _print_pace(attempt_elapsed, i, len(tape_paths))
            continue
        elapsed = time.time() - t0
        attempt_elapsed.append(elapsed)
        if len(swaps) < 50:
            print(f"  only {len(swaps)} swaps in {elapsed:.0f}s -- below store.mints()'s "
                  f"min_swaps=50 floor, won't count toward Stage 1's universe, but "
                  f"writing anyway (cheap, and the corpus filter is applied on READ, "
                  f"not write)")
            too_thin.append(mint)
        written = store.write_swaps(swaps)
        total_swaps += len(swaps)
        succeeded.append(mint)
        print(f"  {len(swaps)} swaps written to {len(written)} file(s) in {elapsed:.0f}s")
        _print_pace(attempt_elapsed, i, len(tape_paths))

    print("\n" + "=" * 70)
    print(f"Done in {_fmt_duration(time.time() - run_start)} wall time. "
          f"{len(succeeded)}/{len(tape_paths)} mints succeeded, "
          f"{total_swaps} swaps written total.")
    if skipped:
        print(f"  {len(skipped)} mint(s) already in the store, skipped: "
              f"{', '.join(skipped)}")
    if too_thin:
        print(f"  {len(too_thin)} mint(s) had < 50 swaps (won't count toward "
              f"Stage 1's universe): {', '.join(too_thin)}")
    if failed:
        print(f"  {len(failed)} mint(s) FAILED:")
        for mint, reason in failed:
            print(f"    {mint}: {reason}")
    print(f"\nNext: python scripts/information_audit.py --data {args.data}")
    print("(it needs >= 30 mints with >= 50 swaps each -- rerun this with a higher "
          "--limit against the rest of the tapes if this batch didn't clear that.)")
    return 0 if (succeeded or skipped) else 1


if __name__ == "__main__":
    raise SystemExit(main())
