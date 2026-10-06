"""The live bot: Bitquery discovery + polling, driving the SAME engine the
backtester drives. The only difference between live and backtest is which
source hands swaps to `engine.on_swap` -- that is the whole design
(docs/PLAN.md Sec 2.2).

How "live" works without the (unverified) websocket: Bitquery's realtime
dataset retains ~9h of history per mint. The loop:

    every poll_s:
      discover()        new pump.fun/PumpSwap mints since the last window
      for each new mint: bootstrap its tape (historical() back a few hours)
      for each tracked mint: historical(since=last_ts) -> feed new swaps
      engine.sweep(now)  wall-clock exits, independent of the tape
      retire quiet mints, save ledgers periodically

Paper executor by default. Real trading needs --live --keypair --acknowledge
and even then prints a loud banner before the first fill. There is no
in-flight state persistence yet (v1): a restart re-bootstraps tapes from
Bitquery, which is safe -- the engine's no-lookahead construction means the
re-bootstrap reproduces the same decisions for the same tape.

    python -m tape.bot.live --poll-s 15 --max-mints 200
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Set

from ..costs import CostModel
from ..sanity import filter_implausible_swaps, filter_post_migration_swaps
from ..sources.bitquery import BitquerySource
from .engine import Engine
from .executors import LiveExecutor
from .spec import BotSpec, load_spec

BOOTSTRAP_BACK_MS = 2 * 3600_000
DISCOVER_OVERLAP_MS = 60_000


class LiveBot:
    def __init__(self, spec: BotSpec, engine: Engine, source: BitquerySource,
                 poll_s: float = 15.0, max_mints: int = 200,
                 retire_quiet_ms: int = 30 * 60_000,
                 log=print) -> None:
        self.spec = spec
        self.engine = engine
        self.source = source
        self.poll_s = poll_s
        self.max_mints = max_mints
        self.retire_quiet_ms = retire_quiet_ms
        self.log = log
        self.tracked: Dict[str, int] = {}   # mint -> last swap ts_ms
        self.seen_mints: Set[str] = set()
        self._sigs: Dict[str, Set[str]] = {}
        self.last_discover_ms = int(time.time() * 1000)
        self._polls = 0

    def _bootstrap(self, mint: str, now_ms: int) -> None:
        swaps = list(self.source.historical(
            mint, now_ms - BOOTSTRAP_BACK_MS, now_ms))
        swaps, _ = filter_implausible_swaps(swaps)
        swaps, _ = filter_post_migration_swaps(swaps)
        self.engine.begin_mint(mint, created_ts_ms=(
            swaps[0].ts_ms if swaps else now_ms))
        for s in swaps:
            self.engine.on_swap(s)
        self.tracked[mint] = swaps[-1].ts_ms if swaps else now_ms

    def _poll_mint(self, mint: str, now_ms: int) -> None:
        since = self.tracked.get(mint, now_ms - BOOTSTRAP_BACK_MS)
        swaps = list(self.source.historical(mint, since, now_ms))
        sigs = self._sigs.setdefault(mint, set())
        for s in swaps:
            if s.ts_ms <= self.tracked.get(mint, 0) or s.sig in sigs:
                continue
            sigs.add(s.sig)
            self.engine.on_swap(s)
            self.tracked[mint] = s.ts_ms
        if swaps:
            self.tracked[mint] = max(self.tracked.get(mint, 0),
                                     swaps[-1].ts_ms)

    def run(self, max_loops: Optional[int] = None) -> None:
        loops = 0
        self.log("LIVE BOT starting -- PAPER MODE. No transactions will be "
                 "submitted." if self.engine.executor.name == "paper" else
                 "LIVE BOT starting -- REAL MONEY MODE.")
        try:
            while max_loops is None or loops < max_loops:
                loops += 1
                now_ms = int(time.time() * 1000)
                self._polls += 1

                # discovery (point-in-time inclusion events, never outcomes)
                try:
                    for hit in self.source.discover(
                            self.last_discover_ms, now_ms):
                        mint = hit["mint"]
                        if mint in self.seen_mints:
                            continue
                        self.seen_mints.add(mint)
                        if len(self.tracked) < self.max_mints:
                            try:
                                self._bootstrap(mint, now_ms)
                                self.log(f"[live] +{mint} "
                                          f"({len(self.tracked)} tracked)")
                            except Exception as e:  # noqa: BLE001
                                self.log(f"[live] bootstrap failed for {mint}: {e}")
                except Exception as e:  # noqa: BLE001
                    self.log(f"[live] discover failed: {e}")
                self.last_discover_ms = now_ms - DISCOVER_OVERLAP_MS

                # poll tracked mints
                for mint in list(self.tracked):
                    try:
                        self._poll_mint(mint, now_ms)
                    except Exception as e:  # noqa: BLE001
                        self.log(f"[live] poll failed for {mint}: {e}")
                        continue
                    last = self.tracked.get(mint, 0)
                    if now_ms - last > self.retire_quiet_ms \
                            and mint not in self.engine.positions:
                        del self.tracked[mint]

                self.engine.sweep(now_ms)

                if self._polls % 20 == 0:
                    self.engine.save()
                    s = self.engine.summary()
                    self.log(f"[live] tracked={len(self.tracked)} "
                             f"entered={s['n_entered']} closed={s['n_closed']} "
                             f"open={s['n_open']} "
                             f"realized={s['total_realized_sol']:+.4f}")
                time.sleep(self.poll_s)
        except KeyboardInterrupt:
            self.log("\n[live] interrupted -- saving ledgers and stopping.")
        finally:
            self.engine.save()
            for k, v in self.engine.summary().items():
                self.log(f"  {k}: {v}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--spec", default="config/bot.yaml")
    ap.add_argument("--models", default="models/bot")
    ap.add_argument("--poll-s", type=float, default=15.0)
    ap.add_argument("--max-mints", type=int, default=200)
    ap.add_argument("--retire-quiet-min", type=float, default=30.0)
    ap.add_argument("--ledger-dir", default="data/bot")
    ap.add_argument("--dataset", default="realtime",
                    choices=("realtime", "archive"))
    ap.add_argument("--max-loops", type=int, default=None,
                    help="stop after N polls (for supervised test runs)")
    ap.add_argument("--live", action="store_true",
                    help="REAL MONEY gate 1 of 3")
    ap.add_argument("--keypair", default=None,
                    help="REAL MONEY gate 2 of 3: path to a JSON keypair")
    ap.add_argument("--acknowledge", action="store_true",
                    help="REAL MONEY gate 3 of 3: operator's explicit acceptance")
    args = ap.parse_args()

    spec: BotSpec = load_spec(args.spec)

    from ..model import ModelArtifact
    models: Dict[str, ModelArtifact] = {}
    mdir = Path(args.models)
    if mdir.is_dir():
        for band in spec.bands:
            p = mdir / band.name / "artifact.pkl"
            if p.exists():
                models[band.name] = ModelArtifact.load(p)

    if args.live or args.keypair or args.acknowledge:
        executor = LiveExecutor(
            CostModel(fee_pct=spec.fee_pct, fixed_cost_sol=spec.fixed_cost_sol,
                      extra_slippage_pct=spec.extra_slippage_pct),
            keypair_path=args.keypair, live=args.live, acknowledge=args.acknowledge)
        print("!!! REAL MONEY MODE: every entry will submit a transaction. !!!")
    else:
        executor = None

    engine = Engine(spec, models=models, executor=executor,
                    autocorrect=spec.autocorrect.enabled,
                    base_dir="data", ledger_dir=args.ledger_dir)
    source = BitquerySource(dataset=args.dataset)
    bot = LiveBot(spec, engine, source, poll_s=args.poll_s,
                  max_mints=args.max_mints,
                  retire_quiet_ms=int(args.retire_quiet_min * 60_000))
    return bot.run(max_loops=args.max_loops) or 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
