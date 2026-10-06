"""The live paper engine: pure event handlers, no network. feeds.py calls
on_create / on_meta / on_logs / on_migration / on_clock; the engine answers
with subscribe / unsubscribe requests through two callbacks."""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from .events import trades_from_logs
from .learner import Learner
from .paper import Config, Position, open_position
from .rails import RecentIndex, create_rails, decision_rails
from .state import TokenState


@dataclass
class Settings:
    # rails (status: unvalidated -- documented risks, not a measured edge)
    require_social: bool = True
    require_twitter: bool = False
    twitter_kinds_ok: Tuple[str, ...] = ("profile", "tweet", "community")
    max_link_reuse: int = 2              # other fresh tokens (7 d) with the same link
    max_name_reuse: int = 0              # other tokens (24 h) with the same name+symbol
    max_creator_launches_24h: int = 3
    farm_min_launches: int = 20          # >= this many launches and 0 graduations -> farm
    max_dev_initial_share: float = 0.10
    max_dev_hold_share: float = 0.10
    max_top10_share: float = 0.50
    max_first_slot_share: float = 0.25
    # watching
    max_watched: int = 800
    watch_ms: int = 61 * 60_000
    max_decision_ms: int = 60 * 60_000
    # entries x exits (fixed grid -> honest statistics)
    floor_q_gain: float = 2.53           # family-A FLOOR threshold (trials/family_A_v3)
    tps: Tuple[float, ...] = (0.2, 0.3, 0.6, 1.0)
    sls: Tuple[float, ...] = (-0.15, -0.30, -0.50)
    horizons_min: Tuple[int, ...] = (5, 10, 30)
    size_sol: float = 0.5
    latency_slots: int = 2
    tip_sol: float = 0.001
    # learner
    window_days: float = 30.0
    min_trades: int = 200

    @classmethod
    def load(cls, path):
        s = cls()
        p = Path(path) if path else None
        if p and p.exists():
            for k, v in json.loads(p.read_text(encoding="utf-8")).items():
                if not hasattr(s, k):
                    raise KeyError(f"unknown setting {k!r} in {p}")
                setattr(s, k, tuple(v) if isinstance(getattr(s, k), tuple) else v)
        return s


ENTRY_RULES = {
    "K5_all": lambda tok, bars, s: bars == 5,
    "K10_all": lambda tok, bars, s: bars == 10,
    "K10_floor": lambda tok, bars, s: bars == 10 and tok.q_gain <= s.floor_q_gain,
    "K20_all": lambda tok, bars, s: bars == 20,
}


def build_configs(s: Settings) -> List[Config]:
    out = []
    for rule, tp, sl, h in itertools.product(ENTRY_RULES, s.tps, s.sls, s.horizons_min):
        out.append(Config(f"{rule}|tp{tp}|sl{sl}|h{h}", rule, tp, sl, h * 60_000, s.size_sol, s.latency_slots,
                          s.tip_sol))
    return out


class Engine:
    def __init__(self, s: Settings, recorder, subscribe: Callable, unsubscribe: Callable,
                 creator_stats: Dict = None, log=print):
        self.s, self.rec, self.sub, self.unsub, self.log = s, recorder, subscribe, unsubscribe, log
        self.configs = build_configs(s)
        self.by_rule = {r: [c for c in self.configs if c.entry == r] for r in ENTRY_RULES}
        self.learner = Learner(len(self.configs), s.window_days, s.min_trades)
        self.recent = RecentIndex(creator_stats)
        self.tokens: Dict[str, TokenState] = {}
        self.by_curve: Dict[str, str] = {}
        self.positions: Dict[str, List[Position]] = {}
        self.counts = {"creates": 0, "create_skips": 0, "rail_rejects": 0, "watched": 0, "trades": 0, "decisions": 0,
                       "decision_rejects": 0, "closed": 0, "lag_ms_sum": 0, "lag_n": 0}
        self.reject_reasons: Dict[str, int] = {}

    # ---- create / metadata ------------------------------------------------
    def on_create(self, msg: dict, now_ms: int = None):
        now_ms = now_ms or int(time.time() * 1000)
        try:
            vs, vt = float(msg.get("vSolInBondingCurve") or 30.0), float(msg.get("vTokensInBondingCurve") or 1.073e9)
            tok = TokenState(mint=msg["mint"], created_ms=now_ms, creator=msg.get("traderPublicKey", ""),
                             name=msg.get("name", ""), symbol=msg.get("symbol", ""), uri=msg.get("uri", ""),
                             bonding_curve=msg.get("bondingCurveKey", ""),
                             dev_initial_tokens=float(msg.get("initialBuy") or 0.0), vsol=vs, vtok=vt)
        except (KeyError, ValueError, TypeError):
            return None
        self.counts["creates"] += 1
        self.tokens[tok.mint] = tok
        self.rec.write("create", mint=tok.mint, creator=tok.creator, name=tok.name, symbol=tok.symbol, uri=tok.uri,
                       curve=tok.bonding_curve, dev_initial=tok.dev_initial_tokens, vsol=vs, vtok=vt,
                       sig=msg.get("signature"))
        # mayhem-mode tokens use a non-standard curve, and PumpPortal reports one shared
        # bondingCurveKey for many of them (probe 2026-10): never watch them; also never let
        # a second mint overwrite the curve -> mint mapping of a watched one
        why = None
        if msg.get("is_mayhem_mode"):
            why = "mayhem_create"
        elif not tok.bonding_curve:
            why = "no_curve_key"
        elif tok.bonding_curve in self.by_curve:
            why = "dup_curve_key"
        elif len(self.by_curve) >= self.s.max_watched:
            why = "watch_full"
        if why:
            self.counts["create_skips"] += 1
            self.reject_reasons[why] = self.reject_reasons.get(why, 0) + 1
            self.tokens.pop(tok.mint, None)
            return None                                        # no metadata fetch, no subscription
        # subscribe at once (snipers trade within the first slots); rails decide when metadata arrives
        self.by_curve[tok.bonding_curve] = tok.mint
        self.sub(tok.bonding_curve)
        return tok

    def on_meta(self, mint: str, meta, now_ms: int = None):
        tok = self.tokens.get(mint)
        if tok is None:
            return
        now_s = (now_ms or int(time.time() * 1000)) / 1000
        tok.meta = meta
        tok.create_rails = create_rails(self.s, tok, meta, self.recent)
        self.recent.add(now_s, tok, meta)
        self.rec.write("meta", mint=mint, meta=meta, rails=tok.create_rails)
        if tok.create_rails:
            self.counts["rail_rejects"] += 1
            for r in tok.create_rails:
                self.reject_reasons[r] = self.reject_reasons.get(r, 0) + 1
            self._drop(mint)
        else:
            self.counts["watched"] += 1

    def _drop(self, mint):
        tok = self.tokens.pop(mint, None)
        if tok and self.by_curve.get(tok.bonding_curve) == mint:
            del self.by_curve[tok.bonding_curve]
            self.unsub(tok.bonding_curve)
        self.positions.pop(mint, None)

    # ---- trades ---------------------------------------------------------------
    def on_logs(self, curve: str, slot: int, sig: str, logs: List[str], err=None, now_ms: int = None):
        if err is not None:
            return
        now_ms = now_ms or int(time.time() * 1000)
        mint = self.by_curve.get(curve)
        tok = self.tokens.get(mint) if mint else None
        if tok is None:
            return
        for t in trades_from_logs(logs, slot, sig):
            if t.mint != mint:
                continue
            self.counts["trades"] += 1
            self.counts["lag_ms_sum"] += max(0, now_ms - t.ts * 1000)
            self.counts["lag_n"] += 1
            if t.creator and t.creator != tok.creator:             # the on-chain event is authoritative
                tok.creator = t.creator
            closed_bars = tok.on_trade(t, now_ms)
            self.rec.write("trade", mint=mint, slot=slot, sig=sig, i=t.idx, b=int(t.is_buy), sol=t.sol, tok=t.tokens,
                           u=t.user, ts=t.ts, vs=t.vsol, vt=t.vtok, rs=t.real_sol, rt=t.real_tok, fee=t.fee,
                           cfee=t.creator_fee)
            for p in self.positions.get(mint, []):
                if p.state != "closed":
                    p.on_trade(t, now_ms, tok.graduated)
                    self._maybe_close(p)
            if tok.meta is not None and not tok.create_rails:
                for bars in closed_bars:
                    self._decide(tok, bars, t, now_ms)

    def _fees(self, t):
        if t.fee is not None and t.sol > 0:
            r = (t.fee + (t.creator_fee or 0.0)) / t.sol
            if 0 < r < 0.05:
                return r, r
        return 0.0125, 0.0125

    def _decide(self, tok, bars, t, now_ms):
        if now_ms - tok.created_ms > self.s.max_decision_ms:
            return
        fired = [r for r, f in ENTRY_RULES.items() if f(tok, bars, self.s)]
        if not fired:
            return
        why = decision_rails(self.s, tok)
        self.counts["decisions"] += 1
        self.rec.write("decision", mint=tok.mint, bars=bars, rules=fired, rails=why, slot=t.slot, vsol=tok.vsol,
                       q_gain=tok.q_gain, n_trades=tok.n_trades, first_obs_slot=tok.first_observed_slot)
        if why:
            self.counts["decision_rejects"] += 1
            for r in why:
                self.reject_reasons[r] = self.reject_reasons.get(r, 0) + 1
            return
        fb, fs = self._fees(t)
        for rule in fired:
            for c in self.by_rule[rule]:
                self.positions.setdefault(tok.mint, []).append(open_position(c, tok, t.slot, now_ms, fb, fs))

    def _maybe_close(self, p):
        if p.state == "closed" and not p.extra.get("logged"):
            p.extra["logged"] = True
            self.counts["closed"] += 1
            if p.net is not None:
                self.learner.add(p.cfg.cid, p.exit_ms, p.net)
            self.rec.write("close", cid=p.cfg.cid, mint=p.mint, net=p.net, reason=p.exit_reason,
                           entry_vsol=p.entry_vsol, entry_ms=p.entry_ms, exit_ms=p.exit_ms)

    def on_migration(self, mint: str, now_ms: int = None):
        tok = self.tokens.get(mint)
        if tok:
            tok.graduated = True
            self.rec.write("migration", mint=mint)

    # ---- clock ----------------------------------------------------------------
    def on_clock(self, now_ms: int = None):
        now_ms = now_ms or int(time.time() * 1000)
        for mint, ps in list(self.positions.items()):
            for p in ps:
                if p.state != "closed":
                    p.on_clock(now_ms)
                    self._maybe_close(p)
        for mint, tok in list(self.tokens.items()):
            open_ps = [p for p in self.positions.get(mint, []) if p.state != "closed"]
            if now_ms - tok.created_ms > self.s.watch_ms and not open_ps:
                self._drop(mint)
            elif tok.meta is None and now_ms - tok.created_ms > 60_000:
                self.on_meta(mint, None, now_ms)                   # metadata never came: fail closed

    def report(self, now_ms: int = None) -> dict:
        now_ms = now_ms or int(time.time() * 1000)
        stats = self.learner.stats(now_ms)
        champ = self.learner.champion(now_ms)
        c = dict(self.counts)
        c["avg_feed_lag_s"] = round(c.pop("lag_ms_sum") / max(c.pop("lag_n"), 1) / 1000, 2)
        rep = {"counts": c, "watched_now": len(self.by_curve), "reject_reasons": dict(sorted(
            self.reject_reasons.items(), key=lambda kv: -kv[1])[:12]),
            "top": stats[:5], "champion": champ["cid"] if champ else "ABSTAIN"}
        self.rec.write("report", **rep)
        return rep
