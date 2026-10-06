"""Labels. This module MUST NOT import tape.features, and features must not
import this. The separation is the only reliable defence against look-ahead:
a leak does not raise, does not fail a type check and does not look wrong in a
diff -- it shows up as a backtest better than reality, which is the failure
mode that gets acted on instead of investigated. Making the wrong data
physically absent from the function's scope is what actually prevents it.

`tests/test_layering.py` asserts the two modules share no imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

UP, DOWN, TIMEOUT = 1, 0, -1


@dataclass(slots=True)
class BarrierConfig:
    upper_multiple: float       # e.g. 1.8 -> take-profit barrier
    lower_pct: float            # e.g. -0.35 -> stop barrier (negative)
    horizon_ms: int

    def __post_init__(self) -> None:
        if self.upper_multiple <= 1.0:
            raise ValueError("upper_multiple must exceed 1.0")
        if not -1.0 < self.lower_pct < 0.0:
            raise ValueError("lower_pct must be negative and above -1.0")


@dataclass(slots=True)
class Label:
    mint: str
    bar_index: int
    t0_ms: int
    t1_ms: int                  # when the label actually resolved
    outcome: int                # UP | DOWN | TIMEOUT
    y: int                      # binary target: 1 iff outcome == UP
    mfe_pct: float
    mae_pct: float
    ret_at_horizon_pct: Optional[float]
    truncated: bool             # tape ended before the horizon


def triple_barrier(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    ts_ms: Sequence[int],
    i: int,
    cfg: BarrierConfig,
    mint: str = "",
) -> Label:
    """Label bar `i`, looking forward only.

    Entry price is bar i's CLOSE -- you cannot trade a bar you have not seen
    close. Using the open, or the high, silently buys information you did not
    have.

    When both barriers fall inside one bar, the label resolves DOWN. Bars carry
    no intra-bar path, and a labeller that breaks ties in its own favour
    produces a backtest nobody can reproduce live.
    """
    n = len(closes)
    if not (len(highs) == len(lows) == len(ts_ms) == n):
        raise ValueError("series length mismatch")
    if not 0 <= i < n:
        raise IndexError(i)

    entry = closes[i]
    if entry <= 0:
        raise ValueError("non-positive entry price")

    up_px = entry * cfg.upper_multiple
    dn_px = entry * (1.0 + cfg.lower_pct)
    deadline = ts_ms[i] + cfg.horizon_ms

    mfe = mae = 0.0
    last_close_in_window = None
    reached_deadline = False

    for j in range(i + 1, n):
        if ts_ms[j] > deadline:
            reached_deadline = True
            break
        last_close_in_window = closes[j]
        mfe = max(mfe, highs[j] / entry - 1.0)
        mae = min(mae, lows[j] / entry - 1.0)

        hit_dn = lows[j] <= dn_px
        hit_up = highs[j] >= up_px
        if hit_dn:                       # ambiguity resolves DOWN, always
            return Label(mint, i, ts_ms[i], ts_ms[j], DOWN, 0, mfe, mae,
                         last_close_in_window / entry - 1.0, False)
        if hit_up:
            return Label(mint, i, ts_ms[i], ts_ms[j], UP, 1, mfe, mae,
                         last_close_in_window / entry - 1.0, False)

    truncated = not reached_deadline
    t1 = ts_ms[-1] if truncated else deadline
    ret = None if last_close_in_window is None else last_close_in_window / entry - 1.0
    return Label(mint, i, ts_ms[i], t1, TIMEOUT, 0, mfe, mae, ret, truncated)


def label_series(highs, lows, closes, ts_ms, cfg: BarrierConfig,
                 mint: str = "", stride: int = 1) -> List[Label]:
    return [triple_barrier(highs, lows, closes, ts_ms, i, cfg, mint)
            for i in range(0, len(closes) - 1, stride)]


# ---------------------------------------------------------------------------
# Sample weighting
# ---------------------------------------------------------------------------

def thin_non_overlapping(labels: Sequence[Label]) -> List[int]:
    """Indices (into `labels`, ascending) of a greedy maximal subset whose
    [t0, t1] windows never overlap.

    Call this per token: overlap across two DIFFERENT tokens is meaningless
    (they are independent tapes), so mixing mints into one call here would
    silently drop labels for no real reason. Sorted by t0, a label is kept
    iff its t0 is at or after the previously kept label's t1 -- greedy and
    optimal for "maximum count of disjoint intervals" (the classical interval
    scheduling result), which is what a stride-based series needs: the
    default `stride` argument to `label_series` overlaps consecutive labels
    almost completely, so the kept subset is what a `--non-overlap` run
    should evaluate on to get observations closer to independent.
    """
    order = sorted(range(len(labels)), key=lambda k: labels[k].t0_ms)
    kept: List[int] = []
    last_t1 = None
    for k in order:
        lab = labels[k]
        if last_t1 is None or lab.t0_ms >= last_t1:
            kept.append(k)
            last_t1 = lab.t1_ms
    return sorted(kept)


def average_uniqueness(labels: Sequence[Label]) -> List[float]:
    """Weight each label by how little it overlaps the others.

    Labels from overlapping windows are not independent observations. A token
    with 3,000 bars contributes 3,000 rows that are nearly the same observation
    repeated; treating them as independent turns noise into a publishable
    p-value and is the single most common way a crypto backtest lies.

    Concurrency is counted on the label boundaries rather than on a fixed grid,
    so cost is O(n log n) and does not depend on tape length in milliseconds.
    """
    if not labels:
        return []

    events = []
    for k, lab in enumerate(labels):
        t1 = max(lab.t1_ms, lab.t0_ms + 1)
        events.append((lab.t0_ms, 1, k))
        events.append((t1, -1, k))
    events.sort(key=lambda e: (e[0], -e[1]))

    # Sweep: integrate 1/concurrency over each label's own span.
    active: set = set()
    integral = {k: 0.0 for k in range(len(labels))}
    prev_t = events[0][0]
    for t, delta, k in events:
        if t > prev_t and active:
            width = t - prev_t
            inv = width / len(active)
            for a in active:
                integral[a] += inv
        prev_t = t
        if delta == 1:
            active.add(k)
        else:
            active.discard(k)

    out = []
    for k, lab in enumerate(labels):
        span = max(lab.t1_ms - lab.t0_ms, 1)
        out.append(min(1.0, integral[k] / span))
    return out


def return_attribution_weights(labels: Sequence[Label]) -> List[float]:
    """Weight also by the absolute move the label spans, so a bar that led
    somewhere counts for more than one that led nowhere."""
    return [abs(l.ret_at_horizon_pct) if l.ret_at_horizon_pct is not None else 0.0
            for l in labels]


def sample_weights(labels: Sequence[Label]) -> List[float]:
    """Uniqueness x return attribution, normalised to mean 1."""
    u = average_uniqueness(labels)
    r = return_attribution_weights(labels)
    w = [a * b for a, b in zip(u, r)]
    total = sum(w)
    if total <= 0:
        return [1.0] * len(labels)
    scale = len(w) / total
    return [x * scale for x in w]
