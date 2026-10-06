#!/usr/bin/env python3
"""D98 (docs/DECISIONS.md) -- merges pumpfundata.com's downloaded pump_fun
files (scripts/pumpfundata_fetch_plan.py's output) into this project's own
Store, alongside the manually-collected Helius/Bitquery data, so
scripts/paper_trade_replay.py can learn on the combined, far more
calendar-diverse corpus ("łączy dane z zbierania manualnego + pumpfundata").

TWO MODES:

  --self-check <file>   Read ONE real downloaded file through
                         tape/sources/pumpfundata.py's load_file() -- the
                         real pandas+pyarrow Parquet path this project's
                         sandbox could NOT exercise (same pyarrow
                         restriction as D93; only load_file's row-mapping
                         logic was unit-tested there, not the actual Parquet
                         read). Prints the first few converted swaps and a
                         pass/fail summary. Writes NOTHING to the Store.
                         Run this FIRST, on a file you already have, before
                         trusting a full ingest run.

  (default)              Walk --raw-dir, load every file, CROSS-CHECK any
                         `sig` that already exists in the Store from another
                         source (Helius/Bitquery) against this file's own
                         side/amounts -- real overlapping evidence for the
                         side-convention assumption tape/sources/pumpfundata.py's
                         module docstring flagged as unverified -- then
                         store.write_swaps() the new rows. Idempotent via
                         the Store's own read-time dedup (dedup_key), but a
                         `.ingested` marker next to each source file also
                         skips already-processed files so a resumed run
                         doesn't re-read gigabytes of Parquet for nothing.

    python scripts/ingest_pumpfundata.py --self-check "F:\\pumpfundata\\pump_fun\\date=2026-02-08\\hour=06.parquet"
    python scripts/ingest_pumpfundata.py --raw-dir F:\\pumpfundata --data data

After this, train the bot on the combined corpus the normal way (nothing
new needed here -- D89 already made this idempotent/safe to re-run):

    python scripts/paper_trade_replay.py --data data --model-in data\\online_policy_state.json --model-out data\\online_policy_state.json

D100 (docs/DECISIONS.md) -- a real run over ~3000 real pumpfundata files
reported getting SLOWER as it went (confirmed by the user watching the
per-file progress lines, not assumed). Real cause, found in `tape/store.py`
and matching this project's own earlier D80/D81 incident at smaller scale:
`Store.write_swaps()` writes one brand-new small Parquet part file per call,
and `Store`'s `swaps` view is a live `read_parquet('data/swaps/**/*.parquet')`
-- not a snapshot -- so every per-file `_corroborate()` query re-scans a
`data/swaps/` directory that is growing by one part file EVERY SINGLE
ITERATION of this exact loop. A straight ~3000-file run reproduces D80/D81's
"many small files" pathology at a larger scale than the incident that first
diagnosed it. Fixed two ways, both load-bearing:

  1. `_corroborate()`'s very first call each run (which happens before this
     run's very first `store.write_swaps()` -- corroborate-then-write is the
     order inside the batch loop below) is what triggers `Store`'s lazy
     `_register_views()`. Once registered, a `Store`'s view is NOT
     re-registered just because new files were written -- so as long as
     corroboration keeps using this SAME `store` object (not a second,
     separately-connected one: DuckDB refuses a second read-write connection
     to the same database file from one process, so two `Store(args.data)`
     instances in one script is a lock-conflict waiting to happen, not just
     a wasted connection), every `_corroborate()` call this run makes stays
     pinned to the pre-run corpus for the rest of the run -- it can never be
     slowed down by this run's own output. Correctness is unaffected either
     way since `_corroborate`'s own `source != 'pumpfundata'` filter was
     already discarding any of this run's own rows regardless of whether its
     view happened to include them.
  2. `--batch-files` (default 20) groups that many source files into one
     `_corroborate()` + `store.write_swaps()` call instead of one per file --
     ~150 write calls (and ~150 new part files) for a 3000-file run instead
     of ~3000. A file only gets its `.ingested` marker once its batch has
     actually been written, so a crash mid-batch loses at most one batch's
     worth of (safely re-doable, nothing-written-twice) work, not more.

Also replaced `_corroborate()`'s `sig IN ('a','b',...)` textual list (which
does not scale -- batching now means a single call can carry tens of
thousands of sigs, and building/parsing a giant SQL literal list is itself
slow and was quietly also costing something on the OLD per-file calls) with
registering the candidate sigs as a real DuckDB-visible table and JOINing --
the standard, non-O(n)-string-building way to do a large membership test.

D101 (docs/DECISIONS.md) -- a real `paper_trade_replay.py` run after a real
ingest found `eligible-by-swaps: 14866` (confirms the merge worked -- orders
of magnitude above this project's long-standing ~61-65-token corpus) but
`excluded(no data): 14731` -- 99% of them. Traced (not guessed) to
`scripts/information_audit.py::filter_by_real_creation`: that gate needs
`data/real_creation_times.json`, a cache previously populated ONLY by
`scripts/fetch_real_creation_times.py` -- one unofficial, ~35%-failure-rate
HTTP call per mint against pump.fun's frontend API -- which had never been
pointed at any pumpfundata-sourced mint. The gate itself is correct and
necessary (D54/D55: without it, Bitquery-"discovered" old survivor tokens
that merely traded again recently get mixed in with genuine new launches);
the fix is feeding it, not loosening it -- same criterion, now actually
populated for the mints this project just spent a purchase budget on.

The fix is free: pumpfundata's own files already carry a `create` event row
per mint with the vendor's own ground-truth launch `timestamp` --
`tape/sources/pumpfundata.py::load_file()` was reading it only to sanity-
check `token_total_supply`, then discarding it. `load_file()` now takes an
optional `creation_times` dict it updates in place with `{mint: ts_ms}`,
and this script merges that into `data/real_creation_times.json` once per
batch via `_merge_creation_times()` -- which never overwrites an existing
recorded `real_created_ts_ms` that disagrees with pumpfundata's own (prints
a warning instead; same "surface a conflict, don't silently pick a side"
culture as `_corroborate()`), and fills in anything previously missing or
merely "error"/"implausible" with no recorded value of its own.

D102 (docs/DECISIONS.md) -- a real `--force` re-run (to backfill D101's
creation times for files already marked `.ingested`) hit a real
`MemoryError` inside `Store.write_swaps()` building a pandas DataFrame for
~2.03M accumulated swaps, at exactly the 20th file of D100's first
`--batch-files`-sized batch. Real pumpfundata hours turned out to carry
100k-150k swaps each -- a fixed FILE-COUNT batch size badly underestimates a
batch's real memory footprint when rows/file varies that much. The flush
trigger is now a DUAL threshold, whichever comes first: `--batch-files`
(unchanged, still an upper bound) OR the new `--batch-max-swaps` (default
100000, calibrated below what this project's pre-D100 one-file-at-a-time
version is known to have handled without issue). `main()`'s loop no longer
groups files via `_batched()` up front (still defined, still tested, just
no longer main()'s flush trigger) -- it flushes inline via the new
`_flush_batch()` helper as soon as either threshold is crossed. The crash
itself was harmless to data already on disk: it happened inside
`pd.DataFrame(rows)`, before `write_swaps()` ever reaches `to_parquet()`, so
nothing partial was written and no `.ingested` marker was (re)set for that
batch -- a plain re-run with the fixed thresholds picks up exactly where it
left off.

D103 (docs/DECISIONS.md) -- after the D102 fix, a real `--force` re-run over
2110 files (grown from 455 -- apparently more files were downloaded
overnight) was reported running at ~1 file/minute, far slower than D100's
fix was meant to achieve. Traced (not guessed, by re-reading this file's own
D101 code against the user's real progress-line evidence) to
`_merge_creation_times()`: it does a full `json.load()` of the ENTIRE
`data/real_creation_times.json` cache, merges, and (if anything changed)
`json.dump()`s the ENTIRE cache back -- on EVERY flush. D102's
`--batch-max-swaps` threshold (real files run 76k-150k swaps each, well
above the 100000 default) now forces a flush almost every 1-2 files instead
of every ~20 (D100's original cadence), so this full-file rewrite -- against
a cache that only grows over a run -- fires at nearly per-file frequency.
Same shape as the D80/D100 "re-scan/rewrite a growing resource every
iteration" pathology, just in JSON instead of DuckDB Parquet.

Fixed by separating the pure in-memory merge from the disk I/O:
`_merge_creation_times_into_cache()` merges `{mint: ts_ms}` into an
already-loaded `cache` dict in place (no I/O at all), and
`_load_creation_times_cache()` / `_save_creation_times_cache()` do the file
reads/writes explicitly. `main()` now loads the cache ONCE at the start of a
run, merges into it on every flush (cheap, in-memory), and persists it to
disk only once every `CREATION_TIMES_PERSIST_EVERY_FLUSHES` flushes, plus
unconditionally at the very end of the run and on the SchemaMismatch-stop
path -- so an unexpected crash can lose at most that many flushes' worth of
creation-time merges, which cost nothing to redo (pure re-reading of
already-downloaded files, not a re-fetch). The old `_merge_creation_times(data_dir,
new_times)` function is kept as a thin load-merge-save-if-changed wrapper
composing the three new pieces, so its external behavior (and its existing
tests) are unchanged -- `_flush_batch()` and `main()` just no longer call it
directly.

`--batch-max-swaps`'s default is deliberately left at 100000: that number
was calibrated against the real MemoryError evidence in D102 (a 20-file,
~2.03M-swap batch crashed; 100000 is a wide safety margin below that), which
is a separate concern from this JSON-I/O cost and shouldn't be loosened
without separate OOM-safety evidence.

D104 (docs/DECISIONS.md) -- a real re-run with D103's fix STILL stalled for
~1.5 minutes right at a flush point, so D103's hypothesis was real but not
the dominant cost. Rather than guess a third time, read the real
`data/swaps/` directory directly off the user's machine via the device
bridge: 710 Parquet files, ~4.66GB, with a single 252-file/~4.5GB partition
for the pump.fun program. Added per-flush timing (`[flush timing] ...`
printed by `_flush_batch()`) and got a real measurement: one flush
(2 files, 149395 swaps) spent `corroborate=141.2s` out of `total=145.4s` --
`write_swaps=4.2s`, everything else ~0s. Confirmed, not guessed:
`_corroborate()`'s query against the `swaps` view re-scans that whole
710-file/4.66GB glob on every single call, and D102's swap-count threshold
now triggers that call almost every 1-2 files.

For a `--force` re-run whose only actual goal is backfilling D101's
creation-time extraction onto files ingested (and already corroborated and
written) before D101 existed, that `_corroborate()`/`write_swaps()` work is
pure waste -- not new verification, and it keeps growing the very corpus
that makes each future call slower. Added `--creation-times-only`: skips
`_corroborate()` and `store.write_swaps()` entirely (no `Store` is even
opened -- `main()` leaves `store` as `None`, and `_flush_batch()` treats a
`None` store as "don't touch the Store this flush"), so a backfill run only
pays for reading each Parquet file and merging its `create`-row timestamps
-- Parquet-read cost only, no longer multiplied by a 141-second-per-flush
tax. Recommended for this exact situation:

    python scripts\\ingest_pumpfundata.py --raw-dir F:\\pumpfundata --data data --force --creation-times-only

`--batch-max-swaps`/`--batch-files` and ordinary (non-`--force`,
non-`--creation-times-only`) runs are UNCHANGED -- corroboration still runs
for genuinely new files, which is the only place it was ever providing new
evidence. The now-confirmed general cost of `_corroborate()` scaling with
the total size of `data/swaps/` (not just this run's own output) is a real,
separate concern for future large pumpfundata ingests and is NOT fixed by
this entry -- flagged here for a later pass (e.g. compacting `data/swaps/`
via `scripts/compact_swaps.py`, or batching corroboration less often),
rather than solved speculatively now.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.schema import CanonicalSwap
from tape.sources.pumpfundata import SchemaMismatch, iter_raw_dir, load_file
from tape.store import Store

AMOUNT_RTOL = 1e-6

# D105 (docs/DECISIONS.md): a dedicated marker for --creation-times-only
# backfill progress, separate from the normal ".ingested" marker. A
# --creation-times-only run is selected by this marker, not ".ingested" --
# see main()'s file-discovery loop and `_flush_now()`'s persist block.
CREATION_TIMES_MARKER_SUFFIX = ".creation_times_backfilled"


def _batched(items: List[Path], n: int) -> Iterator[List[Path]]:
    """Yield successive n-sized chunks of `items` (last chunk may be
    shorter). Pure/stateless on purpose -- D100's batching logic is
    unit-testable without touching Store/DuckDB.

    D102 (docs/DECISIONS.md): `main()`'s flush loop no longer calls this
    directly -- a fixed file-count grouping badly underestimated a batch's
    real memory footprint (real pumpfundata hours carry 100k-150k swaps
    each; a 20-file group hit a real MemoryError at ~2.03M accumulated
    swaps), so the flush trigger is now an inline dual threshold (file count
    OR swap count, whichever comes first). Left in place -- still correct,
    still tested -- as a plain chunking utility, in case a future caller
    only needs a fixed-size-by-count split."""
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _merge_creation_times_into_cache(cache: dict, new_times: Dict[str, int]) -> Tuple[int, List[str]]:
    """Merge `{mint: ts_ms}` into an already-loaded `cache` dict, IN PLACE.
    No file I/O at all -- D103 (docs/DECISIONS.md): this is the part of
    D101's original `_merge_creation_times` that genuinely needs to run on
    every flush; the full-file load/save was the part that didn't, and
    D102's swap-count flush trigger made that full-file cost fire at
    near-per-file frequency (the real cause of a reported ~1-file/minute
    slowdown).

    Same record shape `scripts/information_audit.py` /
    `scripts/fetch_real_creation_times.py` already use
    (`{"status": "ok", "real_created_ts_ms": ...}`). NEVER overwrites an
    existing record that already has a numeric `real_created_ts_ms`
    disagreeing with pumpfundata's own value -- that's a real conflict
    between two sources worth looking at, not something to silently resolve
    by picking one (D101). Does fill in a mint that's missing entirely, or
    whose only existing record has no usable timestamp (e.g. a cached
    "error" from the unofficial API).

    Returns (number of records actually added, conflict messages)."""
    added = 0
    conflicts: List[str] = []
    for mint, ts_ms in new_times.items():
        existing = cache.get(mint)
        existing_ts = existing.get("real_created_ts_ms") if existing else None
        if existing_ts is not None:
            if existing_ts != ts_ms:
                conflicts.append(
                    f"{mint[:16]}...: existing real_created_ts_ms={existing_ts} "
                    f"(status={existing.get('status')}) vs pumpfundata create "
                    f"event={ts_ms} -- kept the existing value.")
            continue
        cache[mint] = {"status": "ok", "real_created_ts_ms": ts_ms,
                       "source": "pumpfundata_create_event"}
        added += 1
    return added, conflicts


def _load_creation_times_cache(data_dir: Path) -> dict:
    """Read `<data_dir>/real_creation_times.json` once (D103) -- an empty
    dict if it doesn't exist yet. Callers hold onto the returned dict and
    merge into it in memory via `_merge_creation_times_into_cache()` rather
    than reloading it on every flush."""
    path = data_dir / "real_creation_times.json"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_creation_times_cache(data_dir: Path, cache: dict) -> None:
    """Write the whole `cache` dict to `<data_dir>/real_creation_times.json`
    (D103) -- the caller decides how often this is worth calling; it is an
    O(cache size) full-file rewrite every time, by design the ONLY place
    that cost is paid."""
    path = data_dir / "real_creation_times.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def _merge_creation_times(data_dir: Path, new_times: Dict[str, int]) -> Tuple[int, List[str]]:
    """Thin backward-compatible wrapper around the three functions above:
    load the on-disk cache, merge `new_times` into it in memory, write back
    only if anything actually changed. Returns (number of records actually
    added, conflict messages).

    D103 (docs/DECISIONS.md): `main()`'s flush loop no longer calls this
    directly -- doing a full load+save on every flush was itself a
    D80/D100-shaped "re-scan/rewrite a growing resource every iteration"
    cost, and D102's swap-count flush trigger made it fire far more often
    than D100's original ~1-per-20-files cadence. `main()` instead loads the
    cache ONCE, merges in memory on every flush via
    `_merge_creation_times_into_cache()`, and persists with
    `_save_creation_times_cache()` only periodically. This function is kept
    only for its own existing tests and any other caller that wants the old
    one-shot load-merge-save behavior."""
    cache = _load_creation_times_cache(data_dir)
    added, conflicts = _merge_creation_times_into_cache(cache, new_times)
    if added:
        _save_creation_times_cache(data_dir, cache)
    return added, conflicts


def self_check(path: Path) -> int:
    print(f"Self-check: {path}")
    if not path.exists():
        print(f"  FILE NOT FOUND: {path}")
        return 1
    creation_times: Dict[str, int] = {}
    try:
        swaps = load_file(path, creation_times=creation_times)
    except SchemaMismatch as e:
        print(f"  SCHEMA MISMATCH (this is exactly what --self-check is for): {e}")
        return 1
    except Exception as e:  # noqa: BLE001 -- deliberately broad: this mode's
                            # whole job is to surface a real Parquet-read
                            # problem clearly, not let it look like a crash.
        print(f"  FAILED to read/parse: {type(e).__name__}: {e}")
        return 1

    print(f"  {len(creation_times)} 'create' row(s) with a usable launch "
          f"timestamp found in this file (would feed data/real_creation_times.json "
          f"on a real run -- D101, docs/DECISIONS.md).")

    if not swaps:
        print("  0 swap rows produced (file may be all create/bonding_complete "
              "rows, or every swap row failed to parse -- check the WARNING "
              "lines above, if any, from load_file's create-row sanity check).")
        return 0

    print(f"  PASS: {len(swaps)} swap rows parsed without a SchemaMismatch "
          f"(load_file's built-in self-check would have raised otherwise). "
          f"If a NOTE line appeared above, a small fraction of timestamps "
          f"landed just outside the filename's hour window -- tolerated as "
          f"vendor boundary noise, not a parsing bug; see D98/D99 in "
          f"docs/DECISIONS.md.")
    mints = {s.mint for s in swaps}
    buys = sum(1 for s in swaps if s.side == "buy")
    print(f"  distinct mints: {len(mints)}   buy/sell: {buys}/{len(swaps) - buys}")
    print(f"  price range: {min(s.price for s in swaps):.9g} .. "
          f"{max(s.price for s in swaps):.9g} SOL/token")
    print("  first 3 converted swaps:")
    for s in swaps[:3]:
        print(f"    mint={s.mint[:12]}... side={s.side} ts_ms={s.ts_ms} "
              f"base_amount={s.base_amount:.6g} quote_amount={s.quote_amount:.6g} "
              f"price={s.price:.9g} venue={s.venue[:12]}...")
    return 0


def _corroborate(store: Store, swaps: List[CanonicalSwap]) -> dict:
    """Compare any swap here whose `sig` already exists in the Store from a
    DIFFERENT source -- real overlapping evidence for whether pumpfundata's
    `action` (buy/sell) and amount scaling actually agree with this
    project's already-trusted Helius/Bitquery data. Returns a tally dict;
    never raises, never blocks the write -- the caller decides what to do
    with disagreements.

    D100 (docs/DECISIONS.md): candidate sigs are registered as a real table
    and JOINed, not interpolated into a `sig IN ('a','b',...)` literal list.
    `--batch-files` can hand this tens of thousands of sigs at once; a
    literal list that size is itself slow to build and parse, on top of not
    being how DuckDB expects a large membership test to be expressed. `store`
    is expected to be a Store whose `con` is safe to call `.register()` /
    `.unregister()` on (any real Store qualifies -- these are standard
    DuckDB connection methods, not something Store adds)."""
    sigs = sorted({s.sig for s in swaps if s.sig})
    if not sigs:
        return {"checked": 0, "side_agree": 0, "side_disagree": 0,
                "amount_agree": 0, "amount_disagree": 0, "disagreements": []}
    import pandas as pd

    con = store.con
    sig_df = pd.DataFrame({"sig": sigs})
    con.register("_pumpfundata_corrob_sigs", sig_df)
    try:
        df = store.sql(
            "SELECT s.sig AS sig, s.side AS side, s.base_amount AS base_amount, "
            "s.quote_amount AS quote_amount, s.source AS source "
            "FROM swaps s JOIN _pumpfundata_corrob_sigs c ON s.sig = c.sig "
            "WHERE s.source != 'pumpfundata'"
        )
    finally:
        con.unregister("_pumpfundata_corrob_sigs")
    existing_by_sig: dict = {}
    for row in df.to_dict("records"):
        existing_by_sig.setdefault(row["sig"], []).append(row)

    tally = {"checked": 0, "side_agree": 0, "side_disagree": 0,
             "amount_agree": 0, "amount_disagree": 0, "disagreements": []}
    for s in swaps:
        for existing in existing_by_sig.get(s.sig, []):
            tally["checked"] += 1
            if existing["side"] == s.side:
                tally["side_agree"] += 1
            else:
                tally["side_disagree"] += 1
                tally["disagreements"].append(
                    f"sig={s.sig[:16]}... pumpfundata side={s.side} vs "
                    f"{existing['source']} side={existing['side']}")
                continue
            ref = max(abs(existing["base_amount"]), 1e-12)
            if abs(existing["base_amount"] - s.base_amount) / ref <= AMOUNT_RTOL:
                tally["amount_agree"] += 1
            else:
                tally["amount_disagree"] += 1
                tally["disagreements"].append(
                    f"sig={s.sig[:16]}... pumpfundata base_amount={s.base_amount:.6g} "
                    f"vs {existing['source']} base_amount={existing['base_amount']:.6g}")
    return tally


def _flush_batch(store: Optional[Store], batch_swaps: List[CanonicalSwap],
                  batch_ok_paths: List[Path], batch_creation_times: Dict[str, int],
                  creation_times_cache: Optional[dict]) -> dict:
    """Corroborate + write_swaps + merge creation times (IN MEMORY ONLY,
    see D103) + (when `store is not None`) mark `.ingested` for one flush. A
    plain function (not a closure) so main()'s loop can call it from two
    different trigger points (file-count ceiling AND swap-count ceiling, see
    D102) without nested-function/`nonlocal` bookkeeping. Returns a dict the
    caller folds into its running totals; never mutates `batch_swaps`/
    `batch_ok_paths`/`batch_creation_times` (the caller resets its own
    accumulators) -- but DOES mutate `creation_times_cache` in place, same as
    a dict's `.update()` would, since that cache is meant to accumulate
    across every flush of the run.

    `store` may be `None` -- D104 (docs/DECISIONS.md): `--creation-times-only`
    skips `_corroborate()`/`store.write_swaps()` ENTIRELY in that case (real
    timing evidence: one real flush spent 141.2s of 145.4s total inside
    `_corroborate()`, which re-scans the WHOLE `data/swaps/` directory --
    710 files/~4.66GB and growing -- on every call; `write_swaps()` itself
    took only 4.2s). This is for re-processing files that were already
    ingested (and already corroborated/written) before D101's creation-time
    extraction existed -- redoing corroboration/writes for them is pure
    waste, not new verification. When `store is None`, this function does
    NOT write any marker for `batch_ok_paths` -- D105 (docs/DECISIONS.md):
    `main()` owns marking `--creation-times-only` progress, and only once
    the creation-times cache has actually been persisted to disk (not merely
    merged in memory), so a crash can't lose a file's creation times while
    believing it already done.

    `creation_times_cache` is the WHOLE RUN's in-memory creation-times dict,
    or `None` to skip creation-time handling entirely (`--no-creation-times`).
    This function never touches disk for it -- D103 (docs/DECISIONS.md): a
    full load+save of the on-disk cache on every flush was itself a
    D80/D100-shaped pathology, firing at near-per-file frequency once D102's
    swap-count threshold made flushes that frequent. `main()` owns loading
    the cache once and persisting it periodically."""
    result = {
        "corroboration": {"checked": 0, "side_agree": 0, "side_disagree": 0,
                          "amount_agree": 0, "amount_disagree": 0, "disagreements": []},
        "creation_times_added": 0, "creation_time_conflicts": [],
    }
    # D104 (docs/DECISIONS.md): per-phase timing, printed below -- after
    # D103's fix, a real re-run still stalled for ~1.5 minutes right at a
    # flush point, so D103's hypothesis (the JSON cache rewrite) was NOT the
    # whole story. Real evidence gathered directly from the user's machine
    # via the device bridge: `data/swaps/` is now 710 files / ~4.66GB, with a
    # single 252-file/~4.5GB partition for the pump.fun program -- the
    # `swaps` view's `read_parquet('data/swaps/**/*.parquet', ...)` glob
    # that `_corroborate()` queries has to scan through that on every call,
    # a cost nobody had measured in isolation before. This timing line
    # exists to find out, from a real run, whether that scan (not
    # write_swaps, not creation-time merging) is actually where the time
    # goes -- rather than guessing a third time.
    t0 = time.monotonic()
    if store is not None and batch_swaps:
        c = _corroborate(store, batch_swaps)
        t_corroborate = time.monotonic()
        for k in ("checked", "side_agree", "side_disagree",
                  "amount_agree", "amount_disagree"):
            result["corroboration"][k] = c[k]
        result["corroboration"]["disagreements"] = c["disagreements"]
        store.write_swaps(batch_swaps)
        t_write = time.monotonic()
    else:
        t_corroborate = t_write = t0
    if batch_creation_times and creation_times_cache is not None:
        added, conflicts = _merge_creation_times_into_cache(
            creation_times_cache, batch_creation_times)
        result["creation_times_added"] = added
        result["creation_time_conflicts"] = conflicts
    t_creation_times = time.monotonic()
    # D105 (docs/DECISIONS.md): only mark `.ingested` here when a Store write
    # actually happened this flush (`store is not None`) -- that write IS
    # the durability event these markers are meant to track. In
    # --creation-times-only mode (`store is None`) there is no write here to
    # tie a marker to; `main()` instead marks files with the separate
    # `CREATION_TIMES_MARKER_SUFFIX` marker, and only once the creation-times
    # cache has actually been persisted to disk -- see `_flush_now()`.
    # Marking here, before that persist, would let a crash between "marked
    # done" and "actually saved" silently lose that file's creation times
    # forever (the next run would skip it, believing it already done).
    if store is not None:
        for p in batch_ok_paths:
            p.with_name(p.name + ".ingested").write_text("ok\n")
    t_mark = time.monotonic()
    print(f"  [flush timing] {len(batch_ok_paths)} file(s), {len(batch_swaps)} swap(s): "
          f"corroborate={t_corroborate - t0:.1f}s  write_swaps={t_write - t_corroborate:.1f}s  "
          f"creation_times={t_creation_times - t_write:.1f}s  "
          f"mark_ingested={t_mark - t_creation_times:.1f}s  total={t_mark - t0:.1f}s")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--self-check", default=None, metavar="FILE",
                     help="read one real file, print a report, write nothing")
    ap.add_argument("--raw-dir", default=r"F:\pumpfundata")
    ap.add_argument("--data", default="data")
    ap.add_argument("--force", action="store_true",
                     help="re-process files even if a .ingested marker exists")
    ap.add_argument("--batch-files", type=int, default=20,
                     help="D100 (docs/DECISIONS.md): an UPPER BOUND on how "
                          "many source files go into one corroborate + "
                          "write_swaps + mark-.ingested flush -- avoids the "
                          "D80/D81 'many small Parquet files' pathology at "
                          "the scale a full pumpfundata run creates it at. "
                          "The actual flush trigger is whichever of this or "
                          "--batch-max-swaps is hit first (D102). A crash "
                          "mid-batch only loses that batch's (harmlessly "
                          "re-doable) work, never more.")
    ap.add_argument("--batch-max-swaps", type=int, default=100_000,
                     help="D102 (docs/DECISIONS.md): ALSO flush once "
                          "accumulated swaps in the current batch reach this "
                          "many, even if --batch-files hasn't been reached -- "
                          "a real MemoryError on the user's own machine "
                          "(Store.write_swaps() building a pandas DataFrame "
                          "for ~2.03M swaps from a 20-file batch) showed real "
                          "pumpfundata hours can carry 100k-150k swaps each, "
                          "so a fixed FILE count badly underestimates a "
                          "batch's real memory footprint. 100000 is "
                          "calibrated below that -- roughly one busy real "
                          "hour, the size this project's pre-batching, "
                          "one-file-at-a-time version is known to have "
                          "handled without issue.")
    ap.add_argument("--no-creation-times", action="store_true",
                     help="D101 (docs/DECISIONS.md): skip extracting 'create' "
                          "row launch timestamps and merging them into "
                          "data/real_creation_times.json. Off by default -- "
                          "this is what unblocks scripts/information_audit.py's "
                          "/ scripts/paper_trade_replay.py's real-creation-time "
                          "gate for pumpfundata-sourced mints, free, from data "
                          "already downloaded.")
    ap.add_argument("--creation-times-only", action="store_true",
                     help="D104 (docs/DECISIONS.md): skip _corroborate() and "
                          "store.write_swaps() ENTIRELY -- only load each file "
                          "for its 'create' row launch timestamps and merge "
                          "them into data/real_creation_times.json. No Store "
                          "connection is even opened. For backfilling D101's "
                          "creation-time extraction onto files already "
                          "ingested (and already corroborated/written) before "
                          "D101 existed: redoing corroboration and writes for "
                          "them is pure waste -- idempotent via Store's "
                          "read-time dedup, but it also grows the "
                          "already-large data/swaps/ corpus for nothing -- "
                          "and, per real timing evidence, is >95% of a "
                          "flush's wall-clock time (one real 2-file/149395-"
                          "swap flush: corroborate()=141.2s vs write_swaps()="
                          "4.2s, out of 145.4s total -- _corroborate()'s "
                          "`swaps` view query re-scans the WHOLE "
                          "data/swaps/ directory, now 710 files/~4.66GB, on "
                          "every single call). Tracks its OWN progress via a "
                          "separate '.creation_times_backfilled' marker (D105, "
                          "docs/DECISIONS.md) -- do NOT pass --force for a "
                          "normal resume after an interruption, a crash, or "
                          "just re-running this command; --force here means "
                          "redo files that marker already says are done.")
    ap.add_argument("--skip-files", default="", metavar="SUBSTR,SUBSTR,...",
                     help="D107 (docs/DECISIONS.md): comma-separated "
                          "substrings -- any file whose path contains one of "
                          "them is excluded from this run ENTIRELY (never "
                          "attempted), instead of this run's SchemaMismatch "
                          "gate stopping everything over it. For a file whose "
                          "boundary-noise diagnostics (the 'offset "
                          "distribution: median=...' line) have been reviewed "
                          "and judged a deliberate, one-off exception -- not "
                          "a silent 'skip past an unverified shape change' "
                          "(this project's own standing rule, D98). Not "
                          "remembered between runs on purpose -- pass it "
                          "again next time, so the exception stays visible "
                          "rather than fading into permanent, invisible "
                          "state. Each skipped file is printed.")
    args = ap.parse_args()

    if args.self_check:
        return self_check(Path(args.self_check))

    if args.creation_times_only and args.no_creation_times:
        print("--creation-times-only and --no-creation-times contradict each "
              "other (one says 'only do creation times', the other says "
              "'skip creation times entirely') -- pick one.", file=sys.stderr)
        return 1

    # D100: ONE Store/connection for the whole run, reused for both
    # corroboration reads and writes -- deliberately NOT two separate
    # Store(args.data) instances (DuckDB refuses a second read-write
    # connection to the same database file from the same process, so that
    # would be a lock-conflict bug, not just redundant). The batch loop below
    # always corroborates a batch BEFORE writing it, so this `store`'s very
    # first `.sql()` call (which lazily registers its `swaps` view) happens
    # before this run's very first write -- the view then stays pinned to
    # the pre-run corpus for every later batch, however many this run writes.
    #
    # D104 (docs/DECISIONS.md): --creation-times-only skips corroboration and
    # writes entirely, so no Store is opened at all -- `store` stays `None`,
    # and `_flush_batch()` treats that as "don't touch the Store this flush."
    store: Optional[Store] = None
    if args.creation_times_only:
        print("--creation-times-only: skipping corroborate()/write_swaps() "
              "entirely (D104, docs/DECISIONS.md) -- only extracting 'create' "
              "row launch timestamps from each file and merging them into "
              "data/real_creation_times.json.")
    else:
        store = Store(args.data)
    raw_dir = Path(args.raw_dir)
    if not raw_dir.exists():
        print(f"{raw_dir} does not exist -- nothing to ingest. "
              f"(scripts/pumpfundata_fetch_plan.py writes here by default.)",
              file=sys.stderr)
        return 1

    total_files = 0
    total_swaps = 0
    total_mints: set = set()
    total_creation_times_added = 0
    creation_time_conflicts: List[str] = []
    corroboration = {"checked": 0, "side_agree": 0, "side_disagree": 0,
                      "amount_agree": 0, "amount_disagree": 0, "disagreements": []}

    # D105 (docs/DECISIONS.md): --creation-times-only tracks its OWN progress
    # via CREATION_TIMES_MARKER_SUFFIX, not the normal ".ingested" marker --
    # a --creation-times-only run has every reason to revisit files that
    # already have ".ingested" (that's the whole point: they were ingested
    # before D101's creation-time extraction existed), but it still needs
    # its own way to skip files THIS mode has already finished, so a crash
    # or an ordinary re-run doesn't reprocess the whole raw_dir from file 1
    # every time.
    marker_suffix = (CREATION_TIMES_MARKER_SUFFIX if args.creation_times_only
                      else ".ingested")
    skip_patterns = [p.strip() for p in args.skip_files.split(",") if p.strip()]
    base = raw_dir / "pump_fun"
    all_files: List[Path] = []
    skipped_by_pattern: List[Path] = []
    for date_dir in sorted(base.glob("date=*")):
        for file_path in sorted(date_dir.glob("hour=*.parquet")):
            marker = file_path.with_name(file_path.name + marker_suffix)
            if marker.exists() and not args.force:
                continue
            if any(pat in str(file_path) for pat in skip_patterns):
                skipped_by_pattern.append(file_path)
                continue
            all_files.append(file_path)

    # D107 (docs/DECISIONS.md): --skip-files exceptions are printed, never
    # silent -- this is a deliberate, reviewed one-off exclusion, not the
    # same thing as a SchemaMismatch being quietly ignored.
    if skipped_by_pattern:
        print(f"--skip-files: excluding {len(skipped_by_pattern)} file(s) "
              f"from this run entirely (D107, docs/DECISIONS.md) -- "
              f"reviewed and deliberately excepted:")
        for p in skipped_by_pattern:
            print(f"    {p}")

    print(f"{len(all_files)} file(s) to ingest (already-{marker_suffix} files "
          f"skipped), flushing every {args.batch_files} files or "
          f"{args.batch_max_swaps} swaps, whichever comes first (D102) ...")

    n_total = len(all_files)
    data_dir = Path(args.data)
    batch_swaps: List[CanonicalSwap] = []
    batch_ok_paths: List[Path] = []
    batch_creation_times: Dict[str, int] = {}

    # D103 (docs/DECISIONS.md): load the on-disk creation-times cache ONCE
    # for the whole run, instead of _merge_creation_times() re-loading and
    # re-saving the entire file on every flush (the real cause of a reported
    # ~1-file/minute slowdown once D102's swap-count threshold made flushes
    # that frequent). `None` means --no-creation-times: skip entirely.
    CREATION_TIMES_PERSIST_EVERY_FLUSHES = 20
    creation_times_cache: Optional[dict] = (
        None if args.no_creation_times else _load_creation_times_cache(data_dir))
    flush_count = 0
    # D105 (docs/DECISIONS.md): files processed since the LAST actual disk
    # persist, in --creation-times-only mode only. Marked with
    # CREATION_TIMES_MARKER_SUFFIX only once `_save_creation_times_cache()`
    # has actually run for them -- never right after a flush, which only
    # merges in memory (D103). A crash between persists leaves these files
    # unmarked, so the next run correctly re-reads exactly them (cheap: no
    # corroborate/write in this mode) instead of silently losing their
    # creation times while believing them already done.
    pending_creation_times_markers: List[Path] = []

    def _flush_now(final: bool = False) -> None:
        nonlocal batch_swaps, batch_ok_paths, batch_creation_times, flush_count
        nonlocal total_creation_times_added, creation_time_conflicts
        nonlocal pending_creation_times_markers
        if args.creation_times_only:
            pending_creation_times_markers.extend(batch_ok_paths)
        r = _flush_batch(store, batch_swaps, batch_ok_paths,
                          batch_creation_times, creation_times_cache)
        for k in ("checked", "side_agree", "side_disagree",
                  "amount_agree", "amount_disagree"):
            corroboration[k] += r["corroboration"][k]
        corroboration["disagreements"].extend(r["corroboration"]["disagreements"])
        total_creation_times_added += r["creation_times_added"]
        creation_time_conflicts.extend(r["creation_time_conflicts"])
        batch_swaps = []
        batch_ok_paths = []
        batch_creation_times = {}
        flush_count += 1
        # Persist the (in-memory-merged) cache to disk only periodically, or
        # unconditionally on the final flush of the run -- not on every
        # flush (D103). An unexpected crash between persists loses at most
        # this many flushes' worth of creation-time merges, which cost
        # nothing to redo: they come from files already on disk, not a
        # re-fetch.
        if creation_times_cache is not None and (
                final or flush_count % CREATION_TIMES_PERSIST_EVERY_FLUSHES == 0):
            _save_creation_times_cache(data_dir, creation_times_cache)
            if args.creation_times_only:
                for p in pending_creation_times_markers:
                    p.with_name(p.name + CREATION_TIMES_MARKER_SUFFIX).write_text("ok\n")
                pending_creation_times_markers = []

    schema_mismatch_hit = False
    for file_path in all_files:
        try:
            swaps = load_file(
                file_path,
                creation_times=None if args.no_creation_times else batch_creation_times,
            )
        except SchemaMismatch as e:
            print(f"SCHEMA MISMATCH, stopping (fix the adapter before "
                  f"continuing, don't skip past an unverified shape "
                  f"change): {e}", file=sys.stderr)
            # Flush whatever was already loaded before this file -- a file
            # already successfully loaded this run should not be thrown away
            # just because a later file failed. `final=True` so the
            # creation-times cache is persisted now, not left for a flush
            # count that will never come.
            _flush_now(final=True)
            schema_mismatch_hit = True
            break

        batch_swaps.extend(swaps)
        batch_ok_paths.append(file_path)
        total_swaps += len(swaps)
        total_mints.update(s.mint for s in swaps)
        total_files += 1
        print(f"[{total_files}/{n_total}] {file_path.relative_to(raw_dir)}: "
              f"{len(swaps)} swaps  (running total: {total_swaps} swaps, "
              f"{len(total_mints)} distinct mints)")

        if len(batch_ok_paths) >= args.batch_files or len(batch_swaps) >= args.batch_max_swaps:
            _flush_now()

    if not schema_mismatch_hit:
        _flush_now(final=True)  # whatever's left, and persist the cache for sure

    if schema_mismatch_hit:
        return 1

    print()
    print("=" * 78)
    if args.creation_times_only:
        print(f"Done. files read this run: {total_files}  "
              f"(swaps/mints below are from this run's reads, NOT written to "
              f"the Store -- --creation-times-only, D104)  "
              f"swaps seen: {total_swaps}  distinct mints: {len(total_mints)}")
    else:
        print(f"Done. files processed this run: {total_files}  "
              f"swaps written: {total_swaps}  distinct mints: {len(total_mints)}")
    if args.creation_times_only:
        print("Corroboration check: skipped (--creation-times-only, D104) -- "
              "these files' swaps were already corroborated and written to "
              "the Store when they were first ingested; this run only "
              "extracted creation timestamps from them.")
    elif corroboration["checked"] == 0:
        print("Corroboration check: 0 overlapping signatures found against "
              "already-Stored Helius/Bitquery data -- the side-convention "
              "assumption in tape/sources/pumpfundata.py's module docstring "
              "is STILL UNVERIFIED. Expected if this is mostly new calendar "
              "coverage with little overlap against the existing narrow "
              "corpus; worth deliberately checking an hour that DOES overlap "
              "an already-backfilled mint if this stays at 0.")
    else:
        print(f"Corroboration check (real overlapping sig's against other "
              f"sources): {corroboration['checked']} checked, "
              f"side agree {corroboration['side_agree']}/{corroboration['checked']}, "
              f"amount agree {corroboration['amount_agree']}/{corroboration['checked']}")
        if corroboration["side_disagree"] or corroboration["amount_disagree"]:
            print("  DISAGREEMENTS FOUND -- do not trust this adapter's output "
                  "until these are understood:")
            for d in corroboration["disagreements"][:20]:
                print(f"    {d}")
        else:
            print("  No disagreements -- this is real evidence (not an "
                  "assumption) that pumpfundata's side convention and amount "
                  "scaling match this project's existing trusted data.")
    print()
    if args.no_creation_times:
        print("Real creation times: skipped (--no-creation-times) -- "
              "scripts/information_audit.py's / scripts/paper_trade_replay.py's "
              "real-creation-time gate will still exclude every mint this run "
              "added unless fetch_real_creation_times.py covers them separately.")
    else:
        print(f"Real creation times (D101): {total_creation_times_added} new "
              f"mint(s) added to data/real_creation_times.json from this "
              f"run's 'create' rows -- unblocks scripts/paper_trade_replay.py's "
              f"real-creation-time gate for that many mints.")
        if creation_time_conflicts:
            print(f"  {len(creation_time_conflicts)} CONFLICT(S) with an "
                  f"already-recorded value -- kept the existing value, did "
                  f"NOT overwrite with pumpfundata's. Worth understanding "
                  f"before trusting either source blindly:")
            for c in creation_time_conflicts[:20]:
                print(f"    {c}")
    print()
    print("Next: train on the combined corpus --")
    print(r"  python scripts\paper_trade_replay.py --data data "
          r"--model-in data\online_policy_state.json "
          r"--model-out data\online_policy_state.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
