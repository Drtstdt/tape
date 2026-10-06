"""Replay the store through the engine -- the backtester IS the bot
(docs/PLAN.md Sec 2.2). No second code path exists for the backtest.

Chronological universe, never outcome-selected: mints are ordered by first
observed trade and only filtered on point-in-time facts (min swaps before
the window, creation time). One engine, one spec, one ledger format -- the
same CSVs the live bot writes.

Known limitation, stated honestly: mints are replayed sequentially and each
position is sized against the account's capital without modelling
cross-token capital lockup, so concurrent drawdown across positions is not
modelled. Sizes are small fractions of capital by construction, and the
daily loss cap operates on the calendar day of each trade, which IS
modelled. This is a per-token strategy simulator, not a portfolio
risk-engine -- the live bot has the same engine but real concurrency.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..model import ModelArtifact
from ..schema import CanonicalSwap
from .engine import Engine
from .report import report_ledger
from .spec import BotSpec, load_spec


def run_backtest(spec: BotSpec, mints: List[str], first_ts: Dict[str, int],
                 swaps_loader: Callable[[str], List[CanonicalSwap]],
                 models: Optional[Dict[str, ModelArtifact]] = None,
                 base_dir: str | Path = "data",
                 ledger_dir: Optional[str | Path] = None,
                 log=print) -> Engine:
    from ..sanity import filter_implausible_swaps, filter_post_migration_swaps

    engine = Engine(spec, models=models, autocorrect=spec.autocorrect.enabled,
                    base_dir=base_dir, ledger_dir=ledger_dir)
    t0 = time.time()
    for i, mint in enumerate(mints, start=1):
        swaps = swaps_loader(mint)
        # same corpus discipline as the research pipeline: drop implausible
        # prices and everything from the first PumpSwap-touching swap onward
        swaps, _ = filter_implausible_swaps(swaps)
        swaps, _ = filter_post_migration_swaps(swaps)
        engine.begin_mint(mint, created_ts_ms=first_ts.get(mint))
        for swap in swaps:
            engine.on_swap(swap)
        engine.end_tape(mint)
        if i % 50 == 0 or i == len(mints):
            el = time.time() - t0
            rate = i / el if el > 0 else 0.0
            eta = (len(mints) - i) / rate if rate > 0 else float("nan")
            log(f"  [backtest] {i}/{len(mints)} mints "
                f"({i / len(mints):.0%}) {el:.0f}s elapsed, "
                f"ETA~{eta:.0f}s  entered={engine.n_entered} "
                f"closed={engine.n_closed} realized={engine.total_realized:+.4f}",
                flush=True)
    engine.save()
    return engine


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--cache", default="E:\\tape_cache\\swaps_by_mint")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--min-swaps", type=int, default=30)
    ap.add_argument("--spec", default="config/bot.yaml")
    ap.add_argument("--models", default="models/bot",
                    help="dir with per-band artifact.pkl; omit = rule-only mode")
    ap.add_argument("--ledger-dir", default="data/bot")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args()

    from ..swaps_cache import check_cache_fresh, fetch_swaps, open_cache_connection
    from .universe import cache_universe

    spec: BotSpec = load_spec(args.spec)
    loader: Callable[[str], List[CanonicalSwap]]
    try:
        check_cache_fresh(args.cache, args.data)
        mints, first_ts = cache_universe(args.cache, args.since, args.until,
                                         min_swaps=args.min_swaps)
        mints = mints[: args.limit]
        con = open_cache_connection()
        loader = lambda m: fetch_swaps(con, args.cache, [m])[m]
        print(f"[backtest] universe: {len(mints)} mints, chronological, "
              f"swaps from cache (since={args.since} until={args.until})")
    except Exception as e:  # noqa: BLE001
        print(f"[backtest] cache unusable ({e}); using store per-mint queries")
        from ..store import Store
        store = Store(args.data, read_only=True)
        mints = store.mints(since=args.since, until=args.until,
                            min_swaps=args.min_swaps)
        first_ts = store.sql(
            "SELECT mint, min(ts_ms) t0 FROM swaps GROUP BY mint"
        ).set_index("mint")["t0"].to_dict()
        mints.sort(key=lambda m: first_ts.get(m, 0))
        mints = mints[: args.limit]
        loader = lambda m: list(store.iter_swaps(m))

    # models, when present and passed their gates
    models: Dict[str, ModelArtifact] = {}
    mdir = Path(args.models)
    if mdir.is_dir():
        for band in spec.bands:
            p = mdir / band.name / "artifact.pkl"
            if p.exists():
                art = ModelArtifact.load(p)
                if art.meta.get("gate", {}).get("passed", True) is False:
                    print(f"[backtest] WARNING: {band.name} artifact FAILED its "
                          f"fold gate -- refusing to trade it (model=None for "
                          f"this band).")
                    continue
                models[band.name] = art
                print(f"[backtest] loaded {band.name} model "
                      f"(n_train={art.meta.get('n_train')}, "
                      f"status={art.meta.get('status')})")
    if not models:
        print("[backtest] no models -- rule-only mode; every entry will be "
              "flat-sized and logged as 'no_model_flat_size'.")

    engine = run_backtest(spec, mints, first_ts, loader, models=models,
                          base_dir=args.data, ledger_dir=args.ledger_dir)
    print("\n" + "=" * 78)
    print("BACKTEST SUMMARY")
    print("=" * 78)
    for k, v in engine.summary().items():
        print(f"  {k}: {v}")
    if not args.no_report:
        report_ledger(Path(args.ledger_dir))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
