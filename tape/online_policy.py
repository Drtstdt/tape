"""D84 (docs/DECISIONS.md) -- an online-learning ENTRY policy for paper
trading, deliberately separate from `tape/policy.py::decide()` and
`tape/model.py::ModelArtifact`.

WHY THIS IS A DIFFERENT MODULE, NOT AN EXTENSION OF `tape/policy.py`:
`decide()` is built around an already-FITTED `ModelArtifact` -- isotonic
calibration and conformal credibility computed once over a fixed training
distribution. An online policy has neither on day one: it starts with no
data and updates itself after every resolved outcome, one step at a time.
Trying to bolt that onto `decide()`'s calibration/conformal machinery would
mean faking a calibration curve and a conformal calibration set that don't
exist yet. This module is the honest alternative: a small, transparent,
from-scratch model that is allowed to be wrong early and is judged on
whether it visibly improves, never on whether it starts good.

SCOPE (docs/PAPER_TRADING_PLAN.md Sec 3, user's explicit choice): v1 learns
only the ENTRY decision (enter vs abstain). The EXIT is the same fixed
`tape/labels.py::triple_barrier` rule `information_audit.py` and
`scripts/live_paper_monitor.py` already use -- learning the exit online too
is a deliberately deferred v2, so a v1 result isn't confounded by two
things changing at once (the exact trap D73-D76 already found and fixed
once, for the exit-only case).

A GENUINE SIMPLIFICATION FOUND WHILE BUILDING THIS (worth stating plainly):
this is NOT a classic partial-feedback bandit. On a public blockchain the
true outcome of a token is observable whether or not a paper position was
taken -- there is no hidden counterfactual the way there would be in, say,
a real ad-serving bandit. So `update()` below is called on every resolved
decision point regardless of the action taken: the model always learns
from the full, true label. Exploration (epsilon-greedy) is therefore not
about buying information the model couldn't otherwise get -- it is about
deliberately randomizing which decisions get PAPER-TRADED (and therefore
counted in the PnL ledger) early on, so the "idiotic mistakes, improves
over time" behavior the user asked to see is visible in the ledger, while
the belief model itself learns as fast as the data allows.

`p_raw` here carries NONE of `ModelArtifact.predict()`'s calibration
promise ("when it says 35%, it wins 35% of the time"). It is this online
model's raw, currently-held belief -- expected to be a bad guess early and
a better one later, never validated the way Stage 2-4 would validate it.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

MODEL_VERSION = "online_policy_v1"


def _sigmoid(z: float) -> float:
    # Clamp to avoid overflow on a wildly large dot product early in
    # training, when weights are still near their random/zero start and a
    # single outlier feature (e.g. an unnormalized age_ms) could otherwise
    # raise a real OverflowError on math.exp.
    z = max(-60.0, min(60.0, z))
    return 1.0 / (1.0 + math.exp(-z))


@dataclass
class _RunningStat:
    """Welford's online mean/variance, per feature. Used to standardize
    features on the fly -- an online model has no fixed training-set
    distribution to standardize against up front, so this estimate is
    itself continuously updated, same "causal, never a lookahead" spirit
    as everything else in `tape/`."""

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.m2 += delta * delta2

    @property
    def std(self) -> float:
        if self.n < 2:
            return 1.0  # no variance estimate yet -- don't divide by ~0
        var = self.m2 / (self.n - 1)
        return math.sqrt(var) if var > 1e-12 else 1.0

    def to_dict(self) -> Dict:
        return {"n": self.n, "mean": self.mean, "m2": self.m2}

    @staticmethod
    def from_dict(d: Dict) -> "_RunningStat":
        return _RunningStat(n=d["n"], mean=d["mean"], m2=d["m2"])


class OnlinePolicy:
    """Hand-rolled online logistic regression + epsilon-greedy exploration.

    Deliberately NOT a library (river/vowpalwabbit): every line here is
    meant to be as auditable and unit-testable as `tape/features.py`, with
    zero new dependencies, matching `information_audit.py`'s own printed
    doctrine -- "do not reach for a bigger model."
    """

    def __init__(
        self,
        feature_names: Sequence[str],
        upper_multiple: float = 1.6,
        lower_pct: float = -0.30,
        probability_margin: float = 0.04,
        learning_rate: float = 0.05,
        epsilon_start: float = 0.90,
        epsilon_floor: float = 0.15,
        epsilon_decay_scale: float = 60.0,
        seed: int = 0,
    ) -> None:
        self.feature_names: List[str] = list(feature_names)
        self.upper_multiple = upper_multiple
        self.lower_pct = lower_pct
        self.probability_margin = probability_margin
        self.learning_rate = learning_rate
        self.epsilon_start = epsilon_start
        self.epsilon_floor = epsilon_floor
        self.epsilon_decay_scale = epsilon_decay_scale

        # Breakeven derived directly from the SAME barrier config the fixed
        # exit rule uses (mirrors tape/policy.py::ConfidencePolicy's
        # breakeven_win_rate, but computed from first principles here
        # instead of fitted from v3's live ledger, since this is a
        # different universe/venue): p*(upper-1) + (1-p)*lower == 0.
        # TIMEOUT is deliberately treated as a wash (0) for this derivation
        # -- it is neither the win nor the loss barrier, and its real
        # expected value depends on the ret_at_horizon distribution, which
        # the online model itself is what's trying to learn.
        gain = upper_multiple - 1.0
        loss = -lower_pct
        self.breakeven_p = loss / (gain + loss) if (gain + loss) > 0 else 0.5
        self.min_probability = self.breakeven_p + probability_margin

        self.weights: Dict[str, float] = {name: 0.0 for name in self.feature_names}
        self.bias: float = 0.0
        self._stats: Dict[str, _RunningStat] = {name: _RunningStat() for name in self.feature_names}

        self.n_decisions: int = 0
        self.n_updates: int = 0
        self.rng = random.Random(seed)

    # -- exploration schedule -------------------------------------------

    @property
    def epsilon(self) -> float:
        """Starts at `epsilon_start`, decays toward `epsilon_floor` (never
        below it -- the policy always keeps SOME exploration, so it can
        keep noticing if the world changes, the same drift-awareness
        principle as `docs/PLAN.md`'s PSI drift gate)."""
        decayed = math.exp(-self.n_decisions / self.epsilon_decay_scale)
        return self.epsilon_floor + (self.epsilon_start - self.epsilon_floor) * decayed

    # -- feature vector ---------------------------------------------------

    def _update_running_stats(self, features: Dict[str, Optional[float]]) -> None:
        for name in self.feature_names:
            v = features.get(name)
            if v is not None:
                self._stats[name].update(float(v))

    def _vector(self, features: Dict[str, Optional[float]]) -> List[float]:
        """Standardized feature vector. A MISSING feature contributes
        exactly 0.0 to the dot product (neutral -- "no opinion from this
        feature right now"), never the raw value 0, matching this project's
        `None is not 0` discipline (`tape/schema.py`'s own module
        docstring) applied to a linear model's input rather than to a
        stored field."""
        out = []
        for name in self.feature_names:
            v = features.get(name)
            if v is None:
                out.append(0.0)
                continue
            stat = self._stats[name]
            out.append((float(v) - stat.mean) / stat.std)
        return out

    def _p_raw(self, x: Sequence[float]) -> float:
        z = self.bias + sum(w * xi for w, xi in zip(
            (self.weights[n] for n in self.feature_names), x))
        return _sigmoid(z)

    # -- decisions --------------------------------------------------------

    def decide(self, features: Dict[str, Optional[float]]) -> Dict:
        """One entry decision. Updates the running feature-standardization
        stats from every observation (entered or not), same as a live
        system would see every candidate whether or not it acts on it."""
        self._update_running_stats(features)
        x = self._vector(features)
        p_raw = self._p_raw(x)
        eps = self.epsilon
        explore = self.rng.random() < eps

        if explore:
            action = self.rng.choice(["enter", "abstain"])
        else:
            action = "enter" if p_raw >= self.min_probability else "abstain"

        idx = self.n_decisions
        self.n_decisions += 1
        return {
            "action": action,
            "p_raw": p_raw,
            "explore": explore,
            "epsilon_at_decision": eps,
            "decision_index": idx,
            "min_probability": self.min_probability,
        }

    def update(self, features: Dict[str, Optional[float]], y: int) -> None:
        """One SGD step on the logistic loss, using the CURRENT
        standardization stats (a documented approximation: by the time a
        decision resolves, the running mean/std may have moved slightly
        since the decision was made -- acceptable drift for an online
        model whose whole point is to keep adapting, not a lookahead,
        since only past-and-present observations ever feed the stats)."""
        if y not in (0, 1):
            raise ValueError(f"y must be 0 or 1, got {y!r}")
        x = self._vector(features)
        p = self._p_raw(x)
        grad = p - y  # d(logloss)/dz
        for name, xi in zip(self.feature_names, x):
            self.weights[name] -= self.learning_rate * grad * xi
        self.bias -= self.learning_rate * grad
        self.n_updates += 1

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> Dict:
        return {
            "version": MODEL_VERSION,
            "feature_names": self.feature_names,
            "upper_multiple": self.upper_multiple,
            "lower_pct": self.lower_pct,
            "probability_margin": self.probability_margin,
            "learning_rate": self.learning_rate,
            "epsilon_start": self.epsilon_start,
            "epsilon_floor": self.epsilon_floor,
            "epsilon_decay_scale": self.epsilon_decay_scale,
            "weights": self.weights,
            "bias": self.bias,
            "stats": {name: s.to_dict() for name, s in self._stats.items()},
            "n_decisions": self.n_decisions,
            "n_updates": self.n_updates,
        }

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @staticmethod
    def from_dict(d: Dict) -> "OnlinePolicy":
        pol = OnlinePolicy(
            feature_names=d["feature_names"],
            upper_multiple=d["upper_multiple"],
            lower_pct=d["lower_pct"],
            probability_margin=d["probability_margin"],
            learning_rate=d["learning_rate"],
            epsilon_start=d["epsilon_start"],
            epsilon_floor=d["epsilon_floor"],
            epsilon_decay_scale=d["epsilon_decay_scale"],
        )
        pol.weights = dict(d["weights"])
        pol.bias = d["bias"]
        pol._stats = {name: _RunningStat.from_dict(s) for name, s in d["stats"].items()}
        pol.n_decisions = d["n_decisions"]
        pol.n_updates = d["n_updates"]
        # NOTE: RNG internal state is deliberately NOT persisted. A restart
        # reseeds from a fresh (unseeded, i.e. OS-entropy) Random instance
        # by construction below -- reproducibility of *future* draws across
        # a restart is not the guarantee this makes; the guarantee is that
        # every decision actually taken is recorded in the paper-trade
        # ledger (see scripts/paper_trade_replay.py / paper_trade_live.py),
        # which is what an audit actually needs.
        pol.rng = random.Random()
        return pol

    @staticmethod
    def load(path: Path) -> "OnlinePolicy":
        return OnlinePolicy.from_dict(json.loads(Path(path).read_text()))


# ---------------------------------------------------------------------------
# Paper-trading safety rails -- a DELIBERATELY SMALLER set than
# `tape/policy.py::Rails`/`evaluate_rails`.
#
# `evaluate_rails` was designed for the fully-enriched system in
# `docs/PLAN.md` (liquidity in USD via a live SOL price feed, on-chain
# mint/freeze authority checks, RugCheck LP-lock data, a creator reputation
# graph). NONE of that enrichment is wired into this project's actual
# pump.fun on-chain-discovery pipeline (`discover_pumpfun_launches.py`,
# `backfill_discovered_launches.py`, `scripts/live_paper_monitor.py`) --
# verified by reading every one of those scripts: they produce swaps, bars
# and `TokenState.features()` only, never a `TokenMeta`/`Reputation` with
# those fields populated. Reusing `evaluate_rails` as-is here would reject
# on `missing_liquidity`/`missing_top_holder_pct`/etc. on every single
# decision, silently reducing "paper trading" to "always abstain" -- worse
# than not having a rail at all, because it would look like it's running.
#
# `PaperRails` below checks only the safety-relevant fields this pipeline
# ACTUALLY populates in `TokenState.features()`. It is a real, named scope
# reduction (D3 discipline: verified against what the code actually
# produces, not assumed), not a silent downgrade -- and it is still
# checked BEFORE the online policy gets a vote, same "rails can only ever
# reject, never be overridden by the model" principle as `evaluate_rails`.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PaperRails:
    min_age_ms: int = 5_000            # some real trading history must exist
    min_n_bars: int = 3                # enough bars for features to be non-degenerate
    min_liquidity: float = 0.0         # quote-denominated (SOL), NOT USD -- no USD feed wired
    min_unique_buyers_10: float = 2.0
    max_largest_buyer_share_10: float = 0.9


def evaluate_paper_rails(features: Dict[str, Optional[float]], rails: PaperRails
                         ) -> Optional[Tuple[str, str]]:
    """Returns (reason, layer) on rejection, or None if every rail passes.
    Every `is None` check is deliberate -- unmeasured is not safe."""
    n_bars = features.get("n_bars")
    if n_bars is None or n_bars < rails.min_n_bars:
        return ("not_enough_bars", "rail")

    age = features.get("age_ms")
    if age is None:
        return ("missing_age", "rail")
    if age < rails.min_age_ms:
        return ("token_too_young", "rail")

    # D86 (docs/DECISIONS.md): `liquidity` is confirmed -- via D85's real
    # rejection-reason tally, 65/65 tokens in the real corpus -- to be
    # unmeasured for every token this pipeline collects (Helius never
    # populates `quote_reserve_after`, so `TokenState.features()["liquidity"]`
    # is always None here). Hard-rejecting on that would silently reduce
    # "paper trading" to "always abstain" -- exactly the failure mode this
    # module's own docstring warns `evaluate_rails` would cause. So this
    # check now tolerates a missing reading, same pattern already used below
    # for `largest_buyer_share_10`: enforce the floor only when a real
    # reading exists, never reject solely because the reading is absent.
    liq = features.get("liquidity")
    if liq is not None and liq < rails.min_liquidity:
        return ("liquidity_below_floor", "rail")

    ub = features.get("unique_buyers_10")
    if ub is None:
        return ("missing_buyer_breadth", "rail")
    if ub < rails.min_unique_buyers_10:
        return ("too_few_unique_buyers", "rail")

    lbs = features.get("largest_buyer_share_10")
    if lbs is not None and lbs > rails.max_largest_buyer_share_10:
        return ("single_wallet_dominates_buying", "rail")

    return None
