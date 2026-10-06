#!/usr/bin/env python3
"""Enrichment utility -- NOT part of Stage 1's model-free audit gate itself.
A one-time (or periodically re-run) annotation step that looks up each Store
mint's REAL creation time from pump.fun's own (unofficial) frontend API and
caches it to a local JSON file that `scripts/information_audit.py` reads (via
`--real-creation-file`, default `<--data>/real_creation_times.json`) to filter
its universe.

Why this exists (D54, D55, docs/DECISIONS.md): this project's `created_ts_ms`
(== the first swap OUR OWN discovery pipeline happened to see, used
throughout as if it were token creation time -- including as the `age_ms`
FEATURE) is NOT real creation time. Bitquery's `discover()` surfaces ANY
mint with recent trade activity in its `dataset: realtime` window, not
specifically new launches. A live check against 152 already-backfilled
mints found: 100% had `real_created < our_first_ts_ms` (never the reverse),
median gap ~27-31h, and a real, non-outlier-driven tail reaching MONTHS to
~2.5 years -- meaning a meaningful fraction of the "discovered" universe are
old survivor tokens that merely traded again recently, not new launches.
Mixing those into a gate meant to test early-life signal in NEW launches is
a confound (a different population, and our TokenState replay only sees a
partial, non-early slice of an old survivor's life), not more data.

pump.fun's `/coins/{mint}` endpoint is UNOFFICIAL and unverified beyond what
this project has checked live: in one run 98/152 resolved, 54 errored/were
missing (network flakiness vs. genuinely delisted/banned tokens -- not yet
distinguished), and 1/98 returned a `created_timestamp` before pump.fun's
own 2024-01-01 launch (a real, live-confirmed data-quality issue in the
source, not this script). Every mint gets one of three cache statuses:
"ok" (plausible, trusted), "implausible" (before PUMPFUN_LAUNCH_MS -- kept
in the cache so a re-run doesn't keep re-fetching it, but
`information_audit.py` treats it the same as missing), or "error" (network/
HTTP failure -- retried on the next run unless --no-retry-errors). Nothing
here is guessed silently; a status is always recorded, never assumed "ok".

Incremental and idempotent: re-running only fetches mints not already in the
cache (plus any cached as "error", unless --no-retry-errors). Safe to run
again after every backfill.

    python scripts/fetch_real_creation_times.py --data data
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.store import Store

BASE = "https://frontend-api-v3.pump.fun"

# See scripts/poc_real_creation_times.py for how this floor was chosen and
# confirmed live to actually catch a real bad record, not a guess.
PUMPFUN_LAUNCH_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z


def fetch_one(mint: str) -> dict:
    """Never raises -- always returns a cache record with an explicit status."""
    now_ms = int(time.time() * 1000)
    try:
        resp = httpx.get(f"{BASE}/coins/{mint}", timeout=15.0,
                         headers={"accept": "application/json"})
    except Exception as e:
        return {"status": "error", "real_created_ts_ms": None,
                "error": str(e), "fetched_at_ms": now_ms}
    if resp.status_code != 200:
        return {"status": "error", "real_created_ts_ms": None,
                "error": f"HTTP {resp.status_code}", "fetched_at_ms": now_ms}
    try:
        data = resp.json()
        real_created = data.get("created_timestamp")
    except Exception as e:
        return {"status": "error", "real_created_ts_ms": None,
                "error": f"bad JSON: {e}", "fetched_at_ms": now_ms}
    if not isinstance(real_created, (int, float)):
        return {"status": "error", "real_created_ts_ms": None,
                "error": "no created_timestamp field", "fetched_at_ms": now_ms}
    if real_created < PUMPFUN_LAUNCH_MS:
        return {"status": "implausible", "real_created_ts_ms": int(real_created),
                "fetched_at_ms": now_ms}
    return {"status": "ok", "real_created_ts_ms": int(real_created), "fetched_at_ms": now_ms}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default=None,
                     help="cache path (default: <--data>/real_creation_times.json)")
    ap.add_argument("--limit", type=int, default=None, help="cap how many NEW mints to fetch this run")
    ap.add_argument("--no-retry-errors", action="store_true",
                     help="don't re-attempt mints cached as 'error' from a previous run")
    ap.add_argument("--sleep", type=float, default=0.15,
                     help="delay between requests -- polite default for an unofficial, "
                          "undocumented-rate-limit API")
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else Path(args.data) / "real_creation_times.json"
    cache: dict = {}
    if out_path.exists():
        with open(out_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"loaded existing cache: {len(cache)} mints already recorded")

    store = Store(args.data)
    all_mints = store.sql("SELECT DISTINCT mint FROM swaps ORDER BY mint")["mint"].tolist()
    print(f"store has {len(all_mints)} distinct mints total")

    to_fetch = []
    for m in all_mints:
        rec = cache.get(m)
        if rec is None:
            to_fetch.append(m)
        elif rec.get("status") == "error" and not args.no_retry_errors:
            to_fetch.append(m)
    if args.limit is not None:
        to_fetch = to_fetch[: args.limit]
    print(f"fetching {len(to_fetch)} mint(s) this run "
          f"({'retrying errors' if not args.no_retry_errors else 'not retrying errors'})\n")

    counts = {"ok": 0, "implausible": 0, "error": 0}
    for i, mint in enumerate(to_fetch, 1):
        rec = fetch_one(mint)
        cache[mint] = rec
        counts[rec["status"]] += 1
        if i % 25 == 0 or i == len(to_fetch):
            print(f"  {i}/{len(to_fetch)}  ok={counts['ok']} "
                  f"implausible={counts['implausible']} error={counts['error']}")
        # Write incrementally so a killed/interrupted run doesn't lose progress --
        # this API's real rate limit is undocumented, unlike Bitquery/Helius.
        if i % 25 == 0:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        time.sleep(args.sleep)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cache, f)

    print(f"\nwrote {len(cache)} total cached mint(s) to {out_path}")
    print(f"this run: ok={counts['ok']}  implausible={counts['implausible']}  error={counts['error']}")
    print("\nRun `python scripts/information_audit.py ...` now -- it will pick this cache up "
          f"automatically from {out_path} and filter out old survivor tokens (D55).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
