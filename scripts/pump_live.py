#!/usr/bin/env python3
"""D144: live PAPER bot for pump.fun (tape/pumplive). No transaction is ever sent.

  * new tokens + migrations: PumpPortal websocket (free)
  * trades of watched tokens: Helius logsSubscribe on each bonding curve
    (HELIUS_API_KEY from .env; ~2 credits per 0.1 MB)
  * rails at create (metadata/socials, copycat, serial creator, dev buy) and at
    decision (dev sold/holding, top-10, first-slot bundle, non-standard curve)
  * 4 entry rules x 36 exits = 144 paper configs on every eligible token
  * learner: champion = best lower confidence bound > 0, else ABSTAIN
  * everything recorded to E:\\tape_live\\YYYY-MM-DD\\HH.jsonl.gz

    python scripts\\pump_live.py --probe 120     (2-minute check of both feeds; prints what arrived)
    python scripts\\pump_live.py                 (run until Ctrl+C)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tape.env import load_project_dotenv  # noqa: E402
from tape.pumplive import feeds  # noqa: E402
from tape.pumplive.engine import Engine, Settings  # noqa: E402
from tape.pumplive.recorder import Recorder  # noqa: E402


def load_creator_stats(path, log):
    p = Path(path)
    if not p.exists():
        log(f"  creator stats: none ({p}) -- run scripts\\build_creator_stats.py for the farm rail")
        return {}
    import pandas as pd
    df = pd.read_parquet(p)
    log(f"  creator stats: {len(df):,} creators")
    return {r.creator: {"launches": int(r.launches), "grads": int(r.grads)} for r in df.itertuples(index=False)}


async def amain(a) -> int:
    def log(m):
        print(time.strftime("%H:%M:%S ") + m, flush=True)

    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        log("STOP: HELIUS_API_KEY missing in .env")
        return 2
    s = Settings.load(a.config)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rec = Recorder(a.record_dir, enabled=not a.no_record)
    stop = asyncio.Event()
    probe = {"pumpportal": 0, "helius": 0, "keys_pp": set(), "trades_decoded": 0}
    probe_f = open(out / "probe.jsonl", "w", encoding="utf-8") if a.probe else None

    def raw_sink(src, msg):
        if probe_f is None:
            return
        probe[src] += 1
        if src == "pumpportal":
            probe["keys_pp"].update(msg.keys())
        if probe[src] <= 50:
            probe_f.write(json.dumps({"src": src, "msg": msg}, default=str)[:4000] + "\n")

    hel = feeds.HeliusLogs(key, None, log, raw_sink if a.probe else None)
    engine = Engine(s, rec, hel.subscribe, hel.unsubscribe, load_creator_stats(a.creator_stats, log), log)
    hel.engine = engine
    mq: asyncio.Queue = asyncio.Queue()
    log(f"pump_live PAPER: {len(engine.configs)} configs, latency {s.latency_slots} slots, tip {s.tip_sol} SOL, "
        f"size {s.size_sol} SOL, max watched {s.max_watched}")
    tasks = [asyncio.create_task(feeds.pumpportal(engine, mq.put_nowait, log, stop, raw_sink if a.probe else None)),
             asyncio.create_task(hel.run(stop)),
             asyncio.create_task(feeds.clock(engine, rec, log, stop, 30 if a.probe else 60, out / "report.json"))]
    tasks += [asyncio.create_task(feeds.meta_worker(engine, mq, log)) for _ in range(8)]
    try:
        if a.probe:
            await asyncio.sleep(a.probe)
        elif a.minutes:
            await asyncio.sleep(a.minutes * 60)
        else:
            await stop.wait()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        rec.flush()
        rep = engine.report()
        (out / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
        if probe_f:
            probe_f.close()
            c = engine.counts
            log(f"PROBE: pumpportal messages {probe['pumpportal']}, creates {c['creates']}, "
                f"keys {sorted(probe['keys_pp'])[:25]}")
            log(f"PROBE: helius notifications {probe['helius']}, decoded trades {c['trades']}, "
                f"metadata ok {c['watched'] + c['rail_rejects']} (rails rejected {c['rail_rejects']}), "
                f"avg feed lag {rep['counts']['avg_feed_lag_s']} s -> raw samples in {out / 'probe.jsonl'}")
            if probe["helius"] and not c["trades"]:
                log("PROBE WARNING: logs arrived but no TradeEvent decoded -- send me probe.jsonl")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=str(ROOT / "config" / "pump_live.json"))
    ap.add_argument("--creator-stats", default=r"E:\tape_research\creator_stats.parquet")
    ap.add_argument("--record-dir", default=r"E:\tape_live")
    ap.add_argument("--out", default=str(ROOT / "data" / "pump_live"))
    ap.add_argument("--probe", type=int, default=0, help="seconds: check both feeds, print a summary, stop")
    ap.add_argument("--minutes", type=float, default=0, help="stop after N minutes (0 = until Ctrl+C)")
    ap.add_argument("--no-record", action="store_true")
    a = ap.parse_args()
    load_project_dotenv()
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(amain(a))
    except KeyboardInterrupt:
        print("stopped (Ctrl+C); report in data\\pump_live\\report.json")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
