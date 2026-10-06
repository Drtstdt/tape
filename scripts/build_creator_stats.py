#!/usr/bin/env python3
"""D144: creator history for the live 'creator_farm' rail: per creator wallet,
launches and graduations in the whole stored history (pumpfundata events).

    python scripts\\build_creator_stats.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape import pf_store as ps  # noqa: E402


def main() -> int:
    ev = ps.load_events(r"E:\tape_store_v2")
    cr = ev[ev["event_type"] == "create"].drop_duplicates("mint")[["mint", "creator"]]
    grads = set(ev.loc[ev["event_type"] == "bonding_complete", "mint"])
    cr["grad"] = cr["mint"].isin(grads)
    st = cr.groupby("creator").agg(launches=("mint", "size"), grads=("grad", "sum")).reset_index()
    out = Path(r"E:\tape_research\creator_stats.parquet")
    st.to_parquet(out, index=False)
    print(f"{len(st):,} creators; {int((st['launches'] >= 20).sum()):,} with >= 20 launches, of which "
          f"{int(((st['launches'] >= 20) & (st['grads'] == 0)).sum()):,} never graduated -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
