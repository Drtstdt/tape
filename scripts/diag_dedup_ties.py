#!/usr/bin/env python3
"""D133: why does F's replay differ from the research set for some tokens?

Hypothesis (from code, not yet measured): the dedup window keeps ONE row per
(sig, mint, side, round(base_amount, 12)) ordered only by `source`; when the
same swap sits in two hourly files (boundary rows, D99) the two copies can
differ (e.g. timestamp by a few seconds) and DuckDB keeps an arbitrary one --
so two fetches of the same token can disagree. Checked on the tokens listed in
trials/family_F/consistency_diff.csv vs an equal number of agreeing tokens.

    python scripts\\diag_dedup_ties.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape import pf_store as ps  # noqa: E402


def dup_stats(con, store, mints):
    import pandas as pd
    by_b = {}
    for m in mints:
        by_b.setdefault(ps.bucket_of(m), []).append(m)
    parts = []
    for b, ms in by_b.items():
        files = "[" + ", ".join(ps._q(f.as_posix()) for f in ps.bucket_files(store, b)) + "]"
        con.register("w", pd.DataFrame({"mint": ms}))
        parts.append(con.execute(
            f"SELECT mint, sig, side, round(base_amount, 12) AS ba, count(*) AS n, "
            f"count(DISTINCT ts_ms) AS n_ts, count(DISTINCT real_quote_reserve_after) AS n_real, "
            f"count(DISTINCT fee_sol) AS n_fee, count(DISTINCT slot) AS n_slot, max(ts_ms) - min(ts_ms) AS ts_span "
            f"FROM read_parquet({files}, union_by_name=true) WHERE mint IN (SELECT mint FROM w) "
            f"GROUP BY 1, 2, 3, 4").df())
        con.unregister("w")
    d = pd.concat(parts, ignore_index=True)
    dup = d[d["n"] > 1]
    return {"tokens": len(set(mints)), "keys": len(d), "dup_keys": len(dup),
            "tokens_with_dups": int(dup["mint"].nunique()),
            "dup_keys_ts_differ": int((dup["n_ts"] > 1).sum()),
            "dup_keys_real_differ": int((dup["n_real"] > 1).sum()),
            "dup_keys_fee_differ": int((dup["n_fee"] > 1).sum()),
            "dup_keys_slot_differ": int((dup["n_slot"] > 1).sum()),
            "ts_span_max_s": float(dup["ts_span"].max() / 1000) if len(dup) else 0.0}


def main() -> int:
    import pandas as pd
    store = r"E:\tape_store_v2"
    fdir = Path(r"E:\tape_research\trials\family_F")
    bad = pd.read_csv(fdir / "consistency_diff.csv")["mint"].tolist()
    import pyarrow.parquet as pq
    parts = sorted(Path(r"E:\tape_research\sets_v2\discovery").glob("part-b*.parquet"))
    allm = pd.concat([pq.read_table(p, columns=["mint", "K"]).to_pandas() for p in parts])
    allm = sorted(set(allm.loc[allm["K"] == 10, "mint"]) - set(bad))
    ctrl = random.Random(0).sample(allm, min(len(bad), 2000, len(allm)))
    con = ps.open_connection(memory_limit="4GB", threads=4, temp_dir=str(Path(store) / "_duckdb_tmp"))
    out = []
    for name, ms in (("differing", bad[:2000]), ("control", ctrl)):
        st = dup_stats(con, store, ms)
        out.append(f"{name:10s} {st}")
        print(out[-1], flush=True)
    (fdir / "diag_dedup.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"-> {fdir / 'diag_dedup.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
