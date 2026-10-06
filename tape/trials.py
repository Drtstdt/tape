"""D127 (docs/DECISIONS.md) -- statistics for registered single-feature trials.

Pre-registered evaluation of one feature on the discovery zone (one row per
token, chronological):
  * rho      Spearman(feature, net return) on all discovery rows
  * p_raw    permutation p of |rho| (net shuffled WITHIN chronological blocks)
  * p_fwer   family-wise p: share of permutations whose MAX |rho| over the
             whole family >= this trial's |rho| (max-statistic null)
  * blocks   sign of rho in each of B equal-count chronological blocks
  * rule     direction and quintile threshold fitted on the FIRST half only;
             'enter iff feature in the favourable quintile' applied to the
             SECOND half: mean net of the selected vs mean net of all rows,
             day-block bootstrap CI of the difference and of the selected mean
"""

from __future__ import annotations

from typing import Dict, List, Sequence

import numpy as np

DAY_MS = 86_400_000


def rankdata(x: np.ndarray) -> np.ndarray:
    """Average ranks (ties averaged), NaN-free input."""
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x), dtype=float)
    r[order] = np.arange(1, len(x) + 1)
    _, inv, cnt = np.unique(x, return_inverse=True, return_counts=True)
    s = np.zeros(len(cnt))
    np.add.at(s, inv, r)
    return (s / cnt)[inv]


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 10:
        return float("nan")
    a, b = rankdata(x[m]), rankdata(y[m])
    a, b = a - a.mean(), b - b.mean()
    den = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / den) if den > 0 else float("nan")


def block_ids(ts: np.ndarray, n_blocks: int) -> np.ndarray:
    """Equal-count chronological blocks (ts must be the chronological key)."""
    order = np.argsort(ts, kind="mergesort")
    ids = np.empty(len(ts), dtype=int)
    ids[order] = np.minimum((np.arange(len(ts)) * n_blocks) // max(len(ts), 1), n_blocks - 1)
    return ids


def permutation_null(features: Dict[str, np.ndarray], y: np.ndarray, blocks: np.ndarray,
                     n_perm: int = 1000, seed: int = 0, progress=None) -> Dict[str, np.ndarray]:
    """|rho| of every feature under n_perm within-block shuffles of y. ONE common
    permutation per replicate for all features (needed for the max-statistic
    null), on the rows where y and every feature are finite."""
    rng = np.random.default_rng(seed)
    m = np.isfinite(y)
    for x in features.values():
        m &= np.isfinite(x)

    def z(v):
        r = rankdata(v)
        r = r - r.mean()
        return r / np.sqrt((r * r).sum())

    A = {k: z(x[m]) for k, x in features.items()}
    b = z(y[m])
    bl = blocks[m]
    groups = [np.flatnonzero(bl == g) for g in np.unique(bl)]
    out = {k: np.empty(n_perm) for k in features}
    for p in range(n_perm):
        bp = np.empty_like(b)
        for g in groups:
            bp[g] = b[g][rng.permutation(len(g))]
        for k, a in A.items():
            out[k][p] = abs(float(a @ bp))
        if progress and (p + 1) % 100 == 0:
            progress(p + 1)
    out["_n_rows"] = np.array([int(m.sum())])
    return out


def quintile_rule(x: np.ndarray, y: np.ndarray, first: np.ndarray, q: float = 0.2) -> Dict[str, float]:
    """Direction + threshold from rows where `first`; evaluated on ~first."""
    xf, yf = x[first], y[first]
    ok = np.isfinite(xf) & np.isfinite(yf)
    rho1 = spearman(xf[ok], yf[ok])
    direction = 1.0 if (rho1 == rho1 and rho1 >= 0) else -1.0
    thr = np.nanquantile(xf[ok], 1 - q) if direction > 0 else np.nanquantile(xf[ok], q)
    xs, ys = x[~first], y[~first]
    sel = (xs >= thr) if direction > 0 else (xs <= thr)
    sel &= np.isfinite(xs)
    return {"rho_first": rho1, "direction": direction, "threshold": float(thr),
            "n_second": int(np.isfinite(ys).sum()), "n_selected": int(sel.sum()),
            "mean_all_second": float(np.nanmean(ys)), "mean_sel_second": float(np.nanmean(ys[sel])) if sel.any() else float("nan"),
            "sel_mask_second": sel}


def day_block_bootstrap(y: np.ndarray, sel: np.ndarray, day: np.ndarray, n_boot: int = 2000,
                        seed: int = 0) -> Dict[str, List[float]]:
    """CI (2.5/97.5%) of mean(y[sel]) and of mean(y[sel]) - mean(y), resampling DAYS."""
    rng = np.random.default_rng(seed)
    days = np.unique(day)
    idx_by_day = [np.flatnonzero(day == d) for d in days]
    ms, ds = [], []
    for _ in range(n_boot):
        pick = rng.integers(0, len(days), len(days))
        idx = np.concatenate([idx_by_day[i] for i in pick])
        yy, ss = y[idx], sel[idx]
        if ss.any():
            ms.append(np.nanmean(yy[ss]))
            ds.append(np.nanmean(yy[ss]) - np.nanmean(yy))
    return {"sel_mean_ci": [float(np.quantile(ms, .025)), float(np.quantile(ms, .975))],
            "diff_ci": [float(np.quantile(ds, .025)), float(np.quantile(ds, .975))]}


# ---------------------------------------------------------------------------
# D129: control for the curve-position factor (D128/T004)
# ---------------------------------------------------------------------------

def _resid(r: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Residual of r after OLS on c (both already ranks), standardised to unit norm."""
    c0 = c - c.mean()
    beta = (c0 @ (r - r.mean())) / (c0 @ c0) if (c0 @ c0) > 0 else 0.0
    e = (r - r.mean()) - beta * c0
    nrm = np.sqrt(e @ e)
    return e / nrm if nrm > 0 else e


def partial_spearman(x: np.ndarray, y: np.ndarray, c: np.ndarray) -> float:
    """Spearman correlation of x and y after removing the (rank-)linear effect of c."""
    m = np.isfinite(x) & np.isfinite(y) & np.isfinite(c)
    if m.sum() < 10:
        return float("nan")
    rc = rankdata(c[m])
    return float(_resid(rankdata(x[m]), rc) @ _resid(rankdata(y[m]), rc))


def partial_permutation_null(features: Dict[str, np.ndarray], y: np.ndarray, c: np.ndarray,
                             blocks: np.ndarray, n_perm: int = 1000, seed: int = 0,
                             progress=None) -> Dict[str, object]:
    """Partial association of each feature with y, controlling for c, and its
    max-statistic permutation null. Statistic (identical for observed and null):
    Pearson correlation of the c-residualised x-rank and the c-residualised
    GLOBAL y-rank, on the rows where x, y and c are finite (each feature keeps
    its own rows -- NaN means 'not measurable here'). Null: ONE within-block
    permutation of y per replicate, shared by every feature."""
    rng = np.random.default_rng(seed)
    base = np.isfinite(y) & np.isfinite(c)
    ry = np.full(len(y), np.nan)
    ry[base] = rankdata(y[base])
    prep = {}
    obs = {}
    for k, x in features.items():
        m = base & np.isfinite(x)
        if m.sum() < 30:
            continue
        rc = rankdata(c[m])
        ex = _resid(rankdata(x[m]), rc)
        prep[k] = (m, rc, ex)
        obs[k] = float(ex @ _resid(ry[m], rc))
    idx_all = np.flatnonzero(base)
    groups = [idx_all[blocks[idx_all] == g] for g in np.unique(blocks[idx_all])]
    out: Dict[str, object] = {k: np.empty(n_perm) for k in prep}
    for p in range(n_perm):
        ryp = ry.copy()
        for g in groups:
            ryp[g] = ry[g][rng.permutation(len(g))]
        for k, (m, rc, ex) in prep.items():
            out[k][p] = abs(float(ex @ _resid(ryp[m], rc)))
        if progress and (p + 1) % 100 == 0:
            progress(p + 1)
    out["_obs"] = obs
    out["_n"] = {k: int(v[0].sum()) for k, v in prep.items()}
    return out


def matched_diff(y: np.ndarray, sel: np.ndarray, strata: np.ndarray) -> float:
    """mean(y[sel]) minus the mean of y over ALL rows of the same strata, weighted
    by how the selection is spread over the strata (curve-position-matched baseline)."""
    if not sel.any():
        return float("nan")
    base = 0.0
    for s in np.unique(strata[sel]):
        w = np.mean(strata[sel] == s)
        base += w * np.nanmean(y[strata == s])
    return float(np.nanmean(y[sel]) - base)


def matched_bootstrap(y: np.ndarray, sel: np.ndarray, strata: np.ndarray, day: np.ndarray,
                      n_boot: int = 2000, seed: int = 0) -> List[float]:
    rng = np.random.default_rng(seed)
    days = np.unique(day)
    idx_by_day = [np.flatnonzero(day == d) for d in days]
    vals = []
    for _ in range(n_boot):
        idx = np.concatenate([idx_by_day[i] for i in rng.integers(0, len(days), len(days))])
        v = matched_diff(y[idx], sel[idx], strata[idx])
        if v == v:
            vals.append(v)
    return [float(np.quantile(vals, .025)), float(np.quantile(vals, .975))]


def two_sample_day_bootstrap(y_a: np.ndarray, day_a: np.ndarray, y_b: np.ndarray, day_b: np.ndarray,
                             n_boot: int = 2000, seed: int = 0) -> Dict[str, object]:
    """D136: mean(y_a) - mean(y_b) for two (possibly overlapping) samples that share
    a calendar; DAYS are resampled jointly (a day drawn brings its rows of both
    samples). Returns diff, 95% CI and the share of resampled diffs <= 0."""
    rng = np.random.default_rng(seed)
    days = np.union1d(np.unique(day_a), np.unique(day_b))
    ia = [np.flatnonzero(day_a == d) for d in days]
    ib = [np.flatnonzero(day_b == d) for d in days]
    diffs = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(days), len(days))
        xa = np.concatenate([ia[i] for i in pick])
        xb = np.concatenate([ib[i] for i in pick])
        if len(xa) and len(xb):
            diffs.append(np.nanmean(y_a[xa]) - np.nanmean(y_b[xb]))
    diffs = np.array(diffs)
    return {"diff": float(np.nanmean(y_a) - np.nanmean(y_b)),
            "ci": [float(np.quantile(diffs, .025)), float(np.quantile(diffs, .975))],
            "p_le0": float((1 + np.sum(diffs <= 0)) / (1 + len(diffs)))}
