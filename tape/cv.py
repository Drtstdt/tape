"""Purged, embargoed, grouped, chronological splits.

All four words are load-bearing:

  grouped        a random split puts bar 400 of a token in train and bar 401 in
                 test. That is leakage wearing a validation score's clothes.
  chronological  you cannot train on October and test on September.
  purged         a training label whose window OVERLAPS the test period must be
                 dropped -- it knows what happened during the test.
  embargoed      a gap after the test period, so serial correlation cannot
                 carry information backwards into later training folds.

Report EVERY fold. A strategy whose mean is positive because one fold is
spectacular is a strategy that worked once. v1's eight tuned archetypes were
all negative; that is the bar being failed, and it is the right bar.
"""

from __future__ import annotations

from typing import Iterator, List, Sequence, Tuple

import numpy as np


def purged_group_time_series_split(
    t0: Sequence[int],
    t1: Sequence[int],
    groups: Sequence[str],
    n_splits: int = 5,
    embargo_frac: float = 0.01,
) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    """Yield (train_idx, test_idx).

    Parameters
    ----------
    t0, t1 : label start / resolution times, ms. `t1` is when the label became
             knowable -- NOT the horizon deadline. A label that resolved in ten
             seconds only needs ten seconds of purging.
    groups : mint per row. Test folds are cut on TIME, then any training row
             sharing a mint with the test fold is also dropped, because two
             bars of the same token are near-duplicates regardless of when they
             happened.
    """
    t0 = np.asarray(t0, dtype=np.int64)
    t1 = np.asarray(t1, dtype=np.int64)
    groups = np.asarray(groups)
    n = len(t0)
    if not (len(t1) == len(groups) == n):
        raise ValueError("length mismatch")
    if n_splits < 2:
        raise ValueError("need at least 2 splits")

    order = np.argsort(t0, kind="mergesort")
    span = int(t0[order[-1]] - t0[order[0]]) or 1
    embargo_ms = int(span * embargo_frac)

    edges = np.linspace(t0[order[0]], t0[order[-1]] + 1, n_splits + 1).astype(np.int64)

    for k in range(n_splits):
        lo, hi = edges[k], edges[k + 1]
        test_mask = (t0 >= lo) & (t0 < hi)
        if not test_mask.any():
            continue

        test_groups = set(groups[test_mask].tolist())
        test_lo = int(t0[test_mask].min())
        test_hi = int(t1[test_mask].max())

        # Purge: a training label whose [t0, t1] window intersects the test
        # window knows something about it.
        overlaps = (t1 >= test_lo) & (t0 <= test_hi)
        # Embargo: also drop anything starting just after the test window.
        embargoed = (t0 > test_hi) & (t0 <= test_hi + embargo_ms)
        # Group purge: never train on a mint that appears in test.
        same_group = np.isin(groups, list(test_groups))

        train_mask = ~(test_mask | overlaps | embargoed | same_group)
        train_idx = np.flatnonzero(train_mask)
        test_idx = np.flatnonzero(test_mask)
        if len(train_idx) == 0 or len(test_idx) == 0:
            continue
        yield train_idx, test_idx


def token_bootstrap(values: Sequence[float], groups: Sequence[str],
                    n_boot: int = 2000, seed: int = 0) -> Tuple[float, float]:
    """95% interval, resampling TOKENS not rows.

    The effective sample size is the number of mints, not the number of bars,
    and any report that does not say so is overstating its confidence by orders
    of magnitude. Print the mint count first rather than burying it.
    """
    rng = np.random.default_rng(seed)
    values = np.asarray(values, dtype=float)
    groups = np.asarray(groups)
    uniq = np.unique(groups)
    if len(uniq) < 2:
        return (float("nan"), float("nan"))

    by_group = {g: values[groups == g] for g in uniq}
    means = np.empty(n_boot)
    for b in range(n_boot):
        picked = rng.choice(uniq, size=len(uniq), replace=True)
        pool = np.concatenate([by_group[g] for g in picked])
        means[b] = pool.mean() if len(pool) else np.nan
    return float(np.nanpercentile(means, 2.5)), float(np.nanpercentile(means, 97.5))


def auc_cluster_bootstrap(scores: Sequence[float], y: Sequence[float],
                          groups: Sequence[str], auc_fn, n_boot: int = 2000,
                          seed: int = 0) -> Tuple[float, float]:
    """95% interval for AUC ITSELF, resampling tokens not rows.

    `token_bootstrap` above resamples a scalar already computed per group (a
    hit rate, say): that only works because a group mean of group means is
    still the right pooled mean. AUC is not that -- it depends on the joint
    ranking of scores pooled across every resampled token, so a replicate
    must resample tokens with replacement, POOL the resampled tokens' rows
    back into one array (repeating a token's own rows in full every time it
    is drawn again), and recompute AUC on that pooled array. Recomputing AUC
    per token and averaging the per-token AUCs would silently discard the
    cross-token ranking the metric is defined on, and is a different (wrong)
    statistic wearing the same name.

    `auc_fn(scores, y) -> float` is injected rather than imported, so this
    stays a generic CV utility with no dependency on the audit script's rank
    formula.
    """
    rng = np.random.default_rng(seed)
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y, dtype=float)
    groups = np.asarray(groups)
    uniq = np.unique(groups)
    if len(uniq) < 2:
        return (float("nan"), float("nan"))

    idx_by_group = {g: np.flatnonzero(groups == g) for g in uniq}
    vals = np.empty(n_boot)
    for b in range(n_boot):
        picked = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([idx_by_group[g] for g in picked])
        vals[b] = auc_fn(scores[idx], y[idx])
    return float(np.nanpercentile(vals, 2.5)), float(np.nanpercentile(vals, 97.5))


def paired_token_bootstrap(diff: Sequence[float], groups: Sequence[str],
                           n_boot: int = 2000, seed: int = 0) -> Tuple[float, float]:
    """Interval on a PAIRED difference (arm minus baseline on the SAME row).

    Pairing cancels the token's own fortune, so these intervals are far tighter
    than comparing two level means -- and comparing level means across
    different exit policies compares two populations, not two policies. That
    mistake is what produced v3's 'time_stop is the whole deficit' reading,
    which the paired ablation then refuted.
    """
    return token_bootstrap(diff, groups, n_boot=n_boot, seed=seed)


def fold_report(fold_scores: List[float], name: str = "metric") -> str:
    lines = [f"{name} per fold:"]
    for i, s in enumerate(fold_scores):
        lines.append(f"  fold {i}: {s:+.4f}")
    if fold_scores:
        pos = sum(1 for s in fold_scores if s > 0)
        lines.append(f"  mean {np.mean(fold_scores):+.4f}  "
                     f"min {min(fold_scores):+.4f}  "
                     f"positive folds {pos}/{len(fold_scores)}")
        lines.append("  GATE: every fold positive -> "
                     + ("PASS" if pos == len(fold_scores) else "FAIL"))
    return "\n".join(lines)
