#!/usr/bin/env python3
"""D77/D79 -- per-token "DNA" report: raw diagnostic table, not another
grid search.

D79: every token's swaps now go through tape/sanity.py::filter_implausible_swaps()
before anything else -- D78 found real evidence of non-trade transactions
(fee claims, dust transfers) getting miscounted as trades by
tape/sources/helius.py, producing implied prices 1,000x-30,000x their
neighbors (exactly the kind of value that was dominating this script's own
max_move_30m column and QUICK DESCRIPTIVE LOOK correlations). This filter
lives here and in scenario_backtest.py ONLY, by the user's explicit choice
-- NOT in tape/bars.py or information_audit.py's build_one(), which stay
untouched (see tape/sanity.py's docstring for why).

Direct user request, in response to scenario_backtest.py's D73-D76 findings
("kiedy coś już zrobi +200%, sprzedaż w tym momencie wygląda fantastycznie"
-- but that only proves a good EXIT rule, conditioned on the pump already
having started). The actually useful question is different: "can we detect
10-30 seconds early that a pearl is starting, BEFORE it's already up 200%?"
That is an ENTRY-signal question, not an exit-rule question, and answering
it needs to see each token's early trajectory laid out, not another summary
statistic.

This script produces exactly one flat row per token -- no grid, no
bootstrap, no p-value -- with the fields the user asked for:

    mint, entry_ts, entry_price
    time_to_+25 / +50 / +100 / +200   (seconds; None if never reached before horizon)
    max_move_30m, time_of_max_s        (largest UPWARD move in the horizon, and when)
    pnl_hold
    pnl_sell100_25 / _50 / _100 / _200 (sell 100% at first +X hit, else = pnl_hold --
                                         same DEPLOYABLE convention as scenario_backtest.py's
                                         D75 fix: never imputed as 0, never tautological)
    direction_of_trigger               ('+' / '-' / 'never': the FIRST move of
                                         magnitude >= 25% in EITHER direction --
                                         does a dump usually happen before a pump?)
    price_after_1s / 2s / 5s / 10s / 30s  (return since entry at fixed early
                                         offsets -- last observed price at or
                                         before that offset, never imputed)

Written to a CSV (one row per token) plus a short console preview, so the
user can eyeball it directly or load it into anything for further looking
-- pandas, Excel, a spreadsheet artifact.

One thing is added beyond the raw table, deliberately modest: a quick
DESCRIPTIVE look (Spearman correlation, a simple split) at whether the early
price_after_Ns columns relate to max_move_30m. This is explicitly NOT a
substitute for information_audit.py's actual gate (no permutation null, no
chronological split, no held-out test) -- it's a "does this look worth
building a real audit feature for" signal, not a claim of a real signal
existing. If it looks promising, the right next step is adding
price_after_Ns-style features to tape/features.py and letting
information_audit.py's Stage 1 gate judge them properly, the same discipline
D71 already applied to v3's flow-reversal idea.

    python scripts/token_dna_report.py --data data --real-creation-file data/real_creation_times.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from tape.sanity import filter_implausible_swaps
from tape.store import Store

# Same script directory is on sys.path[0] automatically -- reuses
# information_audit.py's universe-selection logic verbatim (see
# scenario_backtest.py's D73 note for why: any future fix there propagates
# here too, and the population stays directly comparable across scripts).
from information_audit import (  # noqa: E402
    MIN_SWAPS_PER_TOKEN,
    chronological_universe,
    filter_by_real_creation,
    load_real_creation_cache,
)

UP_THRESHOLDS: Tuple[float, ...] = (0.25, 0.50, 1.00, 2.00)
EARLY_OFFSETS_S: Tuple[float, ...] = (1, 2, 5, 10, 30)
DIRECTION_THRESHOLD = 0.25  # matches the lowest UP_THRESHOLDS entry


def token_dna(swaps, horizon_s: float) -> Optional[Dict]:
    """One token's real swap tape -> the full DNA row (as a dict of raw
    values; main() flattens it into the CSV's columns). None if the tape is
    too short to say anything (fewer than 2 swaps)."""
    if len(swaps) < 2:
        return None
    entry_ts = swaps[0].ts_ms
    entry_price = swaps[0].price
    if entry_price <= 0:
        return None

    horizon_deadline = entry_ts + int(horizon_s * 1000)
    horizon_price = entry_price
    horizon_truncated = True
    for s in swaps:
        if s.ts_ms > horizon_deadline:
            horizon_truncated = False
            break
        horizon_price = s.price
    hold_pnl = horizon_price / entry_price - 1.0

    # Upward-only thresholds -- the "pearl" question is specifically about
    # pumps, unlike scenario_backtest.py's symmetric +/- trigger.
    up_hits: Dict[float, Tuple[Optional[float], Optional[float]]] = {}
    for thr in UP_THRESHOLDS:
        t_hit = p_hit = None
        for s in swaps[1:]:
            if s.ts_ms > horizon_deadline:
                break
            if s.price / entry_price - 1.0 >= thr:
                t_hit = (s.ts_ms - entry_ts) / 1000.0
                p_hit = s.price
                break
        up_hits[thr] = (t_hit, p_hit)

    max_move = 0.0
    max_move_ts = entry_ts
    for s in swaps:
        if s.ts_ms > horizon_deadline:
            break
        m = s.price / entry_price - 1.0
        if m > max_move:
            max_move = m
            max_move_ts = s.ts_ms
    time_of_max_s = (max_move_ts - entry_ts) / 1000.0

    # Does a dump usually happen before a pump, or does it go straight up?
    # The FIRST move (either direction) whose magnitude clears the threshold.
    direction_of_trigger = "never"
    for s in swaps[1:]:
        if s.ts_ms > horizon_deadline:
            break
        m = s.price / entry_price - 1.0
        if abs(m) >= DIRECTION_THRESHOLD:
            direction_of_trigger = "+" if m > 0 else "-"
            break

    price_after: Dict[float, float] = {}
    for sec in EARLY_OFFSETS_S:
        deadline = entry_ts + int(sec * 1000)
        p = entry_price
        for s in swaps:
            if s.ts_ms > deadline:
                break
            p = s.price
        price_after[sec] = p / entry_price - 1.0

    pnl_sell100: Dict[float, float] = {}
    for thr in UP_THRESHOLDS:
        _, p_hit = up_hits[thr]
        pnl_sell100[thr] = (p_hit / entry_price - 1.0) if p_hit is not None else hold_pnl

    return {
        "entry_ts": entry_ts,
        "entry_price": entry_price,
        "horizon_truncated": horizon_truncated,
        "time_to": {thr: up_hits[thr][0] for thr in UP_THRESHOLDS},
        "max_move_30m": max_move,
        "time_of_max_s": time_of_max_s,
        "hold_pnl": hold_pnl,
        "pnl_sell100": pnl_sell100,
        "direction_of_trigger": direction_of_trigger,
        "price_after": price_after,
    }


def _pct_name(thr: float) -> str:
    return f"{thr:.0%}".replace("%", "")


def build_row(mint: str, dna: Dict) -> Dict:
    row = {
        "mint": mint,
        "entry_ts_ms": dna["entry_ts"],
        "entry_time_utc": dt.datetime.utcfromtimestamp(dna["entry_ts"] / 1000).isoformat() + "Z",
        "entry_price": dna["entry_price"],
        "horizon_truncated": dna["horizon_truncated"],
    }
    for thr in UP_THRESHOLDS:
        row[f"time_to_+{_pct_name(thr)}"] = dna["time_to"][thr]
    row["max_move_30m"] = dna["max_move_30m"]
    row["time_of_max_s"] = dna["time_of_max_s"]
    row["pnl_hold"] = dna["hold_pnl"]
    for thr in UP_THRESHOLDS:
        row[f"pnl_sell100_{_pct_name(thr)}"] = dna["pnl_sell100"][thr]
    row["direction_of_trigger"] = dna["direction_of_trigger"]
    for sec in EARLY_OFFSETS_S:
        row[f"price_after_{int(sec)}s"] = dna["price_after"][sec]
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--horizon-min", type=float, default=30.0,
                     help="matches information_audit.py's default")
    ap.add_argument("--real-creation-file", default=None)
    ap.add_argument("--max-observed-age-hours", type=float, default=6.0)
    ap.add_argument("--no-age-filter", action="store_true")
    ap.add_argument("--out", default=None,
                     help="output CSV path (default: <--data>/token_dna_report.csv)")
    ap.add_argument("--top", type=int, default=15,
                     help="how many rows (sorted by max_move_30m desc) to preview on console")
    args = ap.parse_args()

    store = Store(args.data)

    # -- Universe: identical logic/order to information_audit.py / scenario_backtest.py
    universe = chronological_universe(store, args.since, args.until)
    requested = len(universe)
    eligible_by_swaps = [(m, ts) for m, ts, n in universe if n >= MIN_SWAPS_PER_TOKEN]

    real_creation_path = Path(args.real_creation_file) if args.real_creation_file \
        else Path(args.data) / "real_creation_times.json"
    cache = None
    if not args.no_age_filter and real_creation_path.exists():
        try:
            cache = load_real_creation_cache(real_creation_path)
        except Exception as e:
            print(f"  (could not read {real_creation_path}: {e} -- age filter disabled)")

    excluded_too_old = excluded_no_real_creation = 0
    if cache is not None:
        eligible, excluded_too_old, excluded_no_real_creation = filter_by_real_creation(
            eligible_by_swaps, cache, args.max_observed_age_hours)
    else:
        eligible = eligible_by_swaps

    limited = eligible[: args.limit]
    mints = [m for m, _ in limited]

    print("=" * 78)
    print("UNIVERSE  (same selection logic as information_audit.py / scenario_backtest.py)")
    print("=" * 78)
    print(f"  requested: {requested}  eligible-by-swaps: {len(eligible_by_swaps)}  "
          f"excluded(too old): {excluded_too_old}  excluded(no data): {excluded_no_real_creation}  "
          f"considered: {len(mints)}")

    horizon_s = args.horizon_min * 60.0
    rows: List[Dict] = []
    n_too_short = 0
    n_swaps_dropped = 0
    n_tokens_with_drops = 0
    for mint in mints:
        swaps = list(store.iter_swaps(mint))
        if len(swaps) < MIN_SWAPS_PER_TOKEN:
            continue
        # D78/D79: drop swaps whose price is a >50x local outlier -- almost
        # certainly a non-trade transaction (fee claim, dust transfer, LP op)
        # miscounted as a trade (tape/sources/helius.py's own documented
        # limitation), not a real price move. See tape/sanity.py's docstring.
        swaps, implausible = filter_implausible_swaps(swaps)
        if implausible:
            n_swaps_dropped += len(implausible)
            n_tokens_with_drops += 1
        if len(swaps) < MIN_SWAPS_PER_TOKEN:
            continue
        dna = token_dna(swaps, horizon_s)
        if dna is None:
            n_too_short += 1
            continue
        rows.append(build_row(mint, dna))

    print(f"  used: {len(rows)}   too-short-to-simulate: {n_too_short}")
    print(f"  price-sanity filter (D78/D79): dropped {n_swaps_dropped} implausible swap(s) "
          f"across {n_tokens_with_drops} token(s) -- see tape/sanity.py")
    if not rows:
        print("\nstatus=collecting_data  no usable tokens")
        return 2

    df = pd.DataFrame(rows).sort_values("entry_ts_ms").reset_index(drop=True)

    out_path = Path(args.out) if args.out else Path(args.data) / "token_dna_report.csv"
    df.to_csv(out_path, index=False)
    print(f"\nwrote {len(df)} rows to {out_path}")

    preview_cols = ["mint", "entry_time_utc", "max_move_30m", "time_of_max_s",
                     "direction_of_trigger", "price_after_1s", "price_after_5s",
                     "price_after_10s", "price_after_30s", "pnl_hold"]
    print(f"\ntop {min(args.top, len(df))} tokens by max_move_30m:")
    with pd.option_context("display.width", 200, "display.max_columns", None,
                            "display.float_format", lambda v: f"{v:+.3f}" if isinstance(v, float) else str(v)):
        print(df.sort_values("max_move_30m", ascending=False)[preview_cols].head(args.top).to_string(index=False))

    # -- Quick descriptive look, explicitly NOT a Stage-1 gate -------------
    print("\n" + "=" * 78)
    print("QUICK DESCRIPTIVE LOOK  (not a rigorous test -- no permutation null, no chronological")
    print("split, no held-out set. This only says whether it's worth turning an early-price")
    print("feature into a real information_audit.py feature, same as D71 did for v3's idea.)")
    print("=" * 78)
    for sec in EARLY_OFFSETS_S:
        col = f"price_after_{int(sec)}s"
        sub = df[[col, "max_move_30m"]].dropna()
        if len(sub) < 10:
            continue
        rho = sub[col].corr(sub["max_move_30m"], method="spearman")
        pos = sub[sub[col] > 0]
        neg = sub[sub[col] <= 0]
        print(f"  {col:16s} spearman(vs max_move_30m)={rho:+.3f}   "
              f"n_already_up_by_then={len(pos):3d} mean_max_move={pos['max_move_30m'].mean() if len(pos) else float('nan'):+.3f}   "
              f"n_flat_or_down={len(neg):3d} mean_max_move={neg['max_move_30m'].mean() if len(neg) else float('nan'):+.3f}")
    print("\n  Reminder: same single-largely-one-day sample as every other script here (D56/D72).")
    print("  A promising-looking rho at n<70 is a reason to look closer, not to trust it yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
