#!/usr/bin/env python3
"""tape-one bot CLI -- one entry point, five subcommands.

    python scripts/run_bot.py doctor
    python scripts/run_bot.py train   [--since 2026-02-08 --limit 2000 ...]
    python scripts/run_bot.py backtest [--limit 2000 --models models/bot ...]
    python scripts/run_bot.py live    [--poll-s 15 --max-mints 200 ...]
    python scripts/run_bot.py report  [--ledger-dir data/bot]

`live` is PAPER by default. Real money needs three explicit gates:
--live --keypair <path> --acknowledge.

The subcommands are the module mains in tape/bot/; this file only routes.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "train": "tape.bot.train",
    "backtest": "tape.bot.backtest",
    "live": "tape.bot.live",
    "report": "tape.bot.report",
}


def doctor() -> int:
    print("== tape-one doctor ==")
    ok = True

    def check(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'OK' if good else 'MISSING'}] {name:<28} {detail}")
        ok = ok and good

    py = sys.executable
    check("venv python", py and "venv" in py.lower(), py)

    data = ROOT / "data"
    check("data/", data.is_dir(), str(data))
    swaps = data / "swaps"
    n_files = len(list(swaps.rglob("*.parquet"))) if swaps.is_dir() else 0
    check("data/swaps parquet", n_files > 0, f"{n_files} files")

    import importlib.util
    for mod in ("pandas", "pyarrow", "duckdb", "numpy", "lightgbm",
                "sklearn", "httpx", "yaml"):
        check(f"module {mod}", importlib.util.find_spec(mod) is not None)

    key = os.environ.get("BITQUERY_API_KEY") or ""
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("BITQUERY_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    check("BITQUERY_API_KEY", bool(key), "required for live/backfill")

    cache = Path("E:/tape_cache/swaps_by_mint")
    check("swaps cache E:/tape_cache/swaps_by_mint",
          (cache / "meta.json").exists(),
          "fast mint fetches; build with scripts/build_swaps_cache.py")

    spec = ROOT / "config" / "bot.yaml"
    check("config/bot.yaml", spec.exists(), str(spec))

    print("\n  " + ("all checks passed" if ok else "some checks failed"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=list(MODULES) + ["doctor"])
    args, rest = ap.parse_known_args()

    if args.command == "doctor":
        return doctor()

    cmd = [sys.executable, "-m", MODULES[args.command], *rest]
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
