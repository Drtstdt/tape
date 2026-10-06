"""Parquet -> bars -> features -> labels -> calibrated + conformal artifact,
one per band.

The pipeline is exactly the research pipeline (docs/ML.md), reused:

    swaps -> sanity filters -> dollar bars -> TokenState features ->
    triple-barrier labels (band barriers) -> uniqueness x attribution
    weights -> purged/grouped/chronological folds (report EVERY fold) ->
    LightGBM -> isotonic calibration -> conformal scores -> artifact.

Gate discipline (docs/PLAN.md Sec 5): a band's top-decile hit rate must
clear its breakeven win rate in at least 4 of 5 folds before its artifact
is even SAVED. Report every fold -- a model that works in one fold is
noise. The artifact still ships with `meta["status"] = "unvalidated"` until
the fuller gate suite (live paper trading, calibration check) clears.

Costs: feature computation is the long pole; run with --limit and grow it.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from ..bars import BarBuilder, band_bar_threshold
from ..features import TokenState
from ..labels import (BarrierConfig, Label, average_uniqueness,
                      return_attribution_weights, triple_barrier)
from ..model import ModelArtifact, decile_hit_rate, reliability_curve, train
from ..sanity import filter_implausible_swaps, filter_post_migration_swaps
from ..schema import CanonicalSwap
from .spec import BandSpec, BotSpec

MIN_SWAPS = 30


def _bar_threshold(swaps: Sequence[CanonicalSwap], spec: BotSpec) -> float:
    depth = next((s.quote_reserve_after for s in swaps if s.quote_reserve_after), None)
    if not depth:
        return spec.bar_floor_quote
    return band_bar_threshold(depth, spec.bar_fraction_of_liquidity,
                              spec.bar_floor_quote)


def mint_series(swaps: Sequence[CanonicalSwap], spec: BotSpec):
    """(feats_by_bar, highs, lows, closes, ts) for one mint's tape.
    Sanity filters applied first (price implausibility, post-migration cut)."""
    swaps, _ = filter_implausible_swaps(list(swaps))
    swaps, _ = filter_post_migration_swaps(swaps)
    if len(swaps) < MIN_SWAPS:
        return None
    bb = BarBuilder("dollar", threshold=_bar_threshold(swaps, spec))
    st = TokenState(swaps[0].mint, created_ts_ms=swaps[0].ts_ms)
    feats: List[Dict] = []
    highs, lows, closes, ts = [], [], [], []
    for s in swaps:
        bar = bb.push(s)
        if bar is None:
            continue
        st.update(bar)
        feats.append(dict(st.features()))
        highs.append(bar.high)
        lows.append(bar.low)
        closes.append(bar.close)
        ts.append(bar.close_ts_ms)
    if not feats:
        return None
    return feats, highs, lows, closes, ts


def band_rows(series, band: BandSpec, stride_target: int = 60) -> Optional[Dict]:
    """Labels + weights for one mint under one band's barriers, thinned to
    ~`stride_target` rows per token (overlapping labels are down-weighted
    anyway, but a 3,000-bar token must not contribute 3,000 near-identical
    rows)."""
    feats, highs, lows, closes, ts = series
    n = len(closes)
    stride = max(1, n // max(stride_target, 1))
    cfg = BarrierConfig(band.upper_multiple, band.lower_pct, band.horizon_ms)
    labels: List[Label] = []
    idx: List[int] = []
    for i in range(0, n - 1, stride):
        labels.append(triple_barrier(highs, lows, closes, ts, i, cfg))
        idx.append(i)
    if not labels:
        return None
    uniq = average_uniqueness(labels)
    attr = return_attribution_weights(labels)
    weights = [a * b for a, b in zip(uniq, attr)]
    total = sum(weights)
    if total <= 0:
        weights = [1.0] * len(labels)
    else:
        weights = [w * len(weights) / total for w in weights]
    usable = [k for k, lab in enumerate(labels) if not lab.truncated]
    if not usable:
        return None
    return {
        "feats": [feats[idx[k]] for k in usable],
        "y": np.array([labels[k].y for k in usable], dtype=float),
        "w": np.array([weights[k] for k in usable], dtype=float),
        "t0": np.array([labels[k].t0_ms for k in usable], dtype=np.int64),
        "t1": np.array([labels[k].t1_ms for k in usable], dtype=np.int64),
    }


def build_band_dataset(swaps_by_mint: Dict[str, Sequence[CanonicalSwap]],
                       spec: BotSpec, band: BandSpec,
                       stride_target: int = 60,
                       log=print) -> Optional[Dict]:
    """All usable rows for one band. Returns None when there is nothing to
    train on (honest: an empty corpus must be reported, not crashed)."""
    t0 = time.time()
    feats_rows: List[Dict] = []
    ys: List[float] = []
    ws: List[float] = []
    groups: List[str] = []
    t0s: List[int] = []
    t1s: List[int] = []
    n_mints = n_skipped = 0
    for mint, swaps in swaps_by_mint.items():
        series = mint_series(swaps, spec)
        if series is None:
            n_skipped += 1
            continue
        row = band_rows(series, band, stride_target=stride_target)
        if row is None:
            n_skipped += 1
            continue
        n_mints += 1
        feats_rows.extend(row["feats"])
        ys.extend(row["y"].tolist())
        ws.extend(row["w"].tolist())
        groups.extend([mint] * len(row["y"]))
        t0s.extend(row["t0"].tolist())
        t1s.extend(row["t1"].tolist())
        if n_mints % 200 == 0:
            log(f"  [train:{band.name}] {n_mints} mints, "
                f"{len(ys)} rows ({time.time() - t0:.0f}s)")
    if not ys:
        log(f"  [train:{band.name}] NOTHING to train on "
            f"({n_skipped} mints skipped).")
        return None
    return {"feats": feats_rows, "y": np.array(ys, dtype=float),
            "w": np.array(ws, dtype=float), "groups": np.array(groups),
            "t0": np.array(t0s, dtype=np.int64),
            "t1": np.array(t1s, dtype=np.int64),
            "n_mints": n_mints}


def _matrix(rows: Sequence[Dict], feature_names: Sequence[str]) -> np.ndarray:
    """NaN for missing -- LightGBM learns a direction for missing natively,
    which is strictly better than imputing a value the feature never takes
    (tape/model.py)."""
    return np.array(
        [[np.nan if r.get(n) is None else float(r.get(n)) for n in feature_names]
         for r in rows], dtype=float)


def fold_report(dataset: Dict, feature_names: Sequence[str], band: BandSpec,
                n_splits: int = 5, log=print) -> Dict:
    """The Stage-2 gate, per fold: top-decile hit rate vs the band's
    breakeven. Returns the per-fold numbers; nothing is selected on this."""
    from ..cv import purged_group_time_series_split

    X = _matrix(dataset["feats"], feature_names)
    y = dataset["y"]
    w = dataset["w"]
    folds = list(purged_group_time_series_split(dataset["t0"], dataset["t1"],
                                                dataset["groups"],
                                                n_splits=n_splits))
    if not folds:
        return {"folds": [], "passed": False, "n_folds": 0}

    import lightgbm as lgb

    params = {
        "objective": "binary", "learning_rate": 0.05, "num_leaves": 31,
        "min_data_in_leaf": 100, "feature_fraction": 0.7,
        "bagging_fraction": 0.7, "bagging_freq": 1, "lambda_l2": 5.0,
        "verbosity": -1, "seed": 0,
    }
    per_fold = []
    for k, (tr, te) in enumerate(folds):
        dtr = lgb.Dataset(X[tr], label=y[tr], weight=w[tr])
        bst = lgb.train(params, dtr, num_boost_round=200)
        p = bst.predict(X[te])
        per_fold.append(decile_hit_rate(p, y[te]))
    passes = sum(1 for f in per_fold
                 if f["top_decile_hit_rate"] >= band.breakeven_win_rate)
    log(f"  [gate:{band.name}] breakeven={band.breakeven_win_rate:.3f}")
    for k, f in enumerate(per_fold):
        log(f"    fold {k}: top-decile hit rate {f['top_decile_hit_rate']:.3f} "
            f"(n={f['top_decile_n']}, base {f['base_rate']:.3f}) -> "
            f"{'PASS' if f['top_decile_hit_rate'] >= band.breakeven_win_rate else 'FAIL'}")
    return {"folds": per_fold, "n_folds": len(per_fold),
            "passed": passes >= max(1, len(per_fold) - 1),
            "passes": passes}


def train_band(dataset: Dict, feature_names: Sequence[str], band: BandSpec,
               out_dir: Path, log=print) -> Optional[ModelArtifact]:
    """Train the artifact with tape/model.py's chronological calibration
    split, report reliability, save -- but ONLY if the fold gate passed.
    An artifact that fails its gate is not written to disk at all."""
    gate = fold_report(dataset, feature_names, band, log=log)
    if not gate["passed"]:
        log(f"  [train:{band.name}] GATE FAILED ({gate['passes']}/{gate['n_folds']} "
            f"folds above breakeven) -- artifact NOT saved. This is the "
            f"pre-registered kill criterion working as designed.")
        return None

    X = _matrix(dataset["feats"], feature_names)
    y = dataset["y"]
    w = dataset["w"]
    if len(y) < 150:
        log(f"  [train:{band.name}] only {len(y)} rows -- too few to fit + calibrate.")
        return None
    artifact = train(X, y, feature_names, sample_weight=w, seed=0,
                     calibration_frac=0.2)
    artifact.meta["band"] = band.name
    artifact.meta["n_mints"] = int(dataset["n_mints"])
    artifact.meta["status"] = "unvalidated"
    artifact.meta["gate"] = {k: gate[k] for k in ("n_folds", "passes", "passed")}

    p_cal = np.array([artifact.predict(
        {n: (float(r.get(n)) if r.get(n) is not None else None)
         for n in feature_names})["p_calibrated"] for r in dataset["feats"]])
    rel = reliability_curve(p_cal, y)
    log(f"  [train:{band.name}] reliability (predicted vs observed):")
    for b in rel:
        log(f"    bin {b['bin']:>2}  n={b['n']:<5} predicted {b['predicted']:.3f} "
            f"observed {b['observed']:.3f} gap {b['gap']:+.3f}")

    out = Path(out_dir) / band.name
    artifact.save(out)
    log(f"  [train:{band.name}] artifact saved to {out} "
        f"(n_train={artifact.meta['n_train']}, "
        f"n_cal={artifact.meta['n_calibration']})")
    return artifact


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--cache", default="E:\\tape_cache\\swaps_by_mint",
                    help="swaps cache dir; used when fresh, else the store")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--min-swaps", type=int, default=MIN_SWAPS)
    ap.add_argument("--stride-target", type=int, default=60)
    ap.add_argument("--out", default="models/bot")
    ap.add_argument("--spec", default="config/bot.yaml")
    ap.add_argument("--bands", default=None, help="comma list; default all")
    args = ap.parse_args()

    from ..swaps_cache import (check_cache_fresh, fetch_swaps,
                               open_cache_connection)
    from .spec import load_spec
    from .universe import cache_universe

    spec: BotSpec = load_spec(args.spec)
    mints: List[str] = []
    first_ts: Dict[str, int] = {}
    swaps_by_mint: Dict[str, List[CanonicalSwap]] = {}
    cache_ok = True
    try:
        check_cache_fresh(args.cache, args.data)
        mints, first_ts = cache_universe(args.cache, args.since, args.until,
                                         min_swaps=args.min_swaps)
        mints = mints[: args.limit]
        print(f"[train] universe: {len(mints)} mints (chronological, from cache)")
        con = open_cache_connection()
        for i in range(0, len(mints), 200):
            batch = mints[i:i + 200]
            got = fetch_swaps(con, args.cache, batch)
            swaps_by_mint.update(got)
            if (i // 200) % 5 == 0:
                print(f"[train] cache fetch {min(i + 200, len(mints))}/{len(mints)} mints")
    except Exception as e:  # noqa: BLE001 -- cache absence is a normal state
        cache_ok = False
        print(f"[train] swaps cache unusable ({e}); falling back to the store "
              f"(slow, one query per mint).")
        from ..store import Store
        store = Store(args.data, read_only=True)
        mints = store.mints(since=args.since, until=args.until,
                            min_swaps=args.min_swaps)
        first_ts = store.sql(
            "SELECT mint, min(ts_ms) t0 FROM swaps GROUP BY mint"
        ).set_index("mint")["t0"].to_dict()
        mints.sort(key=lambda m: first_ts.get(m, 0))
        mints = mints[: args.limit]
        for mint in mints:
            swaps_by_mint[mint] = list(store.iter_swaps(mint))
    print(f"[train] {len(swaps_by_mint)} mints loaded "
          f"({'cache' if cache_ok else 'store'})")

    bands = [spec.band_by_name(b.strip())
             for b in args.bands.split(",")] if args.bands else spec.bands
    feature_names = list(TokenState("probe").features().keys())
    print(f"[train] {len(feature_names)} feature columns; bands: "
          f"{[b.name for b in bands]}")

    for band in bands:
        ds = build_band_dataset(swaps_by_mint, spec, band,
                                stride_target=args.stride_target)
        if ds is None:
            continue
        print(f"[train:{band.name}] {ds['n_mints']} mints, {len(ds['y'])} rows, "
              f"base rate {ds['y'].mean():.3f}")
        train_band(ds, feature_names, band, Path(args.out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
