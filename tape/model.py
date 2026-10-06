"""Train -> calibrate -> conformalise -> persist.

The three outputs, and why each exists:

  p_raw        the model's score. Ranks well, means nothing numerically.
  p_calibrated isotonic-corrected. When it says 35%, it wins 35% of the time.
               This is the ONLY output you may size against. An uncalibrated
               model can have a perfect AUC and still be useless for the only
               thing you want it for.
  credibility  inductive conformal, Mondrian (per band). How TYPICAL this
               observation is relative to what the model was fitted on. A token
               in an unseen regime gets low credibility and is not traded even
               when p_calibrated is high.

That third number is the mechanism that makes the bot stop when it does not
know. v3 has no way to express it, which is most of the difference between
"confident" and "swinging in the fog".
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np


@dataclass
class ModelArtifact:
    """Everything needed to reproduce a decision, six months later."""

    booster: object                                  # LightGBM Booster or sklearn estimator
    feature_names: List[str]
    calibrator: object                               # IsotonicRegression
    conformal_scores: Dict[str, np.ndarray]          # band -> sorted nonconformity of the calibration set
    train_medians: Dict[str, float]                  # for PSI drift baselines
    train_quantiles: Dict[str, np.ndarray]
    meta: Dict = field(default_factory=dict)         # run id, corpus, gates passed, dates

    # -- inference ------------------------------------------------------

    def _vector(self, features: Dict[str, Optional[float]]) -> np.ndarray:
        # Missing stays NaN. LightGBM handles it natively and learns a
        # direction for it, which is strictly better than imputing a value the
        # feature never takes. Never impute 0 for "unmeasured" -- see schema.py.
        return np.array([[_nan(features.get(n)) for n in self.feature_names]], dtype=float)

    def predict(self, features: Dict[str, Optional[float]], band: str = "_all") -> dict:
        x = self._vector(features)
        raw = float(_predict_proba(self.booster, x)[0])
        cal = float(self.calibrator.predict([raw])[0])
        cred = self.credibility(raw, band)
        return {"p_raw": raw, "p_calibrated": cal, "credibility": cred}

    def credibility(self, p_raw: float, band: str = "_all") -> float:
        """Fraction of calibration-set nonconformity scores at least as strange
        as this one. Near 0 means "I have not seen this before".

        Distribution-free: the guarantee holds under exchangeability alone, no
        assumption about the model or the feature distribution.
        """
        scores = self.conformal_scores.get(band)
        if scores is None:
            scores = self.conformal_scores.get("_all")
        if scores is None or len(scores) == 0:
            return 0.0
        alpha = _nonconformity(p_raw)
        return float((scores >= alpha).sum() + 1) / float(len(scores) + 1)

    # -- persistence ----------------------------------------------------

    def save(self, path: Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        with open(path / "artifact.pkl", "wb") as fh:
            pickle.dump(self, fh)
        # A human-readable sidecar, so a model can be audited without
        # unpickling it. An artifact nobody can read acquires authority by
        # being old; this is the antidote.
        (path / "meta.json").write_text(json.dumps({
            "feature_names": self.feature_names,
            "n_features": len(self.feature_names),
            "bands": sorted(self.conformal_scores.keys()),
            "meta": self.meta,
        }, indent=2))

    @staticmethod
    def load(path: Path) -> "ModelArtifact":
        with open(Path(path) / "artifact.pkl", "rb") as fh:
            return pickle.load(fh)


def _nan(v) -> float:
    return float("nan") if v is None else float(v)


def _nonconformity(p: float) -> float:
    """Distance from a confident prediction. Bigger = stranger."""
    return 1.0 - abs(2.0 * p - 1.0)


def _predict_proba(booster, x: np.ndarray) -> np.ndarray:
    if hasattr(booster, "predict_proba"):
        return booster.predict_proba(x)[:, 1]
    return np.asarray(booster.predict(x)).ravel()


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: Sequence[str],
    sample_weight: Optional[np.ndarray] = None,
    bands: Optional[Sequence[str]] = None,
    calibration_frac: float = 0.2,
    params: Optional[dict] = None,
    seed: int = 0,
) -> ModelArtifact:
    """Fit on the first (1-calibration_frac) chronologically, calibrate and
    conformalise on the tail.

    The calibration slice is held out CHRONOLOGICALLY, never randomly. Random
    would put a token's own future in the set used to decide how much to trust
    predictions about it.
    """
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression

    n = len(y)
    cut = int(n * (1.0 - calibration_frac))
    if cut < 50 or n - cut < 50:
        raise ValueError(f"not enough rows to fit and calibrate (n={n})")

    p = {
        "objective": "binary",
        "learning_rate": 0.03,
        "num_leaves": 31,
        "min_data_in_leaf": 200,      # heavy regularisation: the event is rare
        "feature_fraction": 0.7,
        "bagging_fraction": 0.7,
        "bagging_freq": 1,
        "lambda_l2": 5.0,
        "verbosity": -1,
        "seed": seed,
    }
    p.update(params or {})

    dtrain = lgb.Dataset(
        X[:cut], label=y[:cut], feature_name=list(feature_names),
        weight=None if sample_weight is None else sample_weight[:cut],
    )
    booster = lgb.train(p, dtrain, num_boost_round=400)

    raw_cal = np.asarray(booster.predict(X[cut:])).ravel()
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(raw_cal, y[cut:])

    # Mondrian conformal: one calibration distribution per band, so a rare band
    # cannot borrow confidence from a common one.
    conformal: Dict[str, np.ndarray] = {}
    alphas = np.array([_nonconformity(v) for v in raw_cal])
    conformal["_all"] = np.sort(alphas)
    if bands is not None:
        b = np.asarray(bands)[cut:]
        for band in np.unique(b):
            conformal[str(band)] = np.sort(alphas[b == band])

    med = {name: float(np.nanmedian(X[:cut, i])) for i, name in enumerate(feature_names)}
    qs = {name: np.nanquantile(X[:cut, i], np.linspace(0, 1, 11))
          for i, name in enumerate(feature_names)}

    return ModelArtifact(
        booster=booster, feature_names=list(feature_names), calibrator=iso,
        conformal_scores=conformal, train_medians=med, train_quantiles=qs,
        meta={"n_train": int(cut), "n_calibration": int(n - cut),
              "base_rate": float(y.mean())},
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def reliability_curve(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> List[dict]:
    """The number that matters: when it says 35%, does it win 35%?

    Report this every retrain. AUC is not a substitute -- a model can rank
    perfectly and be systematically overconfident, which produces oversized
    positions on exactly the trades it is most wrong about.
    """
    edges = np.quantile(p, np.linspace(0, 1, n_bins + 1))
    edges[-1] += 1e-9
    out = []
    for i in range(n_bins):
        m = (p >= edges[i]) & (p < edges[i + 1])
        if m.sum() < 5:
            continue
        out.append({
            "bin": i,
            "n": int(m.sum()),
            "predicted": float(p[m].mean()),
            "observed": float(y[m].mean()),
            "gap": float(y[m].mean() - p[m].mean()),
        })
    return out


def decile_hit_rate(p: np.ndarray, y: np.ndarray) -> dict:
    """The gate for Stage 2. Top decile must clear the breakeven win rate."""
    order = np.argsort(-p)
    k = max(1, len(p) // 10)
    top = order[:k]
    return {
        "top_decile_n": int(k),
        "top_decile_hit_rate": float(y[top].mean()),
        "base_rate": float(y.mean()),
        "lift": float(y[top].mean() / y.mean()) if y.mean() > 0 else float("nan"),
    }


def psi(baseline_quantiles: np.ndarray, current: np.ndarray) -> float:
    """Population Stability Index against the training distribution.

    v1 saw PSI 0.80 within 48 hours -- a model predicting a world that no
    longer exists. Above 0.2 halve size; above 0.3 stop and retrain.
    """
    cur = current[~np.isnan(current)]
    if len(cur) == 0:
        return float("nan")
    edges = np.unique(baseline_quantiles)
    if len(edges) < 3:
        return 0.0
    base_hist, _ = np.histogram(baseline_quantiles, bins=edges)
    cur_hist, _ = np.histogram(cur, bins=edges)
    b = np.clip(base_hist / max(base_hist.sum(), 1), 1e-6, None)
    c = np.clip(cur_hist / max(cur_hist.sum(), 1), 1e-6, None)
    return float(((c - b) * np.log(c / b)).sum())
