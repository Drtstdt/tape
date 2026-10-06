#!/usr/bin/env python3
"""PROOF OF CONCEPT, not production code -- same status as the other poc_*
scripts in this directory.

Question this answers: `poc_pumpfun_api_discovery.py`'s cross-check found a
~43.7 HOUR gap between pump.fun's own `created_timestamp` for one already-
backfilled mint and this project's `created_ts_ms` (== `swaps[0].ts_ms`,
the first swap OUR OWN Bitquery-realtime-based discovery happened to see --
tape/scripts/information_audit.py::build_one and TokenState both use this
as the mint's "creation" time, including as the `age_ms` FEATURE). One data
point could be a fluke. This script checks it against every mint already in
the local Store: if the gap is systematic, `age_ms` has been measuring
"time since our own discovery pipeline noticed this token", not real token
age -- and, separately, the TRUE creation-time spread across our existing
152-mint universe might already be much wider than the ~3.3h window
`information_audit.py` computed from `first_ts_ms` (D53/D54), which would
mean the chronological-split problem has a free fix (use real
`created_timestamp`) with NO new backfill needed at all.

Writes nothing to the Store. Read-only against pump.fun's frontend API
(same unverified-until-now source poc_pumpfun_api_discovery.py checked)
and the local Store.

    python scripts/poc_real_creation_times.py --data data
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import numpy as np

from tape.store import Store

BASE = "https://frontend-api-v3.pump.fun"

# pump.fun's own launch is public knowledge (early Jan 2024) -- any
# `created_timestamp` before this is not a real bonding-curve creation time,
# whatever it is (stale metadata, a reused/migrated record, an API bug).
# Not guessed: this is a sanity FLOOR, not a claim about what the field
# means when it passes it -- values after this cutoff aren't thereby proven
# correct, just not provably impossible.
PUMPFUN_LAUNCH_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args()

    store = Store(args.data)
    where = []
    if args.since:
        where.append(f"dt >= '{args.since}'")
    if args.until:
        where.append(f"dt <= '{args.until}'")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    df = store.sql(f"""
        SELECT mint, min(ts_ms) AS first_ts_ms, count(*) AS n
        FROM swaps {clause}
        GROUP BY mint ORDER BY first_ts_ms
    """)
    mints = list(zip(df["mint"].tolist(), df["first_ts_ms"].tolist()))[: args.limit]
    print(f"checking {len(mints)} mints from the local Store against pump.fun's "
          f"real created_timestamp\n")

    rows = []
    errors = 0
    for mint, our_first_ts in mints:
        try:
            resp = httpx.get(f"{BASE}/coins/{mint}", timeout=15.0,
                             headers={"accept": "application/json"})
            if resp.status_code != 200:
                errors += 1
                continue
            data = resp.json()
            real_created = data.get("created_timestamp")
            if not isinstance(real_created, (int, float)):
                errors += 1
                continue
            gap_s = (our_first_ts - real_created) / 1000.0
            rows.append((mint, our_first_ts, real_created, gap_s))
        except Exception:
            errors += 1
        time.sleep(0.15)   # polite, unofficial API -- no documented rate limit

    print(f"resolved {len(rows)}/{len(mints)}  (errors/misses: {errors})\n")
    if not rows:
        print("nothing resolved -- can't say anything yet.")
        return 1

    gaps_all = np.array([r[3] for r in rows])
    real_created_all = np.array([r[2] for r in rows])
    our_first_all = np.array([r[1] for r in rows])

    implausible = real_created_all < PUMPFUN_LAUNCH_MS
    n_implausible = int(implausible.sum())
    print("=" * 78)
    print("SANITY FILTER")
    print("=" * 78)
    print(f"  implausible (real created_timestamp before pump.fun's own launch, "
          f"2024-01-01): {n_implausible}/{len(rows)}")
    if n_implausible:
        bad = [r for r, imp in zip(rows, implausible) if imp]
        for mint, our_ts, real_ts, _ in bad[:5]:
            print(f"    {mint}  real_created={real_ts}  our_first_ts={our_ts}")
        if n_implausible > 5:
            print(f"    ... and {n_implausible - 5} more")

    plausible = ~implausible
    gaps = gaps_all[plausible]
    real_created = real_created_all[plausible]
    our_first = our_first_all[plausible]
    print(f"\n  plausible rows kept for the stats below: {len(gaps)}/{len(rows)}")

    if len(gaps) == 0:
        print("\nNothing plausible survived the filter -- this data source can't be")
        print("trusted as-is; stopping here rather than reporting stats on nothing.")
        return 1

    print("\n" + "=" * 78)
    print("GAP (plausible subset only): our_first_ts_ms - real_created_timestamp (seconds)")
    print("=" * 78)
    for p in (0, 10, 25, 50, 75, 90, 100):
        print(f"  p{p:3d}: {np.percentile(gaps, p):12.1f}s  "
              f"({np.percentile(gaps, p) / 3600:6.2f}h)")
    frac_over_1h = float((np.abs(gaps) > 3600).mean())
    print(f"\n  fraction with |gap| > 1h: {frac_over_1h:.3f}")
    print(f"  fraction with gap > 0 (we saw it AFTER its real creation): "
          f"{float((gaps > 0).mean()):.3f}")

    print("\n" + "=" * 78)
    print("TRUE CREATION-TIME SPREAD (plausible subset) vs. OUR first_ts_ms SPREAD")
    print("=" * 78)
    our_span_h = (our_first.max() - our_first.min()) / 3_600_000
    real_span_h = (real_created.max() - real_created.min()) / 3_600_000
    print(f"  our first_ts_ms span:           {our_span_h:8.2f}h  "
          f"[{int(our_first.min())}, {int(our_first.max())}]")
    print(f"  real created_timestamp span:    {real_span_h:8.2f}h  "
          f"[{int(real_created.min())}, {int(real_created.max())}]")

    print("\nInterpretation:")
    print("  - If the gap is consistently near 0: our created_ts_ms IS real creation time,")
    print("    age_ms is fine, and the 3.3h window is real (not a discovery artifact).")
    print("  - If most gaps are large and positive (we see tokens well AFTER they were")
    print("    really created): our 'age_ms' feature has been measuring discovery lag, not")
    print("    real age -- worth re-deriving it from pump.fun's created_timestamp instead.")
    print("  - If real_span_h >> our_span_h: the true universe already spans much more time")
    print("    than 3.3h, and rebuilding chronological_universe() off real created_timestamp")
    print("    (instead of first_ts_ms) could fix the split with ZERO new backfill.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
