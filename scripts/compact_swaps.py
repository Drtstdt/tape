#!/usr/bin/env python3
"""D81 -- compact data/swaps/venue=*/dt=*/ Parquet partitions from many
small part files down to one file per partition.

Real evidence (docs/DECISIONS.md D80): a real DuckDB OutOfMemoryException on
a routine chronological_universe() query, even after adding an explicit
memory_limit/temp_directory (D80's first attempt). The user's own file-count
check confirmed the real cause: 2,196 Parquet files for only 114.6 MB of
actual data (~52 KB/file average). `tape/store.py::write_swaps()` writes one
NEW part file per (venue, dt) group on every call (by design -- append-only,
never rewrites), and this session has called it dozens of times across many
incremental `backfill_discovered_launches.py` runs, so the file count grew
far out of proportion to the data volume. `Store._register_views()` globs
and unions ALL of them with `hive_partitioning=1, union_by_name=1` on every
connection -- schema reconciliation across thousands of tiny files carries
real per-file overhead that is a plausible, evidence-consistent explanation
for hitting an allocation failure on a dataset this small.

WHAT THIS DOES: for every `data/swaps/venue=<v>/dt=<d>/` directory holding
more than one `part-*.parquet` file, reads ALL of them, concatenates (no
deduplication -- see below), writes ONE new file, verifies the row count
matches before removing anything, then deletes the old part files. The hive
directory structure itself (venue=/dt= layout) is untouched -- only the
files inside each leaf directory are consolidated -- so nothing about how
Store reads the data changes, only how many files it has to open to do it.

WHY NO DEDUPLICATION HERE: `tape/store.py`'s own `swaps` VIEW already
deduplicates on read (`ROW_NUMBER() OVER (PARTITION BY sig, mint, side,
round(base_amount, 12) ORDER BY source) WHERE rn=1`, per that view's own
docstring: "Dedup on READ, not write... running two sources over the same
pool simultaneously is the point"). Re-implementing that same logic here,
at the file level, risks a subtle mismatch with the view's actual behavior
that would silently change which rows are visible. Compaction here ONLY
reduces file count -- it preserves the exact same multiset of raw rows the
existing read-time dedup already operates on, so query RESULTS are
unaffected; only the number of files DuckDB has to scan changes.

SAFETY: every partition is written to a new temp-named file first, and row
counts are checked, before any original file is deleted -- a crash or
Ctrl-C mid-run leaves either the untouched originals or (once the new file
is confirmed and originals are being removed) a still-complete dataset,
never a state with data missing. Uses pandas/pyarrow directly (already a
project dependency, tape/store.py's own write_swaps) -- deliberately NOT
DuckDB, so this script cannot hit the very OOM it exists to fix.

    python scripts/compact_swaps.py --data data --dry-run   (preview, changes nothing)
    python scripts/compact_swaps.py --data data             (actually compacts)
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--dry-run", action="store_true",
                     help="report what would be compacted without changing any files")
    args = ap.parse_args()

    import pandas as pd

    swaps_root = Path(args.data) / "swaps"
    if not swaps_root.exists():
        print(f"{swaps_root} does not exist -- nothing to compact.", file=sys.stderr)
        return 2

    # Leaf directories are exactly two levels down: venue=*/dt=*/
    leaf_dirs = sorted(p for p in swaps_root.glob("venue=*/dt=*") if p.is_dir())
    print(f"found {len(leaf_dirs)} venue/dt partition(s) under {swaps_root}")

    total_files_before = 0
    total_files_after = 0
    total_bytes_before = 0
    total_bytes_after = 0
    partitions_touched = 0
    partitions_skipped = 0

    for leaf in leaf_dirs:
        parts = sorted(leaf.glob("part-*.parquet"))
        n_files = len(parts)
        bytes_before = sum(p.stat().st_size for p in parts)
        total_files_before += n_files
        total_bytes_before += bytes_before
        if n_files <= 1:
            partitions_skipped += 1
            total_files_after += n_files
            total_bytes_after += bytes_before
            continue

        if args.dry_run:
            print(f"  {leaf}: {n_files} files, {bytes_before / 1024:.1f} KB -- would compact to 1")
            partitions_touched += 1
            total_files_after += 1  # projected
            total_bytes_after += bytes_before  # projected (compaction doesn't add/remove rows)
            continue

        frames = [pd.read_parquet(p) for p in parts]
        row_count_before = sum(len(f) for f in frames)
        merged = pd.concat(frames, ignore_index=True)
        if len(merged) != row_count_before:
            print(f"  {leaf}: ROW COUNT MISMATCH after concat ({len(merged)} vs {row_count_before}) "
                  f"-- refusing to touch this partition, skipping.", file=sys.stderr)
            partitions_skipped += 1
            total_files_after += n_files
            total_bytes_after += bytes_before
            continue

        tmp_path = leaf / f"part-{uuid.uuid4().hex[:12]}.parquet.tmp"
        merged.to_parquet(tmp_path, index=False, compression="zstd")

        # Verify the written file round-trips to the same row count before
        # deleting anything -- never trust a write until it's read back.
        check = pd.read_parquet(tmp_path)
        if len(check) != row_count_before:
            print(f"  {leaf}: written file failed row-count verification "
                  f"({len(check)} vs {row_count_before}) -- deleting the temp file, "
                  f"leaving originals untouched.", file=sys.stderr)
            tmp_path.unlink(missing_ok=True)
            partitions_skipped += 1
            total_files_after += n_files
            total_bytes_after += bytes_before
            continue

        for p in parts:
            p.unlink()
        final_path = leaf / f"part-{uuid.uuid4().hex[:12]}.parquet"
        tmp_path.rename(final_path)
        bytes_after = final_path.stat().st_size
        total_files_after += 1
        total_bytes_after += bytes_after
        partitions_touched += 1
        print(f"  {leaf}: {n_files} files ({bytes_before / 1024:.1f} KB) -> 1 file "
              f"({bytes_after / 1024:.1f} KB), {row_count_before} rows preserved")

    print("\n" + "=" * 78)
    if args.dry_run:
        print(f"DRY RUN -- would touch {partitions_touched} partition(s), leave "
              f"{partitions_skipped} untouched (already 1 file)")
        print(f"  files: {total_files_before} -> ~{total_files_after} (projected)")
    else:
        print(f"compacted {partitions_touched} partition(s), left {partitions_skipped} untouched")
        print(f"  files: {total_files_before} -> {total_files_after}")
        print(f"  bytes: {total_bytes_before / 1024 / 1024:.1f} MB -> {total_bytes_after / 1024 / 1024:.1f} MB "
              f"(should match closely -- compaction doesn't add/remove data)")
        print("\nDelete data/tape.duckdb (the cached connection file) if it exists, or just re-run "
              "your script -- Store opens a fresh view over the new file layout on next connect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
