#!/usr/bin/env python3
"""Read-only: rows / distinct mints per `source` in data/swaps (one column scanned)."""
import sys
import duckdb

data = sys.argv[1] if len(sys.argv) > 1 else "data"
c = duckdb.connect()
c.execute("SET threads=2")
c.execute("SET memory_limit='2GB'")
q = f"""
SELECT source, count(*) AS rows, count(DISTINCT mint) AS mints
FROM read_parquet('{data}/swaps/**/*.parquet', union_by_name=1)
GROUP BY source ORDER BY rows DESC
"""
print(c.execute(q).df().to_string(index=False))
