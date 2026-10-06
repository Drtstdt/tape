#!/usr/bin/env python3
"""LIVE feature/label monitor -- watch new pump.fun launches, live, using the
EXACT SAME bar-building (`tape/bars.py`), feature-computation
(`tape/features.py::TokenState`) and triple-barrier labelling
(`tape/labels.py`) code `information_audit.py` uses -- nothing reimplemented,
nothing new invented.

WHAT THIS IS NOT: a trading bot. `information_audit.py` measures whether
features carry information about the future excursion; it deliberately never
turns that into a buy/sell threshold (D53's whole design: "This is NOT a
claim that trading this feature is profitable"), and the one completed audit
run (D56) found `no_edge_found` (underpowered, since fixed -- D66/D67 are
still widening the universe as of this script's creation). There is
therefore no validated entry rule to simulate trades with. This script
invents none. It shows you, live, whether each freshly-launched token's
price actually crosses the audit's own +upper/-lower barrier first, and what
the audit's own features looked like at each bar close along the way -- so
you can watch the real signal (or its absence) happen, honestly, instead of
waiting for a batch run over already-collected history.

WHY BOUNDED CONCURRENCY: real launch volume is roughly 20-30/minute (see
docs/DECISIONS.md D66's discovery-run numbers) and the large majority never
clear MIN_SWAPS_PER_TOKEN (D66: ~17% in a 6h window) -- tracking every single
one live would flood the console and burn through Helius/Bitquery calls on
tokens the audit itself would exclude anyway. `--max-concurrent` (default 15)
caps how many launches are actively polled at once; the rest are counted as
"skipped (at capacity)", not silently dropped. Comprehensive, unbounded
coverage is what `scripts/discover_pumpfun_launches.py` +
`scripts/backfill_discovered_launches.py` are for -- this script is a live
window onto a SAMPLE of what those two are accumulating, for watching, not
for building the audit's actual universe.

KNOWN COST TRADEOFF: `HeliusSource.historical()` re-resolves the pool's
vault-owner address (one `getTokenLargestAccounts` + one `getAccountInfo`
call) on EVERY call, by design (D25) -- calling it once per poll per tracked
mint means that overhead recurs every poll, even when a mint has zero new
swaps. At the defaults (15 concurrent, 30s polls) that's a bounded, modest
RPC cost; not optimized further here since this is an observation tool, not
the production backfill path.

    python scripts/live_paper_monitor.py --data data
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.bars import BarBuilder, band_bar_threshold
from tape.env import load_project_dotenv
from tape.features import TokenState
from tape.labels import UP, DOWN, TIMEOUT, BarrierConfig, Label, triple_barrier
from tape.sources.bitquery import PUMPFUN_PROGRAM, _ms_to_bq_iso, _parse_bq_iso_ms
from tape.sources.helius import HeliusSource

MIN_SWAPS_PER_TOKEN = 50  # same corpus filter as information_audit.py

ENDPOINT = "https://streaming.bitquery.io/graphql"
BQ_MAX_RETRIES = 5
BQ_RETRY_BACKOFF_CAP_S = 30.0
PAGE_LIMIT = 1000

# ---------------------------------------------------------------------------
# Discovery -- local copy of discover_pumpfun_launches.py's CREATE_QUERY /
# extract_mint / parse_row, same reasoning as every other per-script local
# copy in this project (sources/adapters and scripts/ tools don't import each
# other -- see tape/sources/helius.py's own comment on this).
# ---------------------------------------------------------------------------

CREATE_QUERY = """
query CreateEvents($program: String!, $method: String!, $since: DateTime!, $until: DateTime!, $limit: Int!) {
  Solana(dataset: realtime) {
    Instructions(
      limit: {count: $limit}
      orderBy: {ascending: Block_Time}
      where: {
        Instruction: { Program: { Address: {is: $program}, Method: {is: $method} } }
        Block: { Time: {after: $since, before: $until} }
      }
    ) {
      Block { Time }
      Transaction { Signature }
      Instruction {
        Accounts { Address IsWritable }
        Program { Address Method Name AccountNames }
      }
    }
  }
}
"""


def _post_query(query: str, variables: dict, api_key: str) -> dict:
    for attempt in range(1, BQ_MAX_RETRIES + 1):
        resp = httpx.post(
            ENDPOINT,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30.0,
        )
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait_s = float(retry_after) if retry_after else min(2 ** attempt, BQ_RETRY_BACKOFF_CAP_S)
            print(f"  [bitquery] 429, waiting {wait_s:.0f}s (retry {attempt}/{BQ_MAX_RETRIES})",
                  file=sys.stderr)
            time.sleep(wait_s)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]!r}")
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Bitquery returned errors: {body['errors']}")
        return body["data"]
    raise RuntimeError(f"Still 429 after {BQ_MAX_RETRIES} retries.")


def extract_mint(accounts: List[dict], account_names: List[str]) -> Optional[str]:
    """Same exact-name, never-positional match as discover_pumpfun_launches.py
    (D58/D60/D67 -- confirmed live, repeatedly, to be correct even across the
    "mayhem" and extra-account create_v2 variants D67 found)."""
    if not accounts or not account_names:
        return None
    lowered = [str(n).lower() for n in account_names]
    if "mint" not in lowered:
        return None
    idx = lowered.index("mint")
    if idx >= len(accounts):
        return None
    entry = accounts[idx]
    if not isinstance(entry, dict):
        return None
    return entry.get("Address")


def parse_row(row: dict) -> Optional[dict]:
    bt = (row.get("Block") or {}).get("Time")
    sig = (row.get("Transaction") or {}).get("Signature")
    instr = row.get("Instruction") or {}
    accounts = instr.get("Accounts") or []
    program = instr.get("Program") or {}
    account_names = program.get("AccountNames") or []
    mint = extract_mint(accounts, account_names)
    ts_ms = _parse_bq_iso_ms(bt) if bt else None
    if mint is None or ts_ms is None or sig is None:
        return None
    return {"mint": mint, "ts_ms": ts_ms, "sig": sig}


def fetch_new_launches(since_ms: int, until_ms: int, api_key: str) -> Dict[str, int]:
    """{mint: earliest ts_ms} for every create_v2 event in [since_ms, until_ms].
    Paginated the same way discover_pumpfun_launches.py is, in case a poll
    window ends up wider than expected (a slow cycle, a restart)."""
    by_mint: Dict[str, int] = {}
    cur = since_ms
    while cur <= until_ms:
        data = _post_query(CREATE_QUERY, {
            "program": PUMPFUN_PROGRAM, "method": "create_v2",
            "since": _ms_to_bq_iso(cur), "until": _ms_to_bq_iso(until_ms),
            "limit": PAGE_LIMIT,
        }, api_key)
        rows = data["Solana"]["Instructions"]
        max_ts = cur
        for row in rows:
            parsed = parse_row(row)
            if parsed is None:
                continue
            max_ts = max(max_ts, parsed["ts_ms"])
            if parsed["mint"] not in by_mint or parsed["ts_ms"] < by_mint[parsed["mint"]]:
                by_mint[parsed["mint"]] = parsed["ts_ms"]
        if len(rows) < PAGE_LIMIT:
            break
        cur = max_ts
    return by_mint


# ---------------------------------------------------------------------------
# Per-mint live state
# ---------------------------------------------------------------------------

@dataclass
class TrackedMint:
    mint: str
    created_ts_ms: int
    last_fetched_ts_ms: int
    bar_builder: Optional[BarBuilder] = None
    state: Optional[TokenState] = None
    h: List[float] = field(default_factory=list)
    l: List[float] = field(default_factory=list)
    c: List[float] = field(default_factory=list)
    t: List[int] = field(default_factory=list)
    latest_feats: Optional[Dict[str, Optional[float]]] = None
    bar0_reported: bool = False
    error: Optional[str] = None
    total_swaps: int = 0


def poll_mint(source: HeliusSource, tm: TrackedMint, now_ms: int, bar_fraction: float) -> None:
    """Fetch new swaps since tm.last_fetched_ts_ms, feed them into the SAME
    BarBuilder/TokenState machinery build_one() (information_audit.py) uses.
    Mutates tm in place. Sets tm.error and stops polling that mint on a
    RuntimeError (e.g. D67's Token-2022 "not a Token mint" case) rather than
    retrying a call that will keep failing identically."""
    if tm.error is not None:
        return
    try:
        new_swaps = list(source.historical(tm.mint, tm.last_fetched_ts_ms, now_ms))
    except RuntimeError as e:
        tm.error = str(e)
        return
    if not new_swaps:
        tm.last_fetched_ts_ms = now_ms
        return
    tm.total_swaps += len(new_swaps)
    tm.last_fetched_ts_ms = max(s.ts_ms for s in new_swaps) + 1

    if tm.bar_builder is None:
        depth = next((s.quote_reserve_after for s in new_swaps if s.quote_reserve_after), None) or 1.0
        tm.bar_builder = BarBuilder("dollar", threshold=band_bar_threshold(depth, bar_fraction))
        tm.state = TokenState(tm.mint, created_ts_ms=tm.created_ts_ms)

    for s in new_swaps:
        bar = tm.bar_builder.push(s)
        if bar is None:
            continue
        tm.state.update(bar)
        tm.latest_feats = dict(tm.state.features())
        tm.h.append(bar.high)
        tm.l.append(bar.low)
        tm.c.append(bar.close)
        tm.t.append(bar.close_ts_ms)


def bar0_label(tm: TrackedMint, cfg: BarrierConfig) -> Optional[Label]:
    """The first bar's triple-barrier label (`tape/labels.py::triple_barrier`,
    called directly -- the SAME function `label_series`/`build_one()` use
    under the hood, just for a single, fixed index rather than a strided
    series), recomputed from whatever bars exist so far -- `truncated=True`
    until enough real time/bars have elapsed to resolve it, exactly like a
    batch run would see mid-tape."""
    if len(tm.c) < 2:
        return None
    return triple_barrier(tm.h, tm.l, tm.c, tm.t, 0, cfg, mint=tm.mint)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

_OUTCOME_NAME = {UP: "UP     (+upper hit)", DOWN: "DOWN   (-lower hit)", TIMEOUT: "TIMEOUT (neither, horizon elapsed)"}


def _short(mint: str) -> str:
    return mint[:4] + ".." + mint[-4:]


def _fmt_feat(feats: Optional[Dict[str, Optional[float]]]) -> str:
    if not feats:
        return "(no bars yet)"
    keys = ["netflow_10", "buysell_ratio_10", "ret_10"]
    parts = []
    for k in keys:
        v = feats.get(k)
        parts.append(f"{k}={v:+.3f}" if isinstance(v, (int, float)) else f"{k}=n/a")
    return " ".join(parts)


def print_status(tracked: Dict[str, TrackedMint], cfg: BarrierConfig, horizon_min: float,
                 session_outcomes: List[int], skipped_this_cycle: int, now_ms: int) -> None:
    ts_str = time.strftime("%H:%M:%S", time.localtime(now_ms / 1000))
    n_up = sum(1 for o in session_outcomes if o == UP)
    n_down = sum(1 for o in session_outcomes if o == DOWN)
    n_timeout = sum(1 for o in session_outcomes if o == TIMEOUT)
    print(f"\n[{ts_str}] tracking {len(tracked)} mint(s)  |  session bar0 outcomes so far: "
          f"{n_up} UP / {n_down} DOWN / {n_timeout} TIMEOUT  |  skipped this cycle "
          f"(at capacity): {skipped_this_cycle}")
    if not tracked:
        return
    print(f"{'mint':14s} {'age':>7s} {'bars':>5s} {'swaps':>6s}  {'latest features (audit-identical)':45s}  bar0 label")
    for mint, tm in sorted(tracked.items(), key=lambda kv: kv[1].created_ts_ms):
        age_min = (now_ms - tm.created_ts_ms) / 60_000
        if tm.error:
            status = f"ERROR: {tm.error[:60]}"
        else:
            lab = bar0_label(tm, cfg)
            if lab is None:
                status = "pending (not enough bars yet)"
            elif lab.truncated:
                status = f"pending (t+{age_min:.1f}/{horizon_min:.0f}min)"
            else:
                status = f"{_OUTCOME_NAME.get(lab.outcome, str(lab.outcome))} " \
                         f"at t+{(lab.t1_ms - tm.created_ts_ms) / 60_000:.1f}min, " \
                         f"mfe={lab.mfe_pct:+.1%} mae={lab.mae_pct:+.1%}"
        print(f"{_short(mint):14s} {age_min:6.1f}m {len(tm.c):5d} {tm.total_swaps:6d}  "
              f"{_fmt_feat(tm.latest_feats):45s}  {status}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data", help="unused for storage (this script is read-only "
                     "and touches no Store) -- kept only so --data matches other scripts' habits")
    ap.add_argument("--max-concurrent", type=int, default=15)
    ap.add_argument("--poll-interval-s", type=float, default=30.0)
    ap.add_argument("--initial-lookback-min", type=float, default=5.0)
    ap.add_argument("--grace-min", type=float, default=2.0,
                     help="how long past the horizon to keep polling a mint before retiring it, "
                          "in case its final bar0 label is still resolving")
    # Same barrier defaults as information_audit.py, so the label being
    # watched here IS the audit's own definition, not a different one.
    ap.add_argument("--upper", type=float, default=1.6)
    ap.add_argument("--lower", type=float, default=-0.30)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--bar-fraction", type=float, default=0.01)
    ap.add_argument("--bitquery-api-key", default=None)
    args = ap.parse_args()

    load_project_dotenv()
    bq_key = args.bitquery_api_key or os.environ.get("BITQUERY_API_KEY")
    if not bq_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2
    try:
        source = HeliusSource()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    cfg = BarrierConfig(args.upper, args.lower, int(args.horizon_min * 60_000))

    print("=" * 100)
    print("LIVE FEATURE/LABEL MONITOR -- not a trading bot. See this file's module docstring.")
    print(f"barrier: +{(args.upper - 1) * 100:.0f}% / {args.lower * 100:.0f}%  "
          f"horizon={args.horizon_min:.0f}min  bar_fraction={args.bar_fraction}  "
          f"max_concurrent={args.max_concurrent}  poll={args.poll_interval_s:.0f}s")
    print("Ctrl+C to stop.")
    print("=" * 100)

    tracked: Dict[str, TrackedMint] = {}
    session_outcomes: List[int] = []
    now_ms = int(time.time() * 1000)
    last_discovery_ts_ms = now_ms - int(args.initial_lookback_min * 60_000)

    try:
        while True:
            now_ms = int(time.time() * 1000)

            # 1. discover new launches since the last poll. Only advance the
            # watermark on SUCCESS -- on failure, keep it where it was so the
            # next cycle retries the same window instead of silently losing
            # whatever launched during the failed cycle (same "redundant
            # re-fetch over a silent gap" reasoning as RESUME_OVERLAP_MINUTES
            # elsewhere in this project).
            try:
                new_by_mint = fetch_new_launches(last_discovery_ts_ms, now_ms, bq_key)
                last_discovery_ts_ms = now_ms
            except RuntimeError as e:
                print(f"  [discovery FAILED this cycle, will retry the same window: {e}]",
                      file=sys.stderr)
                new_by_mint = {}

            skipped_this_cycle = 0
            for mint, ts_ms in new_by_mint.items():
                if mint in tracked:
                    continue
                if len(tracked) >= args.max_concurrent:
                    skipped_this_cycle += 1
                    continue
                tracked[mint] = TrackedMint(mint=mint, created_ts_ms=ts_ms, last_fetched_ts_ms=ts_ms)

            # 2. poll each tracked mint for new swaps, update bars/features
            for mint, tm in list(tracked.items()):
                age_min = (now_ms - tm.created_ts_ms) / 60_000
                if tm.error is None:
                    poll_mint(source, tm, now_ms, args.bar_fraction)

                lab = bar0_label(tm, cfg) if tm.error is None else None
                if lab is not None and not lab.truncated and not tm.bar0_reported:
                    tm.bar0_reported = True
                    session_outcomes.append(lab.outcome)
                    print(f"  [resolved] {_short(mint)}: {_OUTCOME_NAME.get(lab.outcome, lab.outcome)} "
                          f"at t+{(lab.t1_ms - tm.created_ts_ms) / 60_000:.1f}min "
                          f"(mfe={lab.mfe_pct:+.1%} mae={lab.mae_pct:+.1%})")

                retire_reason = None
                if tm.error is not None:
                    retire_reason = f"error: {tm.error[:120]}"
                elif age_min > args.horizon_min + args.grace_min:
                    # Always retire past horizon+grace, no matter what --
                    # a thinly-traded mint whose dollar-bars never advance
                    # past the deadline must still free its tracking slot,
                    # or --max-concurrent would silently leak down to zero.
                    if tm.total_swaps < MIN_SWAPS_PER_TOKEN:
                        retire_reason = f"aged out with only {tm.total_swaps} swaps (< {MIN_SWAPS_PER_TOKEN})"
                    elif tm.bar0_reported:
                        retire_reason = "aged out, bar0 label already resolved"
                    else:
                        retire_reason = "aged out, bar0 label never resolved (thin/slow trading)"
                if retire_reason:
                    print(f"  [retiring] {_short(mint)}: {retire_reason}")
                    del tracked[mint]

            # 3. print live status
            print_status(tracked, cfg, args.horizon_min, session_outcomes, skipped_this_cycle, now_ms)

            time.sleep(args.poll_interval_s)
    except KeyboardInterrupt:
        print("\nStopped.")
        n_up = sum(1 for o in session_outcomes if o == UP)
        n_down = sum(1 for o in session_outcomes if o == DOWN)
        n_timeout = sum(1 for o in session_outcomes if o == TIMEOUT)
        print(f"Session total: {len(session_outcomes)} bar0 label(s) resolved "
              f"({n_up} UP / {n_down} DOWN / {n_timeout} TIMEOUT). This is informal and "
              f"unweighted -- NOT a substitute for information_audit.py's controlled, "
              f"multiple-testing-corrected evaluation over the full accumulated Store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
