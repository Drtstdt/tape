#!/usr/bin/env python3
"""D38 (2026-09-22, docs/DECISIONS.md) -- the universe-scaling backfill.
`scripts/backfill.py` only pulls swaps for mints this project already has a
v3 tape filename for (~50, capped by how many tokens were recorded live
before this rebuild started). This script has no such ceiling: it consumes
a flat signature list from `scripts/bigquery_discover.py` (or anywhere
else -- it doesn't care where the signatures came from) and decodes each
one via `HeliusSource.from_signatures()` (D38), which discovers whatever
mint(s) the transaction actually touched instead of requiring one known in
advance. Every mint that ever traded on pump.fun/PumpSwap in the
discovery window ends up in the Store, not just the ones this project
happened to record tapes for.

This is genuinely free-to-cheap: BigQuery discovery costs whatever a
narrow, dry-run-checked query costs (see bigquery_discover.py -- often
nothing, inside the free tier, if scoped to a sane date range), and the
`getTransaction` calls here run against the SAME Helius Developer plan
credits `scripts/backfill.py` already spends (docs/DATA.md, $49/mo,
already budgeted, not a new cost).

CHECKPOINTED (`--checkpoint`, default `<data>/backfill_wide_checkpoint.txt`):
a discovery-driven signature list has no natural since_ms/until_ms window
to resume by half-open interval the way scripts/backfill.py's per-mint
loop does, and can be many thousands of signatures long. Losing progress
on an interrupted run here means re-paying for `getTransaction` calls
already made -- the exact class of waste D36 fixed on the per-mint path,
now avoided here from the start rather than found the hard way twice.

    python scripts/bigquery_discover.py --since 2026-06-01 --until 2026-09-01 --print-query
    # ... run the dry-run, then the real bq query, export JSON ...
    python scripts/bigquery_discover.py --load discovery_export.json
    python scripts/backfill_wide.py --signatures data/discovered_signatures.txt --limit 5000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.sources.helius import HeliusSource
from tape.store import Store


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}min"
    return f"{minutes / 60:.1f}h"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--signatures", required=True,
                     help="path from scripts/bigquery_discover.py's --out (one signature per line)")
    ap.add_argument("--data", default="data", help="Store root (default: data)")
    ap.add_argument("--limit", type=int, default=2000,
                     help="max signatures to process THIS run (default 2000 -- each is one "
                          "getTransaction call; raise once a small run looks right, same "
                          "reasoning as scripts/backfill.py's --limit)")
    ap.add_argument("--checkpoint", default=None,
                     help="resume file (default: <data>/backfill_wide_checkpoint.txt)")
    args = ap.parse_args()

    sig_path = Path(args.signatures)
    if not sig_path.exists():
        print(f"{sig_path} does not exist -- run scripts/bigquery_discover.py --load first.",
              file=sys.stderr)
        return 2
    all_sigs = [line.strip() for line in sig_path.read_text().splitlines() if line.strip()]
    if not all_sigs:
        print(f"{sig_path} is empty.", file=sys.stderr)
        return 2
    sigs = all_sigs[: args.limit]
    print(f"Processing {len(sigs)}/{len(all_sigs)} signatures from {sig_path} "
          f"(--limit {args.limit}).\n")

    try:
        source = HeliusSource()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    store = Store(args.data)
    checkpoint = args.checkpoint or str(Path(args.data) / "backfill_wide_checkpoint.txt")
    Path(checkpoint).parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    swaps_by_mint: dict = {}
    total_swaps = 0
    try:
        for swap in source.from_signatures(sigs, checkpoint_path=checkpoint):
            swaps_by_mint.setdefault(swap.mint, []).append(swap)
            total_swaps += 1
            if total_swaps % 500 == 0:
                elapsed = time.time() - t0
                print(f"  ... {total_swaps} swaps decoded across {len(swaps_by_mint)} "
                      f"mint(s) so far, {_fmt_duration(elapsed)} elapsed")
    except RuntimeError as e:
        print(f"\nStopped early by a real error (progress up to here is checkpointed, "
              f"safe to re-run): {e}", file=sys.stderr)

    written_files = 0
    for mint, mint_swaps in swaps_by_mint.items():
        written_files += len(store.write_swaps(mint_swaps))

    elapsed = time.time() - t0
    print("\n" + "=" * 70)
    print(f"Done in {_fmt_duration(elapsed)}. {total_swaps} swaps decoded, "
          f"{len(swaps_by_mint)} distinct mint(s) discovered, "
          f"{written_files} file(s) written.")
    thin = sorted(m for m, s in swaps_by_mint.items() if len(s) < 50)
    if thin:
        print(f"  {len(thin)} of those mints have < 50 swaps in THIS batch (won't count "
              f"toward store.mints()'s min_swaps=50 floor on their own -- a later batch "
              f"covering more of their history may push them over it).")
    print(f"\nResume point saved to {checkpoint} -- re-running with the same "
          f"--signatures file will skip everything already processed.")
    print(f"Next: python scripts/information_audit.py --data {args.data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
