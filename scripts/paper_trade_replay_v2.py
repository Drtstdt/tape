#!/usr/bin/env python3
"""D117 (docs/DECISIONS.md) -- parallel + fast-universe version of
`paper_trade_replay.py`. SAME decisions, SAME ledger/checkpoint formats.

What is parallel and what is not:

  * PARALLEL (worker processes): fetch swaps, sanity filters, bars, features,
    first rails-passing bar, triple-barrier label, PnL -- all of it is a pure
    function of one mint's tape (`paper_trade_replay.prepare_mint`), no policy
    state involved.
  * SEQUENTIAL (this process only): `apply_decision` -- both OnlinePolicy
    instances decide() then update() on each prepared decision point IN
    CHRONOLOGICAL ORDER. Online learning is order-dependent by design (D84);
    `ProcessPoolExecutor.map` returns results in INPUT order, so the policy
    sees exactly the sequence the single-process script would give it.

Also uses StoreV2's file-by-file universe scan (no whole-corpus GROUP BY, the
D80/D115 spill/OOM path) and the D116 chunked fetch (one corpus scan per
chunk of mints instead of one per mint). Source Parquet is read-only.

    python scripts\\paper_trade_replay_v2.py --data data --limit 100000 --workers 5 ^
        --model-out data\\online_policy_replay_v2.json --ledger-out data\\paper_trades_replay_v2.csv

Use NEW --model-out / --ledger-out for a clean evaluation run (the ledger is
append-only and D89 skips mints already decided in it).
"""

from __future__ import annotations

import argparse
import csv
import gc
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.costs import CostModel
from tape.features import TokenState
from tape.labels import BarrierConfig
from tape.online_policy import OnlinePolicy, PaperRails
from tape.store import Store
from tape.store_v2 import StoreV2
from tape.swaps_cache import (check_cache_fresh, commit_preflight, fetch_swaps,
                              open_cache_connection, parse_gb)

from paper_trade_replay import (  # noqa: E402
    LEDGER_COLUMNS,
    apply_decision,
    mints_already_decided,
    prepare_mint,
)
from info_audit_v2 import (  # noqa: E402
    CHUNK_MINTS,
    SWAPS_CACHE_DEFAULT,
    DEFAULT_WORKERS,
    MIN_SWAPS_PER_TOKEN,
    UNIVERSE_MAX_TEMP_SIZE,
    UNIVERSE_MEMORY_LIMIT,
    UNIVERSE_TEMP_DIR,
    WORKER_MAX_TEMP_SIZE,
    WORKER_MEMORY_LIMIT,
    WORKER_TEMP_ROOT,
    WORKER_THREADS,
    _swaps_for_mints,
    filter_by_real_creation,
    load_real_creation_cache,
)

# ---------------------------------------------------------------------------
# Worker side (module-level: Windows spawn pickles these by name)
# ---------------------------------------------------------------------------

_w_store: Optional[Store] = None
_w_cfg: Optional[BarrierConfig] = None
_w_rails: Optional[PaperRails] = None
_w_cost: Optional[CostModel] = None
_w_bar_fraction: float = 0.01
_w_nominal_size: float = 1.0
_w_cache_con = None
_w_cache_path: Optional[str] = None


def _init_worker(data_dir: str, upper: float, lower: float, horizon_ms: int,
                 bar_fraction: float, nominal_size: float, memory_limit: str,
                 cache_path: Optional[str] = None) -> None:
    global _w_store, _w_cfg, _w_rails, _w_cost, _w_bar_fraction, _w_nominal_size
    global _w_cache_con, _w_cache_path
    if cache_path:
        # D118: mint-clustered cache; no Store / corpus view needed.
        _w_cache_con = open_cache_connection(
            memory_limit, WORKER_THREADS,
            os.path.join(WORKER_TEMP_ROOT, f"cache_worker_{os.getpid()}"))
        _w_cache_path = cache_path
    else:
        _w_store = Store(data_dir, read_only=True)
        con = _w_store.con
        con.execute(f"SET threads = {WORKER_THREADS}")
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute("SET preserve_insertion_order = false")
        con.execute("SET enable_progress_bar = false")
        con.execute(f"SET max_temp_directory_size = '{WORKER_MAX_TEMP_SIZE}'")
    _w_cfg = BarrierConfig(upper, lower, horizon_ms)
    _w_rails = PaperRails()
    _w_cost = CostModel()
    _w_bar_fraction = bar_fraction
    _w_nominal_size = nominal_size


def _prepare_chunk(mints: List[str]) -> Tuple[List[Tuple[str, Optional[Dict]]], Counter]:
    """One DuckDB query for the chunk, then prepare_mint() per mint. Results
    in input order. Failures are re-raised with context, never converted into
    a per-mint 'skipped' (that would silently drop the biggest tapes)."""
    try:
        fetched = (fetch_swaps(_w_cache_con, _w_cache_path, mints)
                   if _w_cache_con is not None else _swaps_for_mints(_w_store, mints))
        reasons: Counter = Counter()
        out = []
        for m in mints:
            prep = prepare_mint(m, fetched.pop(m), _w_cfg, _w_bar_fraction, _w_rails,
                                _w_cost, _w_nominal_size, reasons=reasons)
            out.append((m, prep))
        return out, reasons
    except Exception as e:  # noqa: BLE001
        rss = "n/a"
        try:
            import psutil
            rss = f"{psutil.Process().memory_info().rss / 1e9:.2f} GB"
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(
            f"worker pid={os.getpid()} failed on chunk of {len(mints)} mint(s) "
            f"starting {mints[0]!r}: {type(e).__name__}: {e} (worker rss={rss})") from e


def iter_prepared(mints: List[str], args, cfg: BarrierConfig, store_main, cache_path=None):
    """Yield (mint, prep_or_None, chunk_reasons_or_None) in INPUT order."""
    if args.workers <= 1:
        rails, cost = PaperRails(), CostModel()
        for m in mints:
            reasons: Counter = Counter()
            prep = prepare_mint(m, store_main.iter_swaps(m), cfg, args.bar_fraction,
                                rails, cost, args.nominal_size, reasons=reasons)
            yield m, prep, reasons
        return

    import concurrent.futures
    chunk = max(1, int(args.chunk_mints))
    chunks = [mints[k:k + chunk] for k in range(0, len(mints), chunk)]
    print(f"  [parallel] {args.workers} worker(s), {len(chunks)} chunk(s) of up to "
          f"{chunk} mint(s), worker memory_limit={args.worker_memory}, "
          f"temp={WORKER_TEMP_ROOT}", flush=True)
    with concurrent.futures.ProcessPoolExecutor(
            max_workers=args.workers, initializer=_init_worker,
            initargs=(args.data, args.upper, args.lower, cfg.horizon_ms,
                      args.bar_fraction, args.nominal_size, args.worker_memory,
                      cache_path)) as ex:
        for pairs, reasons in ex.map(_prepare_chunk, chunks, chunksize=1):
            for j, (m, prep) in enumerate(pairs):
                yield m, prep, (reasons if j == 0 else None)


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--real-creation-file", default=None)
    ap.add_argument("--max-observed-age-hours", type=float, default=6.0)
    ap.add_argument("--no-age-filter", action="store_true")
    ap.add_argument("--upper", type=float, default=1.6)
    ap.add_argument("--lower", type=float, default=-0.30)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--bar-fraction", type=float, default=0.01)
    ap.add_argument("--nominal-size", type=float, default=1.0)
    ap.add_argument("--epsilon-start", type=float, default=0.90)
    ap.add_argument("--epsilon-floor", type=float, default=0.15)
    ap.add_argument("--epsilon-decay-scale", type=float, default=60.0)
    ap.add_argument("--learning-rate", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model-out", default=None)
    ap.add_argument("--model-in", default=None)
    ap.add_argument("--ledger-out", default=None)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--chunk-mints", type=int, default=CHUNK_MINTS)
    ap.add_argument("--worker-memory", default=WORKER_MEMORY_LIMIT)
    ap.add_argument("--swaps-cache", default=None,
                    help=f"mint-clustered swaps parquet (D118). Default: {SWAPS_CACHE_DEFAULT} if present")
    ap.add_argument("--no-swaps-cache", action="store_true")
    ap.add_argument("--allow-stale-cache", action="store_true")
    args = ap.parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")

    os.environ.setdefault("TAPE_DUCKDB_TMP", WORKER_TEMP_ROOT)
    print(f"[replay-v2] data={args.data!r} limit={args.limit} workers={args.workers} "
          f"-- starting (source data read-only)", flush=True)
    commit_preflight(args.workers * parse_gb(args.worker_memory) + 4.0,
                     f"{args.workers} workers + main process")

    cfg = BarrierConfig(args.upper, args.lower, int(args.horizon_min * 60_000))

    store = StoreV2(args.data, read_only=True, temp_dir=UNIVERSE_TEMP_DIR,
                    memory_limit=UNIVERSE_MEMORY_LIMIT, max_temp_size=UNIVERSE_MAX_TEMP_SIZE)
    universe = store.chronological_universe(args.since, args.until)
    requested = len(universe)
    eligible_by_swaps = [(m, ts) for m, ts, n in universe if n >= MIN_SWAPS_PER_TOKEN]
    n_eligible_by_swaps = len(eligible_by_swaps)

    real_creation_path = Path(args.real_creation_file) if args.real_creation_file \
        else Path(args.data) / "real_creation_times.json"
    excluded_too_old = excluded_no_real_creation = 0
    if not args.no_age_filter and real_creation_path.exists():
        cache = load_real_creation_cache(real_creation_path)
        eligible, excluded_too_old, excluded_no_real_creation = filter_by_real_creation(
            eligible_by_swaps, cache, args.max_observed_age_hours)
        del cache
    else:
        eligible = eligible_by_swaps
    del universe, eligible_by_swaps
    gc.collect()

    all_mints = [m for m, _ in eligible[: args.limit]]
    ledger_path = Path(args.ledger_out) if args.ledger_out else Path(args.data) / "paper_trades.csv"
    already_decided = mints_already_decided(ledger_path, mode="replay")
    mints = [m for m in all_mints if m not in already_decided]

    print("=" * 78)
    print("PAPER TRADE REPLAY v2 (D117) -- not a claim of edge.")
    print("=" * 78)
    print(f"  requested: {requested}  eligible-by-swaps(raw count): {n_eligible_by_swaps}  "
          f"excluded(too old): {excluded_too_old}  excluded(no data): {excluded_no_real_creation}  "
          f"already decided (D89): {len(all_mints) - len(mints)}  considered: {len(mints)}")
    print(f"  exit rule (fixed, v1): upper={args.upper}x lower={args.lower*100:.0f}% "
          f"horizon={args.horizon_min:.0f}min")
    print(f"  exploration: epsilon {args.epsilon_start:.2f} -> {args.epsilon_floor:.2f} "
          f"(decay scale {args.epsilon_decay_scale:.0f} decisions)", flush=True)

    feature_names = list(TokenState("probe").features().keys())
    if args.model_in:
        online = OnlinePolicy.load(Path(args.model_in))
        print(f"  resumed online policy from {args.model_in} "
              f"(n_decisions={online.n_decisions}, n_updates={online.n_updates})")
    else:
        online = OnlinePolicy(feature_names, upper_multiple=args.upper, lower_pct=args.lower,
                              learning_rate=args.learning_rate, epsilon_start=args.epsilon_start,
                              epsilon_floor=args.epsilon_floor,
                              epsilon_decay_scale=args.epsilon_decay_scale, seed=args.seed)
    random_baseline = OnlinePolicy(feature_names, upper_multiple=args.upper, lower_pct=args.lower,
                                   epsilon_start=1.0, epsilon_floor=1.0, seed=args.seed + 1)

    cache_path = None
    if not args.no_swaps_cache:
        cp = args.swaps_cache or SWAPS_CACHE_DEFAULT
        if Path(cp).exists():
            if not args.allow_stale_cache:
                check_cache_fresh(cp, args.data)
            cache_path = cp
            print(f"  swaps cache: {cp} (fingerprint matches data/swaps)", flush=True)
        elif args.swaps_cache:
            raise FileNotFoundError(args.swaps_cache)
        else:
            print(f"  NOTE: no swaps cache at {cp} -- slow path. Build once: "
                  f"python scripts\\build_swaps_cache.py --data {args.data}", flush=True)

    total = len(mints)
    t0 = last = time.time()
    all_rows: List[Dict] = []
    reason_counter: Counter = Counter()
    n_decisions = 0

    for i, (mint, prep, chunk_reasons) in enumerate(iter_prepared(mints, args, cfg, store, cache_path), start=1):
        if chunk_reasons:
            reason_counter.update(chunk_reasons)
        if prep is not None:
            all_rows.extend(apply_decision(prep, mint, online, random_baseline, "replay"))
            n_decisions += 1
        now = time.time()
        if now - last >= 5.0 or i == total:
            el = now - t0
            rate = i / el if el > 0 else 0.0
            eta = (total - i) / rate if rate > 0 else float("nan")
            print(f"  [replay-v2] {i}/{total} mints ({i / total:.1%})  elapsed={el:.0f}s  "
                  f"ETA~{eta:.0f}s  decision_points={n_decisions}  "
                  f"online_decisions={online.n_decisions}", flush=True)
            last = now

    n_entered_online = sum(1 for r in all_rows if r["policy"] == "online" and r["action"] == "enter")
    n_entered_random = sum(1 for r in all_rows if r["policy"] == "random" and r["action"] == "enter")
    print(f"\n  tokens considered: {total}  decision points reached: {n_decisions}  "
          f"no-decision: {total - n_decisions}")
    if reason_counter:
        print("  rejection reasons (see docs/DECISIONS.md D85):")
        for reason, count in reason_counter.most_common():
            print(f"    {reason}: {count}")
    print(f"  online policy entered: {n_entered_online}/{n_decisions}   "
          f"random baseline entered: {n_entered_random}/{n_decisions}")
    print(f"  online policy after replay: n_decisions={online.n_decisions} "
          f"n_updates={online.n_updates} epsilon_now={online.epsilon:.3f}")

    write_header = not ledger_path.exists()
    with open(ledger_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
        if write_header:
            writer.writeheader()
        for row in all_rows:
            writer.writerow(row)
    print(f"\n  appended {len(all_rows)} row(s) to {ledger_path}")

    model_out = Path(args.model_out) if args.model_out else Path(args.data) / "online_policy_state.json"
    online.save(model_out)
    print(f"  saved online policy checkpoint to {model_out}")
    print("\nRun scripts\\paper_trading_report.py next -- this script does not judge results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
