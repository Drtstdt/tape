#!/usr/bin/env python3
"""D118: build the mint-clustered swaps cache (see tape/swaps_cache.py).
Read-only on data/swaps. Run once; rebuild whenever data/swaps grows.

    python scripts\\build_swaps_cache.py --data data
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.swaps_cache import build_swaps_cache  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default=r"E:\tape_cache\swaps_by_mint")
    ap.add_argument("--temp-dir", default=r"E:\duckdb_tmp_cache")
    ap.add_argument("--memory-limit", default="4GB")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--row-group-size", type=int, default=25000)
    ap.add_argument("--verify-sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--accept-diffs", action="store_true",
                    help="move the cache into place even if the sampled "
                         "source-vs-cache comparison shows differences")
    a = ap.parse_args()
    build_swaps_cache(a.data, a.out, temp_dir=a.temp_dir, memory_limit=a.memory_limit,
                      threads=a.threads, row_group_size=a.row_group_size,
                      verify_sample=a.verify_sample, seed=a.seed,
                      accept_diffs=a.accept_diffs, log=lambda m: print(m, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
