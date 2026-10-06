"""Self-improvement with a statistical brake.

In paper EVERY config is simulated on EVERY eligible token, so the comparison
between configs is exact (no exploration cost). The learner keeps each config's
closed trades for a rolling window and, every report, picks the CHAMPION:
the config with the highest LOWER confidence bound of its mean net return,
using a day-clustered standard error and a Bonferroni z over all configs
(the search-size correction learnt in D142/D143). If no lower bound is above
zero, the champion is ABSTAIN -- trading nothing is the default.
A real-money executor (not included) would only ever follow the champion."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from statistics import NormalDist
from typing import Dict, List, Optional

DAY_MS = 86_400_000


class Learner:
    def __init__(self, n_configs: int, window_days: float = 30.0, min_trades: int = 200, alpha: float = 0.05):
        self.window_ms = int(window_days * DAY_MS)
        self.min_trades = min_trades
        self.z = NormalDist().inv_cdf(1 - alpha / max(n_configs, 1))
        self.trades: Dict[str, deque] = defaultdict(deque)

    def add(self, cid: str, close_ms: int, net: float) -> None:
        if net is None or net != net:
            return
        self.trades[cid].append((close_ms, net))

    def _prune(self, now_ms):
        for dq in self.trades.values():
            while dq and now_ms - dq[0][0] > self.window_ms:
                dq.popleft()

    def stats(self, now_ms: int) -> List[dict]:
        self._prune(now_ms)
        out = []
        for cid, dq in self.trades.items():
            n = len(dq)
            if n == 0:
                continue
            nets = [x[1] for x in dq]
            mean = sum(nets) / n
            by_day = defaultdict(list)
            for ms, v in dq:
                by_day[ms // DAY_MS].append(v - mean)
            D = len(by_day)
            # cluster-robust SE of the mean: sqrt(sum_d (sum of residuals in day d)^2) / n
            se = math.sqrt(sum(sum(v) ** 2 for v in by_day.values())) / n if D > 1 else float("inf")
            se = se * math.sqrt(D / (D - 1)) if D > 1 else se
            lcb = mean - self.z * se
            out.append({"cid": cid, "n": n, "days": D, "mean": mean, "se": se, "lcb": lcb,
                        "win": sum(1 for v in nets if v > 0) / n})
        return sorted(out, key=lambda r: -r["lcb"])

    def champion(self, now_ms: int) -> Optional[dict]:
        for r in self.stats(now_ms):
            if r["n"] >= self.min_trades and r["days"] >= 7 and r["lcb"] > 0:
                return r
        return None
