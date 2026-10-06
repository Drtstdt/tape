"""Read the ledgers the engine writes and print the numbers that matter.

The doctrine (docs/ARCHITECTURE.md): the tally of WHY the bot did not act
is the most informative output of any run -- more than the PnL -- and it is
the first thing to read after a run. This module prints that tally first,
then the trade book, then cost sensitivity (an edge that dies under 2x
cost is a cost artefact, not an edge -- docs/PLAN.md Sec 9).
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import List, Optional


def read_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def report_ledger(ledger_dir: Path) -> dict:
    decisions = read_csv(Path(ledger_dir) / "decisions.csv")
    trades = read_csv(Path(ledger_dir) / "trades.csv")

    print("\n  DECISIONS (why the bot acted or did not)")
    n = len(decisions)
    print(f"    total decisions: {n}")
    if not n:
        return {}
    by_action = Counter(d["action"] for d in decisions)
    for action, count in by_action.most_common():
        print(f"    {action:<8} {count:>6}  ({count / n:.1%})")
    by_layer_reason = Counter(
        (d["layer"], d["reason"]) for d in decisions
        if d["action"] != "enter")
    print("    abstentions/rejections, by layer.reason:")
    for (layer, reason), count in by_layer_reason.most_common(25):
        print(f"      {layer}.{reason:<40} {count}")

    print("\n  TRADES")
    exits = [t for t in trades if t["side"] == "exit" and t["pnl_sol"] not in (None, "")]
    enters = [t for t in trades if t["side"] == "enter"]
    print(f"    entries: {len(enters)}   closed: {len(exits)}")
    if not exits:
        return {"decisions": n}
    pnls = [float(t["pnl_sol"]) for t in exits]
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    print(f"    realized PnL: {total:+.4f} SOL   win rate: {wins}/{len(pnls)} "
          f"({wins / len(pnls):.1%})   mean {total / len(pnls):+.4f}")

    by_reason = Counter(t["reason"] for t in exits)
    print("    exit reasons:")
    for reason, count in by_reason.most_common():
        print(f"      {reason:<35} {count}")

    # per-band book
    by_band: dict = {}
    for t in exits:
        band = next((d["band"] for d in decisions
                     if d["mint"] == t["mint"] and d["action"] == "enter"), "?")
        by_band.setdefault(band, []).append(float(t["pnl_sol"]))
    print("    per-band realized PnL:")
    for band, ps in sorted(by_band.items()):
        print(f"      {band:<12} n={len(ps):<5} total {sum(ps):+8.4f} "
              f"mean {sum(ps) / len(ps):+.4f}")

    # cost sensitivity: what if the proportional fee doubled?
    fee_extra = sum(float(t["fee_sol"]) for t in trades if t["fee_sol"])
    pnl_2x = total - fee_extra
    print("\n  COST SENSITIVITY (docs/PLAN.md Sec 9: an edge that dies under "
          "2x cost is a cost artefact)")
    print(f"    fees paid: {fee_extra:.4f} SOL")
    print(f"    PnL at 2x proportional fee: {pnl_2x:+.4f} SOL "
          f"({'SURVIVES' if pnl_2x > 0 else 'DIES'})")

    return {"decisions": n, "trades": len(exits), "pnl": total,
            "pnl_2x": pnl_2x, "win_rate": wins / len(pnls)}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ledger-dir", default="data/bot")
    args = ap.parse_args()
    report_ledger(Path(args.ledger_dir))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
