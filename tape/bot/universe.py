"""Universe selection from the swaps cache.

The cache (E:/tape_cache, tape/swaps_cache.py) is mint-sorted Parquet, so
"which mints traded in [since, until] with at least N swaps" is a fast
columnar scan -- far cheaper than the Store's dedup-window over the whole
corpus. Selection is on POINT-IN-TIME facts only (a swap inside the
window), never on outcomes (docs/PLAN.md's standing rule).

Parity note: the Store's `mints()` counts DEDUPED rows; the cache counts
raw rows. The two universes can differ by a handful of mints at the
margins; both are honest point-in-time filters.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple


def _ms(s: str) -> int:
    return int(dt.datetime.strptime(s, "%Y-%m-%d").timestamp() * 1000)


def cache_universe(cache_dir: str | Path, since: str | None = None,
                   until: str | None = None, min_swaps: int = 1
                   ) -> Tuple[List[str], Dict[str, int]]:
    """(mints chronological by first trade, {mint: first_ts_ms})."""
    import duckdb

    cache_dir = Path(cache_dir)
    where = []
    if since:
        where.append(f"ts_ms >= {_ms(since)}")
    if until:
        # inclusive of the whole last day
        where.append(f"ts_ms < {_ms(until) + 86_400_000}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    con = duckdb.connect(":memory:")
    con.execute("SET enable_progress_bar = false")
    sql = f"""
        SELECT mint, count(*) AS n, min(ts_ms) AS t0
        FROM read_parquet('{(cache_dir / 'bucket=*.parquet').as_posix()}')
        {clause}
        GROUP BY mint HAVING n >= {int(min_swaps)}
    """
    df = con.execute(sql).df()
    con.close()
    order = df.sort_values("t0")["mint"].tolist()
    first_ts = dict(zip(df["mint"], df["t0"]))
    return order, first_ts
