#!/usr/bin/env python3
"""D93/D93-addendum (docs/DECISIONS.md) follow-up, now that the user has a
real pumpfundata.com API key.

Calls the FREE `/range` endpoint (costs no credits -- confirmed in
pumpfundata.com/docs) to find out, before spending a single credit, what the
vendor's real available date range actually is for `pump_fun`. This is
exactly the check D93's addendum said to do first: the scattered-purchase
strategy (buy 1 hour from each of up to 3,000 DIFFERENT calendar days,
directly targeting this project's own standing calendar-diversity weakness,
D56/D61/D62/D83) only makes sense if the real range is wide enough to
scatter across -- don't assume the marketing page's historical-depth claim,
measure it.

Also reports `pump_amm` for completeness (the call is free either way), but
per this project's current scope decision (docs/DECISIONS.md D94/D95):
this project has no feature/model/rails code that can use PumpSwap data, and
D95 found real evidence that letting PumpSwap-phase data leak into the
bonding-curve pipeline actively loses money. The recommendation stays
100% pump_fun, 0% pump_amm, regardless of what /range reports for pump_amm.

    python scripts/check_pumpfundata_range.py
    python scripts/check_pumpfundata_range.py --budget-credits 3000
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.env import load_project_dotenv

BASE_URL = "https://api.pumpfundata.com"


def fetch_range(exchange: str, api_key: str) -> dict:
    """GET /range?exchange=<exchange> -- free, no credits spent (confirmed
    in pumpfundata.com/docs). Returns the parsed JSON on success, or
    {"__error__": "..."} on any HTTP or network failure -- never raises, so
    one exchange's failure doesn't stop the other from being checked."""
    try:
        resp = httpx.get(
            f"{BASE_URL}/range",
            params={"exchange": exchange},
            headers={"X-API-Key": api_key},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        return {"__error__": f"network error: {e}"}
    if resp.status_code != 200:
        return {"__error__": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    try:
        return resp.json()
    except ValueError:
        return {"__error__": f"non-JSON response: {resp.text[:300]}"}


def _parse_date(s: str) -> date:
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--budget-credits", type=int, default=3000,
                     help="credit budget to plan against, e.g. the $50 tier's "
                          "3000 credits (default 3000)")
    args = ap.parse_args()

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("PUMPFUNDATA_API_KEY")
    if not api_key:
        print("PUMPFUNDATA_API_KEY not set. Put `PUMPFUNDATA_API_KEY=pfd_...` "
              "on its own line in .env, or pass --api-key.", file=sys.stderr)
        return 2

    results = {}
    for exchange in ("pump_fun", "pump_amm"):
        results[exchange] = fetch_range(exchange, api_key)

    print("=" * 78)
    print("PUMPFUNDATA /range -- real available history (free call, no credits spent)")
    print("=" * 78)

    pump_fun = results["pump_fun"]
    for exchange, data in results.items():
        print(f"\n[{exchange}]")
        if "__error__" in data:
            print(f"  ERROR: {data['__error__']}")
            continue
        print(f"  start={data.get('start')}  end={data.get('end')}  "
              f"files={data.get('files')}")

    if "__error__" in pump_fun:
        print("\ncould not evaluate the purchase plan -- pump_fun /range call failed.")
        return 1

    start = _parse_date(pump_fun["start"])
    end = _parse_date(pump_fun["end"])
    span_days = (end - start).days + 1
    files_available = pump_fun.get("files")

    print()
    print("=" * 78)
    print(f"PURCHASE PLAN for pump_fun (this project's only recommended buy -- "
          f"see docs/DECISIONS.md D94/D95 for why pump_amm is out of scope)")
    print("=" * 78)
    print(f"  real range: {start} to {end} = {span_days} calendar days")
    print(f"  real files available across that range: {files_available}")
    print(f"  credit budget being planned against: {args.budget_credits}")

    if files_available is not None and args.budget_credits >= files_available:
        print(f"\n  budget ({args.budget_credits}) covers the ENTIRE available "
              f"pump_fun history ({files_available} files) -- just buy "
              f"everything, no scattering strategy needed.")
    else:
        # D93 addendum's scattering strategy: at most 1 credit/day across as
        # many DISTINCT calendar days as the budget allows, capped by how
        # many days actually exist in the range.
        days_coverable = min(args.budget_credits, span_days)
        pct_of_range = 100.0 * days_coverable / span_days
        print(f"\n  budget does NOT cover everything -- scattering strategy "
              f"(D93 addendum) applies:")
        print(f"    spend 1 credit per calendar day -> cover {days_coverable} "
              f"distinct days out of {span_days} ({pct_of_range:.1f}% of the "
              f"whole range), vs. {args.budget_credits} CONTIGUOUS hours "
              f"(~{args.budget_credits / 24:.1f} days) if spent as one block.")
        print(f"    scattering gets you ~{days_coverable / (args.budget_credits / 24):.0f}x "
              f"more distinct calendar days of coverage for the same credits.")

    print("\nThis call spent 0 credits. Nothing downloaded, nothing written to "
          "the Store. Decide the scatter schedule from these real numbers "
          "before spending anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())