#!/usr/bin/env python3
"""D96 follow-up (docs/DECISIONS.md) -- now that /range has given REAL numbers
(237 calendar days, 2026-02-08 to 2026-10-02, 5658 pump_fun files available),
this builds the actual scattered-purchase plan from D93's addendum and, with
--execute, carries it out.

THE NUMBERS CHANGED THE RECOMMENDATION: at 237 real days and a 3000-credit
budget, "1 credit/day" (D93 addendum's original framing) only spends 237 of
3000 credits -- it trivially covers every single calendar day and leaves
~2760 credits unspent. The real question is not "can we reach every day"
(yes, easily) but "what's the best use of the rest of the bdget".

D98 (docs/DECISIONS.md) UPDATE: the first real run under-spent on purpose --
`budget // days` (here 3000 // 237 = 12) floors the division and leaves the
237-day remainder (156 credits) completely unspent. Fixed: `distribute_hours_per_day()`
spreads that remainder evenly across the days (classic "N extra items over
M slots" distribution, same idea as Bresenham's line algorithm) so some days
get 13 hours instead of 12 and the FULL budget is targeted, not just the
floor. Each day's hours are still spread EVENLY across the 24-hour clock
(not one contiguous block) -- so the corpus gets both calendar-date
diversity (this project's standing weakness, D56/D61/D62/D83) AND intraday-
hour diversity (spreading across the clock avoids silently only ever
sampling, say, US-daytime hours, which would bias the corpus toward
whatever behavior happens to cluster in those hours -- a real but not
previously measured risk, flagged here as a judgment call, not an
established project requirement).

SAFE BY DEFAULT: this is real vendor credits. Without --execute, it only
PRINTS the plan (dates, hours, total file count, total credit cost) -- it
calls `/range` (free) to refresh the real numbers but never calls
`/download`. With --execute, it actually downloads, and still: respects the
documented 30 req/min rate limit (throttled to ~28/min with margin), skips
any (date, hour) file already saved locally so a re-run or a resumed run
after an interruption never re-spends a credit on the same file, and honours
--limit to cap how many files a single run will fetch (use a small --limit
first to spend a handful of credits and sanity-check the output before
committing the rest of the budget).

D98: a REAL first run found 404s on the first available day's earliest
hours (2026-02-08 hours 00/02/04) -- `/range`'s `start` date is apparently
not fully covered from hour 0. Handled already: a 404 is logged and the run
continues (it already did, correctly) -- this is not a bug to fix, just
confirms gaps can exist and the per-file error handling matters.

Downloads land in <raw-dir>/pump_fun/date=YYYY-MM-DD/hour=HH.parquet --
raw vendor files only, OUTSIDE the project's own data/ dir by default (D98:
moved off the D: drive onto F:\pumpfundata on the user's machine -- this is
real vendor data, not Store-managed state, and doesn't need to live next to
the Store or even on the same drive). This script does NOT parse these
files or touch the Store; that's tape/sources/pumpfundata.py (D98, now
built) plus scripts/ingest_pumpfundata.py (D98, now built) for the merge
step.

    python scripts/pumpfundata_fetch_plan.py                       # plan only
    python scripts/pumpfundata_fetch_plan.py --execute --limit 5   # spend 5 credits, sanity check
    python scripts/pumpfundata_fetch_plan.py --execute             # spend the rest of the plan
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.env import load_project_dotenv

BASE_URL = "https://api.pumpfundata.com"
EXCHANGE = "pump_fun"  # D94/D95: the only exchange this project buys
MIN_REQUEST_INTERVAL_S = 2.15  # 30 req/min documented limit -> ~27.9/min with margin


def fetch_range(api_key: str) -> dict:
    resp = httpx.get(f"{BASE_URL}/range", params={"exchange": EXCHANGE},
                      headers={"X-API-Key": api_key}, timeout=20.0)
    resp.raise_for_status()
    return resp.json()


def _parse_date(s: str) -> date:
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def distribute_hours_per_day(budget: int, days: int) -> List[int]:
    """`days`-long list of hours/day summing to as close to `budget` as
    possible (capped at 24/day), spreading the `budget % days` remainder
    evenly across the days rather than dumping it all on the first ones --
    standard even-distribution (Bresenham-line-style running-error) method.

    e.g. budget=3000, days=237 -> base=12, remainder=156: 156 of the 237
    days get 13 hours, the other 81 get 12, and the 13-hour days are spread
    roughly every ~1.5 days apart, not clustered at the start.
    """
    if days <= 0:
        return []
    base, remainder = divmod(budget, days)
    base = max(0, base)
    hours: List[int] = []
    acc = 0
    for _ in range(days):
        acc += remainder
        if acc >= days:
            acc -= days
            hours.append(min(24, base + 1))
        else:
            hours.append(min(24, base))
    return hours


def build_schedule(start: date, end: date, hours_per_day) -> List[Tuple[date, int]]:
    """Evenly spaced hours across the 24h clock for each day from `start` to
    `end` inclusive. `hours_per_day` is either a single int (same count every
    day, old behaviour, still used when --hours-per-day is passed explicitly)
    or a list with one entry per day (from `distribute_hours_per_day`, the
    default -- lets different days get different counts so the full budget,
    not just its floor division, gets targeted). Each day's own hour count is
    clamped to [1, 24]."""
    n_days = (end - start).days + 1
    if isinstance(hours_per_day, int):
        per_day = [hours_per_day] * n_days
    else:
        per_day = list(hours_per_day)
        if len(per_day) != n_days:
            raise ValueError(f"hours_per_day has {len(per_day)} entries, expected {n_days}")

    schedule: List[Tuple[date, int]] = []
    d = start
    for n in per_day:
        n = max(1, min(24, n))
        hours = sorted({round(i * 24 / n) % 24 for i in range(n)})
        for h in hours:
            schedule.append((d, h))
        d += timedelta(days=1)
    return schedule


def local_path(raw_dir: Path, d: date, h: int) -> Path:
    return raw_dir / EXCHANGE / f"date={d.isoformat()}" / f"hour={h:02d}.parquet"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--raw-dir", default=r"F:\pumpfundata",
                     help="where raw vendor files land (D98: off the project's "
                          "own drive/data dir on purpose -- default F:\\pumpfundata)")
    ap.add_argument("--budget-credits", type=int, default=3000)
    ap.add_argument("--hours-per-day", type=int, default=None,
                     help="override the auto-computed, full-budget-targeting "
                          "per-day hour count with a FLAT number for every day "
                          "(old behaviour; leaves the remainder unspent, same "
                          "as before D98's distribute_hours_per_day fix)")
    ap.add_argument("--execute", action="store_true",
                     help="actually call /download and spend credits. "
                          "Without this flag, only prints the plan.")
    ap.add_argument("--limit", type=int, default=None,
                     help="cap the number of files fetched THIS run (with --execute)")
    args = ap.parse_args()

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("PUMPFUNDATA_API_KEY")
    if not api_key:
        print("PUMPFUNDATA_API_KEY not set. Put `PUMPFUNDATA_API_KEY=pfd_...` "
              "on its own line in .env, or pass --api-key.", file=sys.stderr)
        return 2

    print(f"Refreshing /range for {EXCHANGE} (free call)...")
    r = fetch_range(api_key)
    start, end = _parse_date(r["start"]), _parse_date(r["end"])
    span_days = (end - start).days + 1
    print(f"  real range: {start} to {end}  ({span_days} days, {r['files']} files available)")

    if args.hours_per_day is not None:
        hours_per_day = args.hours_per_day
        schedule = build_schedule(start, end, hours_per_day)
        print(f"\nPlan: FLAT {hours_per_day} hour(s)/day (explicit --hours-per-day), "
              f"spread evenly across the clock, over all {span_days} days = "
              f"{len(schedule)} files ({len(schedule)} credits if every one is "
              f"actually downloaded).")
    else:
        per_day = distribute_hours_per_day(args.budget_credits, span_days)
        schedule = build_schedule(start, end, per_day)
        lo, hi = min(per_day), max(per_day)
        hours_desc = f"{lo}" if lo == hi else f"{lo}-{hi} (mix, to use the full budget)"
        print(f"\nPlan: {hours_desc} hour(s)/day, spread evenly across the clock, "
              f"over all {span_days} days = {len(schedule)} files "
              f"({len(schedule)} credits if every one is actually downloaded) "
              f"-- D98: distributes the budget%days remainder across days "
              f"instead of leaving it unspent.")
    if len(schedule) > args.budget_credits:
        print(f"  WARNING: plan ({len(schedule)}) exceeds --budget-credits "
              f"({args.budget_credits}) -- lower --hours-per-day or raise the budget.")
    else:
        print(f"  {args.budget_credits - len(schedule)} credits left unspent at this plan size.")

    raw_dir = Path(args.raw_dir)
    already_have = [(d, h) for d, h in schedule if local_path(raw_dir, d, h).exists()]
    to_fetch = [(d, h) for d, h in schedule if not local_path(raw_dir, d, h).exists()]
    print(f"  raw files will be saved under: {raw_dir}")
    print(f"  already on disk: {len(already_have)}   still to fetch: {len(to_fetch)}")

    if not args.execute:
        print("\nDRY RUN -- no /download call made, no credits spent. "
              "Re-run with --execute to actually fetch (consider --limit N for "
              "a small first run).")
        if to_fetch[:5]:
            print("  first few that would be fetched:")
            for d, h in to_fetch[:5]:
                print(f"    {d} hour={h:02d}")
        return 0

    if args.limit is not None:
        to_fetch = to_fetch[: args.limit]
    print(f"\nEXECUTING -- about to spend up to {len(to_fetch)} credits. "
          f"Ctrl+C now to abort (5s)...")
    time.sleep(5)

    n_ok = 0
    n_err = 0
    for d, h in to_fetch:
        path = local_path(raw_dir, d, h)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = httpx.get(
                f"{BASE_URL}/download",
                params={"exchange": EXCHANGE, "date": d.isoformat(), "hour": f"{h:02d}"},
                headers={"X-API-Key": api_key},
                timeout=60.0,
            )
        except httpx.HTTPError as e:
            print(f"  ERROR {d} hour={h:02d}: network error: {e}")
            n_err += 1
            time.sleep(MIN_REQUEST_INTERVAL_S)
            continue
        if resp.status_code != 200:
            print(f"  ERROR {d} hour={h:02d}: HTTP {resp.status_code}: {resp.text[:200]}")
            n_err += 1
            if resp.status_code in (401, 403):
                print("  auth error -- stopping the run rather than burning "
                      "through the rest of the plan on a bad key.")
                break
        else:
            path.write_bytes(resp.content)
            n_ok += 1
            print(f"  OK {d} hour={h:02d} -> {path} ({len(resp.content)} bytes)  "
                  f"[{n_ok} fetched, {n_err} errors]")
        time.sleep(MIN_REQUEST_INTERVAL_S)

    print(f"\nDone. fetched={n_ok} errors={n_err}. Whether a failed request still "
          f"consumed a credit is NOT documented anywhere this project has checked "
          f"-- don't assume either way, check your actual pumpfundata account "
          f"balance after this run if errors > 0.")
    print("Raw files only -- nothing parsed, nothing written to the Store yet.")
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
