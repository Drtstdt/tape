"""Rails: reasons to NOT trade a token. Every rail is a veto; a missing input
fails CLOSED. Two moments:
  at create     metadata/socials, copycat, creator history, dev initial buy
  at decision   dev sold, dev holding, observed top-10 concentration, first-slot
                (bundle/sniper) share, non-standard curve (mayhem)
All thresholds live in Config (config/pump_live.json overrides); status
'unvalidated' -- they encode documented risks (D134/D135, MemeTrans, 'Meme Coin
Factories'), not a measured edge."""

from __future__ import annotations

from typing import Dict, List

from .meta import socials, twitter_handle, twitter_kind

SUPPLY = 1_000_000_000.0
K_STD = 30.0 * 1.073e9


def create_rails(cfg, tok, meta, recent) -> List[str]:
    """recent: RecentIndex (link / name / creator counts over rolling windows)."""
    why = []
    if meta is None:
        why.append("no_metadata")
    s = socials(meta)
    if cfg.require_social and not any(s.values()):
        why.append("no_socials")
    if cfg.require_twitter and twitter_kind((meta or {}).get("twitter")) not in cfg.twitter_kinds_ok:
        why.append("twitter_kind")
    for kind, link in s.items():
        if link and recent.link_count(link) > cfg.max_link_reuse:
            why.append(f"reused_{kind}")
    h = twitter_handle((meta or {}).get("twitter"))
    if h and "x.com/" + h != s["twitter"] and recent.link_count("x.com/" + h) > cfg.max_link_reuse:
        why.append("reused_twitter_handle")
    if recent.name_count(tok.name, tok.symbol) > cfg.max_name_reuse:
        why.append("copycat_name")
    if recent.creator_count(tok.creator) > cfg.max_creator_launches_24h:
        why.append("serial_creator_24h")
    hist = recent.creator_history(tok.creator)
    if hist and hist["launches"] >= cfg.farm_min_launches and hist["grads"] == 0:
        why.append("creator_farm")
    if tok.dev_initial_tokens / SUPPLY > cfg.max_dev_initial_share:
        why.append("dev_initial_buy")
    return why


def decision_rails(cfg, tok) -> List[str]:
    why = []
    if tok.k and abs(tok.k / K_STD - 1) > 0.01:
        why.append("nonstandard_curve")
    if tok.creator_sold_tokens > 0:
        why.append("dev_sold")
    if tok.holding(tok.creator) / SUPPLY > cfg.max_dev_hold_share:
        why.append("dev_holding")
    if tok.top_holders_share(10) > cfg.max_top10_share:
        why.append("top10_concentration")
    if tok.first_slot_other_share() > cfg.max_first_slot_share:
        why.append("bundle_first_slot")
    return why


class RecentIndex:
    """Rolling counts (7 days for links, 24 h for names/creators) with O(1)
    lookups, + static creator history from build_creator_stats.py."""

    WINDOWS = {"link": 7 * 86400, "name": 86400, "creator": 86400}

    def __init__(self, creator_stats: Dict[str, Dict] = None):
        from collections import Counter, deque
        self.q = {k: deque() for k in self.WINDOWS}
        self.c = {k: Counter() for k in self.WINDOWS}
        self.creator_stats = creator_stats or {}

    def _push(self, kind, now, key):
        self.q[kind].append((now, key))
        self.c[kind][key] += 1

    def _prune(self, now):
        for kind, win in self.WINDOWS.items():
            dq, cnt = self.q[kind], self.c[kind]
            while dq and now - dq[0][0] >= win:
                _, key = dq.popleft()
                cnt[key] -= 1
                if cnt[key] <= 0:
                    del cnt[key]

    def add(self, now, tok, meta):
        self._prune(now)
        for link in socials(meta).values():
            if link:
                self._push("link", now, link)
        h = twitter_handle((meta or {}).get("twitter"))
        if h and "x.com/" + h != socials(meta)["twitter"]:       # a tweet/other link of a handle
            self._push("link", now, "x.com/" + h)
        self._push("name", now, (str(tok.name).strip().lower(), str(tok.symbol).strip().lower()))
        self._push("creator", now, tok.creator)

    def link_count(self, link):
        return self.c["link"].get(link, 0)

    def name_count(self, name, symbol):
        return self.c["name"].get((str(name).strip().lower(), str(symbol).strip().lower()), 0)

    def creator_count(self, creator):
        return self.c["creator"].get(creator, 0)

    def creator_history(self, creator):
        return self.creator_stats.get(creator)
