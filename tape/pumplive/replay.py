"""D145: replay of the live paper bot on stored history (family R, stage 1).

The live code decides; history only supplies the events:
  * tape.pumplive.state.TokenState is fed the token's stored swaps (execution
    order inside slots, market curve state, tape.curve via prepare_token) as
    live `Trade` objects, up to the decision swap -> bars, holders, dev
    sold/holding, top-10, first-slot share exactly as live computes them;
  * tape.pumplive.rails.decision_rails runs on that state;
  * the create rails that need no metadata are computed point-in-time
    (creator history strictly before the create; dev buy in the create slot);
  * exits: tape.fastexit.exit_grid for the live Settings grid (TP x SL x
    horizon, 2 slots, 0.5 SOL, 0.001 SOL tip) -- equal to simulate_anchored
    (tests/test_fastexit.py), which the live Position equals
    (tests/test_pumplive.py); a direct Position-vs-grid test is in
    tests/test_replay.py.
Rails are RECORDED per decision (one boolean per rail), never applied inside the
replay, so rails-on and rails-off come from one pass.

The learner is replayed with a vectorised copy of learner.Learner.stats/champion
(equality tested): at each week start the champion is chosen from trades closed
before it (rolling window), then traded in that week only.
"""

from __future__ import annotations

import itertools
import math
from statistics import NormalDist
from typing import Dict, List, Sequence

import numpy as np

from .engine import ENTRY_RULES, Settings
from .events import Trade
from .rails import SUPPLY, decision_rails
from .state import TokenState

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
MONDAY0 = 4 * DAY_MS                     # 1970-01-05 was a Monday

DECISION_RAILS = ("nonstandard_curve", "dev_sold", "dev_holding", "top10_concentration", "bundle_first_slot")
ONCHAIN_CREATE_RAILS = ("dev_initial_buy", "serial_creator_24h", "creator_farm")
ALL_RAILS = ONCHAIN_CREATE_RAILS + DECISION_RAILS


def exit_combos(s: Settings):
    """(tp, sl, horizon_min) in the order of exit_grid(...).reshape(-1) == build_configs order."""
    return list(itertools.product(s.tps, s.sls, s.horizons_min))


def config_ids(s: Settings) -> List[str]:
    return [f"{rule}|tp{tp}|sl{sl}|h{h}" for rule in ENTRY_RULES for tp, sl, h in exit_combos(s)]


def onchain_create_rails(s: Settings, dev_initial_tokens: float, prior_launches: float,
                         prior_grads: float, launches_24h: float) -> List[str]:
    """The create rails of rails.create_rails that need no metadata, point-in-time.
    Unknown creator history (NaN) passes, as live with an empty creator_stats entry."""
    why = []
    if dev_initial_tokens / SUPPLY > s.max_dev_initial_share:
        why.append("dev_initial_buy")
    if launches_24h == launches_24h and launches_24h > s.max_creator_launches_24h:
        why.append("serial_creator_24h")
    if prior_launches == prior_launches and prior_launches >= s.farm_min_launches and prior_grads == 0:
        why.append("creator_farm")
    return why


def replay_token(s: Settings, mint: str, creator: str, create_ms: int, create_slot: int,
                 side_buy: np.ndarray, sol: np.ndarray, tokens: np.ndarray, wallet: Sequence[str],
                 ts_ms: np.ndarray, slot: np.ndarray, q: np.ndarray, k: float, decisions: Dict[int, int]):
    """Feed one token's swaps (execution order) to a live TokenState and snapshot
    the decision rails at each decision index. decisions: {K: swap index}.
    Returns {K: dict(raw values, rails list, bars, q_gain)} and dev_initial_tokens."""
    if not decisions:
        return {}, 0.0
    tok = TokenState(mint=mint, created_ms=int(create_ms), creator=creator or "", create_slot=int(create_slot),
                     dev_initial_tokens=0.0, vsol=float(q[0]), vtok=float(k / q[0]))
    want = {}
    for K, d in decisions.items():
        want.setdefault(int(d), []).append(int(K))
    last = max(want)
    out = {}
    dev_init = 0.0
    for i in range(last + 1):
        qi = float(q[i])
        t = Trade(mint, float(sol[i]), float(tokens[i]), bool(side_buy[i]), str(wallet[i]), int(ts_ms[i]) // 1000,
                  qi, float(k / qi), max(qi - 30.0, 0.0), 1.0, slot=int(slot[i]), sig=f"r{i}", idx=i)
        if creator and t.user == creator and t.is_buy and t.slot == create_slot:
            dev_init += t.tokens
        tok.on_trade(t, int(ts_ms[i]))
        if i in want:
            snap = {"bars": tok.bars, "q_gain": tok.q_gain, "rails": decision_rails(s, tok),
                    "dev_sold_tokens": tok.creator_sold_tokens, "dev_hold_share": tok.holding(tok.creator) / SUPPLY,
                    "top10_share": tok.top_holders_share(10), "first_slot_share": tok.first_slot_other_share(),
                    "n_trades": tok.n_trades}
            for K in want[i]:
                out[K] = snap
    return out, dev_init


# ---------------------------------------------------------------------------
# learner (vectorised copy of learner.Learner, equality tested)
# ---------------------------------------------------------------------------

def lcb_stats(net: np.ndarray, day: np.ndarray, z: float):
    """net: (n,) finite values of ONE config; day: (n,) day numbers.
    Returns (n, D, mean, se, lcb) exactly as Learner.stats."""
    n = len(net)
    if n == 0:
        return 0, 0, float("nan"), float("inf"), float("-inf")
    mean = float(net.mean())
    u, inv = np.unique(day, return_inverse=True)
    D = len(u)
    if D <= 1:
        return n, D, mean, float("inf"), float("-inf")
    sums = np.bincount(inv, weights=net - mean, minlength=D)
    se = math.sqrt(float((sums ** 2).sum())) / n * math.sqrt(D / (D - 1))
    return n, D, mean, se, mean - z * se


def learner_z(n_configs: int, alpha: float = 0.05) -> float:
    return NormalDist().inv_cdf(1 - alpha / max(n_configs, 1))


def champion_at(now_ms: int, close_ms: np.ndarray, nets: np.ndarray, elig: np.ndarray, z: float,
                window_ms: int, min_trades: int, min_days: int = 7):
    """Learner.champion at `now_ms` from trades closed before it, inside the window.
    close_ms, nets, elig: (n, C) -- per decision and config (close = decision +
    horizon, so it differs per config); elig = entry rule fired and rails passed.
    Champion = highest LCB among configs with n >= min_trades, >= min_days days
    and LCB > 0. Returns (config index or None, [(c, n, D, mean, se, lcb)] by LCB desc)."""
    rows = []
    for c in range(nets.shape[1]):
        m = (elig[:, c] & np.isfinite(nets[:, c]) & (close_ms[:, c] < now_ms)
             & (close_ms[:, c] >= now_ms - window_ms))              # Learner._prune keeps now-ms <= window
        n, D, mean, se, lcb = lcb_stats(nets[m, c], close_ms[m, c] // DAY_MS, z)
        if n:
            rows.append((c, n, D, mean, se, lcb))
    rows.sort(key=lambda r: -r[5])
    for r in rows:
        if r[1] >= min_trades and r[2] >= min_days and r[5] > 0:
            return r[0], rows
    return None, rows


# ---------------------------------------------------------------------------
# stage 2 (D147): metadata rails, point-in-time, with the live create_rails
# ---------------------------------------------------------------------------

META_RAILS = ("no_metadata", "no_socials", "twitter_kind", "reused_twitter", "reused_telegram", "reused_website",
              "reused_twitter_handle", "copycat_name")


def meta_rails_in_order(s: Settings, tokens, metas: Dict[str, dict]) -> Dict[str, List[str]]:
    """tokens: iterable of (mint, create_ms, creator, name, symbol) SORTED by create_ms.
    metas: mint -> pump.fun JSON fields (twitter/telegram/website) or None when the
    token really has no metadata. A mint ABSENT from `metas` (fetch failed today)
    is skipped: no rails, not added to the index (unknown, not 'none').
    Live order: rails are computed BEFORE the token joins the rolling index (link 7 d,
    name 24 h), so only earlier tokens count. Only the metadata rails are kept; the
    creator/dev rails come point-in-time from stage 1."""
    from types import SimpleNamespace
    from .rails import RecentIndex, create_rails
    recent = RecentIndex({})
    out = {}
    for mint, create_ms, creator, name, symbol in tokens:
        if mint not in metas:
            continue
        meta = metas[mint]
        tok = SimpleNamespace(mint=mint, creator=creator or "", name=name or "", symbol=symbol or "",
                              dev_initial_tokens=0.0)
        now = create_ms / 1000.0
        recent._prune(now)
        out[mint] = [r for r in create_rails(s, tok, meta, recent) if r in META_RAILS]
        recent.add(now, tok, meta)
    return out
