#!/usr/bin/env python3
"""PROOF OF CONCEPT, not production code -- same status as poc_idl_classify.py
/ poc_gtfa_reach.py.

Question this answers: pump.fun's own (unofficial, reverse-engineered --
there is no official public docs page for it) frontend API has a `/coins`
listing endpoint that a third-party spec (github.com/BankkRoll/pumpfun-apis)
and an MCP tool wrapper both document as supporting `sort=created_timestamp`
with `offset`/`limit` pagination -- if real, this would give free, no-Solana-
decoding discovery of token creation times, which is exactly this project's
current gap (D54, docs/DECISIONS.md): gTFA gives deep REACH but discovering
NEW mints spread across a wide time range still means decoding raw program
traffic. This has never been called from this project before, so nothing
about it (field names, real depth, rate limits, whether it needs auth/headers
this environment doesn't have) is trusted -- this script only checks.

Cross-validates against ground truth this project already has: an already-
backfilled mint from the local Store (real first-swap-ts, independently
measured) is looked up via this API's per-coin endpoint, if the listing
checks out, to see whether the two `created_timestamp`s actually agree
before trusting this as a real discovery source.

    python scripts/poc_pumpfun_api_discovery.py --data data
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

BASE = "https://frontend-api-v3.pump.fun"


def get(path: str, params: dict) -> httpx.Response:
    return httpx.get(f"{BASE}{path}", params=params, timeout=20.0,
                     headers={"accept": "application/json"})


def dump_first(label: str, resp: httpx.Response) -> None:
    print(f"\n-- {label} -- HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"   body (first 500 chars): {resp.text[:500]!r}")
        return
    try:
        data = resp.json()
    except Exception as e:
        print(f"   NOT JSON: {e}; body[:200]={resp.text[:200]!r}")
        return
    items = data if isinstance(data, list) else data.get("coins") or data.get("data") or []
    print(f"   type={type(data).__name__}  n_items={len(items) if isinstance(items, list) else '?'}")
    if isinstance(items, list) and items:
        first = items[0]
        print(f"   first item keys: {sorted(first.keys()) if isinstance(first, dict) else type(first)}")
        for k in ("mint", "address", "created_timestamp", "createdTimestamp", "name", "symbol"):
            if isinstance(first, dict) and k in first:
                print(f"     {k} = {first[k]!r}")
    elif not items:
        print(f"   raw top-level keys: {list(data.keys()) if isinstance(data, dict) else data}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--cross-check-mint", default=None,
                     help="a mint already in the local Store; if omitted, one is picked "
                          "automatically from Store.mints()")
    args = ap.parse_args()

    # -- 1. Sanity: newest coins, DESC. Should look like right now. ----------
    resp = get("/coins", {"offset": 0, "limit": 5, "sort": "created_timestamp",
                          "order": "DESC", "includeNsfw": "true"})
    dump_first("DESC (newest first), offset=0", resp)
    time.sleep(0.5)

    # -- 2. Oldest reachable via ASC at offset=0. -----------------------------
    resp = get("/coins", {"offset": 0, "limit": 5, "sort": "created_timestamp",
                          "order": "ASC", "includeNsfw": "true"})
    dump_first("ASC (oldest first), offset=0", resp)
    time.sleep(0.5)

    # -- 3. Bisect depth: does ASC + growing offset keep paging back in time, -
    #    or cap out / error / start repeating? ------------------------------
    for offset in (100, 1_000, 10_000, 50_000, 100_000):
        resp = get("/coins", {"offset": offset, "limit": 1, "sort": "created_timestamp",
                              "order": "ASC", "includeNsfw": "true"})
        dump_first(f"ASC, offset={offset}", resp)
        time.sleep(0.5)

    # -- 3b. First real run (2026-09-22) found offset=1000 still returns data
    #    but offset=10000 comes back EMPTY (both ASC and, worth checking,
    #    maybe DESC too) -- classic signature of an Elasticsearch/OpenSearch
    #    `from+size <= 10000` window cap, not a real depth wall. Bisect the
    #    exact boundary, on BOTH sort directions (a shared search backend
    #    would cap both the same way; nothing here assumes that without
    #    checking). ------------------------------------------------------
    print("\n" + "=" * 78)
    print("BISECTING THE OFFSET CAP (both directions)")
    print("=" * 78)
    for order in ("ASC", "DESC"):
        print(f"\n-- order={order} --")
        for offset in (2_000, 4_000, 6_000, 8_000, 9_000, 9_500, 9_900, 9_990, 9_999):
            resp = get("/coins", {"offset": offset, "limit": 1, "sort": "created_timestamp",
                                  "order": order, "includeNsfw": "true"})
            n = None
            ts = None
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    items = data if isinstance(data, list) else []
                    n = len(items)
                    ts = items[0].get("created_timestamp") if items else None
                except Exception:
                    pass
            print(f"   offset={offset:6d}  HTTP={resp.status_code}  n_items={n}  created_timestamp={ts}")
            time.sleep(0.3)

    # -- 3c. The number that actually answers "does the offset cap alone cover
    #    24-48h?": under DESC (most recent first), what's the created_timestamp
    #    of the OLDEST item still reachable near the cap (offset ~9999)? The
    #    gap between that and right-now is the real, usable recent-history
    #    window without needing anything past the cap. ----------------------
    resp = get("/coins", {"offset": 9_999, "limit": 1, "sort": "created_timestamp",
                          "order": "DESC", "includeNsfw": "true"})
    print("\n" + "=" * 78)
    print("RECENT-HISTORY WINDOW SIZE (DESC, at the offset cap)")
    print("=" * 78)
    if resp.status_code == 200:
        try:
            items = resp.json()
            if items:
                oldest_ts_ms = items[0]["created_timestamp"]
                now_ms = int(time.time() * 1000)
                hours = (now_ms - oldest_ts_ms) / 3_600_000
                print(f"  oldest token still reachable at offset=9999 (DESC): "
                      f"created_timestamp={oldest_ts_ms}")
                print(f"  that is {hours:.1f} hours before now -- this is the actual usable")
                print(f"  recent-history window from plain offset pagination alone.")
            else:
                print("  empty at offset=9999 too -- the real cap is lower; see the bisection above.")
        except Exception as e:
            print(f"  could not parse: {e}")
    else:
        print(f"  HTTP {resp.status_code}: {resp.text[:300]!r}")

    # -- 4. Cross-check against a mint this project already backfilled, ------
    #    independently, from real swaps (not from this API). -----------------
    mint = args.cross_check_mint
    known_first_ts_ms = None
    if mint is None:
        try:
            from tape.store import Store
            store = Store(args.data)
            mints = store.mints(min_swaps=50)
            if mints:
                mint = mints[0]
                df = store.sql(f"SELECT min(ts_ms) AS t FROM swaps WHERE mint = '{mint}'")
                known_first_ts_ms = int(df["t"][0])
        except Exception as e:
            print(f"\n(could not auto-pick a mint from the local Store: {e})")

    if mint:
        print(f"\n-- Cross-check: {mint} --")
        if known_first_ts_ms is not None:
            print(f"   this project's own first-swap-ts_ms for this mint: {known_first_ts_ms} "
                  f"({known_first_ts_ms / 1000:.0f}s epoch)")
        resp = get(f"/coins/{mint}", {})
        print(f"   GET /coins/{{mint}} -> HTTP {resp.status_code}")
        if resp.status_code == 200:
            try:
                data = resp.json()
                print(f"   keys: {sorted(data.keys()) if isinstance(data, dict) else type(data)}")
                for k in ("created_timestamp", "createdTimestamp"):
                    if isinstance(data, dict) and k in data:
                        v = data[k]
                        print(f"     {k} = {v!r}")
                        if known_first_ts_ms is not None:
                            # created_timestamp could be ms or s -- print both comparisons,
                            # don't assume which, per this project's own discipline.
                            print(f"     vs. our first-swap-ts_ms {known_first_ts_ms}: "
                                  f"diff_if_both_ms={v - known_first_ts_ms if isinstance(v, (int, float)) else '?'}  "
                                  f"diff_if_v_is_seconds={v * 1000 - known_first_ts_ms if isinstance(v, (int, float)) else '?'}")
            except Exception as e:
                print(f"   NOT JSON: {e}")
        else:
            print(f"   body[:300]: {resp.text[:300]!r}")
    else:
        print("\n(no mint available to cross-check -- pass --cross-check-mint or ensure "
              "--data points at a Store with data)")

    print("\nInterpretation:")
    print("  - If DESC offset=0 items have created_timestamp near right-now: endpoint is live/real.")
    print("  - If ASC items' created_timestamp keeps getting OLDER as offset grows (not stuck/")
    print("    erroring/repeating): this can page arbitrarily deep, for free, no Solana decoding.")
    print("  - If the cross-check timestamps agree (after fixing ms-vs-s if needed): this source")
    print("    is trustworthy for discovery. If they disagree or the endpoint errors/blocks: don't")
    print("    build a pipeline on it without figuring out why first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
