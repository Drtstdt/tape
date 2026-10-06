#!/usr/bin/env python3
"""D84 (docs/DECISIONS.md) -- Phase B of docs/PAPER_TRADING_PLAN.md: live
paper trading with `tape/online_policy.py::OnlinePolicy`.

Built by extending `scripts/live_paper_monitor.py`'s existing
discovery/polling/bar-building loop (reused verbatim -- discovery query,
`poll_mint`, concurrency cap, retirement logic) rather than duplicating it,
per that script's own module docstring inviting exactly this ("There is
therefore no validated entry rule to simulate trades with. This script
invents none." -- this one does, explicitly, as an ONLINE, self-improving
one, not a validated one).

Same fixed exit rule, same `PaperRails`, same `OnlinePolicy`, same ledger
schema (`data/paper_trades.csv`, `mode="live"`) as
`scripts/paper_trade_replay.py`, so the two are directly comparable and a
single `scripts/paper_trading_report.py` run can pool both (or isolate one
via `--mode`). Run this ONLY after Phase A (`paper_trade_replay.py`) has
been checked and looks mechanically correct -- see
docs/PAPER_TRADING_PLAN.md Sec 8's build order.

ONE DECISION PER TOKEN, same as replay: the policy decides at the first
bar where `PaperRails` clear, then waits for that bar's `triple_barrier`
outcome to resolve (or for the token to age out unresolved) before moving
on. A parallel random-policy baseline (epsilon=1.0) decides at the same
point and is logged separately, exactly as in replay.

    python scripts/paper_trade_live.py --data data
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.bars import BarBuilder, band_bar_threshold
from tape.costs import CostModel
from tape.env import load_project_dotenv
from tape.features import TokenState
from tape.labels import BarrierConfig, triple_barrier
from tape.online_policy import OnlinePolicy, PaperRails, evaluate_paper_rails
from tape.sanity import filter_post_migration_swaps
from tape.sources.bitquery import PUMPFUN_PROGRAM, _ms_to_bq_iso, _parse_bq_iso_ms
from tape.sources.helius import HeliusSource

from paper_trade_replay import LEDGER_COLUMNS, _pnl_for_outcome  # noqa: E402

MIN_SWAPS_PER_TOKEN = 50  # same corpus filter as information_audit.py / live_paper_monitor.py

ENDPOINT = "https://streaming.bitquery.io/graphql"
BQ_MAX_RETRIES = 5
BQ_RETRY_BACKOFF_CAP_S = 30.0
PAGE_LIMIT = 1000

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
    error: Optional[str] = None
    total_swaps: int = 0
    # -- decision/resolution state, new for paper trading --
    decided: bool = False
    decision_index: Optional[int] = None
    decision_features: Optional[Dict] = None
    online_decision: Optional[Dict] = None
    random_decision: Optional[Dict] = None
    resolved: bool = False
    # Same rejection-reason tracking added to scripts/paper_trade_replay.py
    # after the real 0/65-decisions replay run (docs/DECISIONS.md D85) --
    # whichever PaperRails check the LATEST bar still fails, so a token that
    # ages out with "rails never passed" is never a silent dead end here
    # either. Overwritten every bar; only meaningful once decided is False
    # and the mint eventually retires.
    last_rail_reason: Optional[str] = None
    # D95 (docs/DECISIONS.md): set once this mint's swap stream has shown a
    # PumpSwap-touching swap. Sticky, not re-checked per poll -- once a mint
    # has migrated off the bonding curve there is nothing left for this
    # pipeline to watch, so every later poll_mint() call short-circuits
    # before spending an API call on it.
    migrated: bool = False


def poll_mint(source: HeliusSource, tm: TrackedMint, now_ms: int, bar_fraction: float,
             rails: PaperRails, online: OnlinePolicy, random_baseline: OnlinePolicy) -> None:
    if tm.error is not None:
        return
    if tm.migrated:
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

    # D95 (docs/DECISIONS.md): drop this mint's swaps from the first
    # PumpSwap-touching one onward, same cut as paper_trade_replay.py, and
    # latch `migrated` so every later poll short-circuits above instead of
    # re-fetching data for a mint this pipeline can no longer model.
    new_swaps, post_migration = filter_post_migration_swaps(new_swaps)
    if post_migration:
        tm.migrated = True
    if not new_swaps:
        return

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
        tm.h.append(bar.high); tm.l.append(bar.low); tm.c.append(bar.close)
        tm.t.append(bar.close_ts_ms)

        if not tm.decided:
            rail_reason = evaluate_paper_rails(tm.latest_feats, rails)
            if rail_reason is None:
                tm.decided = True
                tm.decision_index = len(tm.c) - 1
                tm.decision_features = dict(tm.latest_feats)
                tm.online_decision = online.decide(tm.decision_features)
                tm.random_decision = random_baseline.decide(tm.decision_features)
            else:
                tm.last_rail_reason = rail_reason[0]


def try_resolve(tm: TrackedMint, cfg: BarrierConfig, online: OnlinePolicy,
                random_baseline: OnlinePolicy, cost: CostModel, nominal_size: float,
                mode: str) -> Optional[List[Dict]]:
    """Returns ledger rows once the decision bar's label resolves (or None
    while still pending / truncated). Calls update() on BOTH policies the
    moment it resolves -- full-feedback learning, same as
    scripts/paper_trade_replay.py::replay_one, and the same reasoning
    (tape/online_policy.py's module docstring): the true outcome is
    observable on-chain whether or not a paper position was taken."""
    if not tm.decided or tm.resolved:
        return None
    label = triple_barrier(tm.h, tm.l, tm.c, tm.t, tm.decision_index, cfg, mint=tm.mint)
    if label.truncated:
        return None  # still pending, or the tape ended before the horizon -- keep waiting

    tm.resolved = True
    y = 1 if label.outcome == 1 else 0  # UP == 1 in tape/labels.py
    raw_pnl = _pnl_for_outcome(label.outcome, label.ret_at_horizon_pct, cfg)
    cost_adjusted_pnl = raw_pnl - cost.round_trip_pct(nominal_size)

    rows = []
    for policy_name, policy, decision in (
        ("online", online, tm.online_decision), ("random", random_baseline, tm.random_decision)
    ):
        policy.update(tm.decision_features, y)
        rows.append({
            "mode": mode, "policy": policy_name, "mint": tm.mint,
            "decision_ts_ms": tm.t[tm.decision_index], "bar_index": tm.decision_index,
            "action": decision["action"], "explore": decision["explore"],
            "p_raw": decision["p_raw"], "epsilon_at_decision": decision["epsilon_at_decision"],
            "min_probability": decision["min_probability"],
            "outcome": {1: "UP", 0: "DOWN", -1: "TIMEOUT"}[label.outcome],
            "truncated": label.truncated,
            "raw_pnl_pct": raw_pnl if decision["action"] == "enter" else None,
            "cost_adjusted_pnl_pct": cost_adjusted_pnl if decision["action"] == "enter" else None,
            "n_bars_at_decision": tm.decision_features.get("n_bars"),
            "age_ms_at_decision": tm.decision_features.get("age_ms"),
            "model_n_decisions": policy.n_decisions,
        })
    return rows


def append_ledger(rows: List[Dict], ledger_path: Path) -> None:
    write_header = not ledger_path.exists()
    with open(ledger_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--max-concurrent", type=int, default=15)
    ap.add_argument("--poll-interval-s", type=float, default=30.0)
    ap.add_argument("--initial-lookback-min", type=float, default=5.0)
    ap.add_argument("--grace-min", type=float, default=2.0)
    ap.add_argument("--upper", type=float, default=1.6)
    ap.add_argument("--lower", type=float, default=-0.30)
    ap.add_argument("--horizon-min", type=float, default=30.0)
    ap.add_argument("--bar-fraction", type=float, default=0.01)
    ap.add_argument("--nominal-size", type=float, default=1.0)
    ap.add_argument("--epsilon-start", type=float, default=0.90)
    ap.add_argument("--epsilon-floor", type=float, default=0.15)
    ap.add_argument("--epsilon-decay-scale", type=float, default=60.0)
    ap.add_argument("--learning-rate", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model-in", default=None,
                     help="resume from a checkpoint, e.g. one saved by paper_trade_replay.py "
                          "-- RECOMMENDED, so live picks up wherever Phase A already learned to")
    ap.add_argument("--model-out", default=None,
                     help="path to save the checkpoint (default: <--data>/online_policy_state.json)")
    ap.add_argument("--checkpoint-every", type=int, default=5,
                     help="save the model checkpoint every N resolved decisions")
    ap.add_argument("--ledger-out", default=None,
                     help="default: <--data>/paper_trades.csv -- SAME file paper_trade_replay.py "
                          "writes, so paper_trading_report.py can pool or isolate by --mode")
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
    cost = CostModel()
    rails = PaperRails()
    feature_names = list(TokenState("probe").features().keys())

    if args.model_in:
        online = OnlinePolicy.load(Path(args.model_in))
        print(f"resumed online policy from {args.model_in} "
              f"(n_decisions={online.n_decisions}, n_updates={online.n_updates})")
    else:
        online = OnlinePolicy(feature_names, upper_multiple=args.upper, lower_pct=args.lower,
                              learning_rate=args.learning_rate, epsilon_start=args.epsilon_start,
                              epsilon_floor=args.epsilon_floor,
                              epsilon_decay_scale=args.epsilon_decay_scale, seed=args.seed)
    random_baseline = OnlinePolicy(feature_names, upper_multiple=args.upper, lower_pct=args.lower,
                                   epsilon_start=1.0, epsilon_floor=1.0, seed=args.seed + 1)

    ledger_path = Path(args.ledger_out) if args.ledger_out else Path(args.data) / "paper_trades.csv"
    model_out = Path(args.model_out) if args.model_out else Path(args.data) / "online_policy_state.json"

    print("=" * 100)
    print("PAPER TRADE LIVE (Phase B, docs/PAPER_TRADING_PLAN.md) -- fake money, real decisions.")
    print(f"exit rule (fixed): +{(args.upper - 1) * 100:.0f}% / {args.lower * 100:.0f}%  "
          f"horizon={args.horizon_min:.0f}min  bar_fraction={args.bar_fraction}")
    print(f"exploration: epsilon {args.epsilon_start:.2f} -> {args.epsilon_floor:.2f}  "
          f"max_concurrent={args.max_concurrent}  poll={args.poll_interval_s:.0f}s")
    print(f"ledger: {ledger_path}   model checkpoint: {model_out}")
    print("Ctrl+C to stop.")
    print("=" * 100)

    tracked: Dict[str, TrackedMint] = {}
    n_resolved_total = 0
    rail_reject_counter: Counter = Counter()
    now_ms = int(time.time() * 1000)
    last_discovery_ts_ms = now_ms - int(args.initial_lookback_min * 60_000)

    try:
        while True:
            now_ms = int(time.time() * 1000)

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

            newly_resolved: List[Dict] = []
            for mint, tm in list(tracked.items()):
                age_min = (now_ms - tm.created_ts_ms) / 60_000
                if tm.error is None:
                    poll_mint(source, tm, now_ms, args.bar_fraction, rails, online, random_baseline)

                if tm.error is None and tm.decided and not tm.resolved:
                    rows = try_resolve(tm, cfg, online, random_baseline, cost,
                                       args.nominal_size, mode="live")
                    if rows is not None:
                        newly_resolved.extend(rows)
                        entered = [r for r in rows if r["policy"] == "online" and r["action"] == "enter"]
                        pnl_str = (f"pnl={entered[0]['cost_adjusted_pnl_pct']:+.1%}"
                                  if entered else "abstained")
                        print(f"  [resolved] {mint[:4]}..{mint[-4:]}: {rows[0]['outcome']} "
                              f"online={rows[0]['action']} ({pnl_str})")

                retire_reason = None
                if tm.error is not None:
                    retire_reason = f"error: {tm.error[:120]}"
                elif age_min > args.horizon_min + args.grace_min:
                    if tm.total_swaps < MIN_SWAPS_PER_TOKEN:
                        retire_reason = f"aged out with only {tm.total_swaps} swaps (< {MIN_SWAPS_PER_TOKEN})"
                    elif not tm.decided:
                        reason_tag = tm.last_rail_reason or "no_bars_emitted"
                        rail_reject_counter[reason_tag] += 1
                        retire_reason = (f"aged out, rails never passed ({reason_tag}) "
                                        "-- no decision made")
                    elif tm.resolved:
                        retire_reason = "aged out, decision already resolved"
                    else:
                        retire_reason = "aged out, decision made but never resolved (thin/slow trading)"
                if retire_reason:
                    print(f"  [retiring] {mint[:4]}..{mint[-4:]}: {retire_reason}")
                    del tracked[mint]

            if newly_resolved:
                append_ledger(newly_resolved, ledger_path)
                n_resolved_total += len(newly_resolved) // 2
                if n_resolved_total % max(args.checkpoint_every, 1) == 0:
                    online.save(model_out)
                    print(f"  [checkpoint] saved online policy to {model_out} "
                          f"(n_decisions={online.n_decisions}, n_updates={online.n_updates}, "
                          f"epsilon={online.epsilon:.3f})")

            print(f"\n[{time.strftime('%H:%M:%S', time.localtime(now_ms / 1000))}] "
                  f"tracking {len(tracked)}  resolved-this-session={n_resolved_total}  "
                  f"skipped(at capacity)={skipped_this_cycle}")
            time.sleep(args.poll_interval_s)
    except KeyboardInterrupt:
        online.save(model_out)
        print(f"\nStopped. Saved final checkpoint to {model_out}. "
              f"Resolved {n_resolved_total} decision(s) this session. "
              f"Run scripts/paper_trading_report.py for the pre-registered verdict "
              f"(not a substitute for it -- see that script's own docstring).")
        if rail_reject_counter:
            print("Rail-rejection reasons for mints that aged out with no decision "
                  "(docs/DECISIONS.md D85):")
            for reason, count in rail_reject_counter.most_common():
                print(f"    {reason}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
