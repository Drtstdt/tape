# The modelling programme

The target, the features, the validation, and the machinery that turns a score
into a position size you can defend.

## 1. Target

**Binary, triple-barrier:** from bar *i*, does the price reach the
cost-recovery multiple (upper barrier) before the adverse barrier (lower), within
the horizon (vertical barrier)?

Measured live base rate for that event: **15.8%** of positions reached cost
recovery. Breakeven win rate at the observed 2.38 payoff: **29.6%**.

So the model does not need to predict returns. It needs to find a subset of bars
where the rate of that one event roughly doubles, and it needs to be *calibrated*
enough that you can size against it. Regression on returns is the wrong tool —
the return distribution is a fat tail and a regressor will spend its capacity
chasing three outliers.

Three barrier configurations, fitted separately per band:

| band | upper | lower | horizon |
|---|---|---|---|
| A_graduate | 1.8× | −35% | 45 min |
| B_mid | 1.6× | −30% | 8 h |
| C_deep | 1.4× | −25% | 3 d |

**When both barriers fall inside one bar, the label resolves DOWN.** Bars have
no intra-bar path, and a labeller that breaks ties in its own favour produces a
backtest nobody can reproduce live.

Keep v3's other two label families alongside, because they answer different
questions:

- **Excursion** (`mfe_pct`, `mae_pct`, `ret_at_horizon`) — model-free, what price
  physically did. This is what Stage 1's information audit tests against. If
  nothing predicts MFE, nothing will predict profit.
- **Simulated** (`sim_pnl_pct`) — the real exit state machine run forward from
  that bar, net of costs. The only label whose units are the thing being
  optimised, and the only one that catches exit bugs.

Train on the first two to learn whether signal exists; select on the third to
decide what to ship.

## 2. Features

Six families. The first four exist in some form in v3; the last two are the ones
that are actually yours.

**Flow** (measured, never inferred). Net flow over k bars, buy/sell volume
ratio, trade count, arrival rate and its acceleration. Deliberately *not* OBV or
Accumulation/Distribution: those infer order flow from price and volume because
equities cannot see it. You can. Inferring what you can measure is strictly
worse.

**Breadth and concentration.** Unique buyers/sellers, largest-buyer share,
Herfindahl index of buy volume, new-wallet fraction, repeat-buyer fraction. All
computed over attributed trades only (§DATA 3.1).

**Liquidity path.** Depth now, slope over k bars, drawdown from peak depth,
depth relative to the token's own history, position-to-depth ratio at the
candidate size.

**Price / indicator stack.** Returns at several bar lags, realised volatility,
RSI *as a veto and divergence source only* — never as a trigger: on the v3
reference token RSI read 15–28 through an entire −98.4% collapse and never
crossed 30, so `RSI<30 → buy` buys all the way down. Fib retracement level of
the current bounce, and flow/price divergence, which is the strong one because
it substitutes a measured quantity for an inferred one.

**Creator and wallet reputation — the defensible family.** Built from your own
backfill, as-of-time strict:
- creator: prior launches, graduation rate, rug rate, median peak multiple,
  time-since-last-launch, launches-in-last-hour (v3 found **7 of 20 consecutive
  creations from one spam wallet**);
- early buyers: what fraction of this token's first N buyers are wallets that
  historically buy tokens which reach cost recovery; what fraction are
  first-block-and-out-in-30-seconds wallets;
- funding clusters: wallets sharing a funding source, treated as one actor for
  breadth and insider tests.

This is the family nobody else computes, because it needs a cross-token wallet
graph and history rather than a faster connection. It is also the only thing in
the project that gets *better* the longer you run it.

**Market regime.** Chain-wide, one row per minute, joined on time: launchpad
creation rate, graduation rate, aggregate DEX volume, SOL price and its 24h
trend, median new-pool liquidity. A model fitted across regimes with no regime
feature will look stable in backtest and swing in the fog live — this is a large
part of why v1 saw PSI 0.80 within 48 hours.

## 3. Bars, and why not seconds

A bar closes when a fixed amount of **quote volume** has traded (dollar bars),
not when a clock ticks. Consequences that matter:

- bar returns are far closer to IID and much less heteroskedastic, which is what
  every downstream statistic assumes and time bars violate badly on memecoins;
- a token with no activity produces no bars, so it stops contributing thousands
  of near-identical training rows;
- "three bars of confirmation" becomes a statement about *how much trading has
  happened*, which is what you mean, rather than about the wall clock.

Bar size is set **per band**, as a fraction of pool depth, so one bar is a
comparable economic event in a $13k pool and a $500k one.

Time bars are built in parallel for anything that is genuinely clock-shaped
(liquidity snapshots, holder counts, age).

## 4. Sample weighting — the part most people skip

Labels from overlapping windows are not independent observations. A token with
3,000 bars contributes 3,000 rows that are nearly the same observation repeated,
and treating them as independent turns noise into a publishable p-value.

Three corrections, all standard and all cheap:

1. **Uniqueness weights.** Each label spans a time interval. A sample's weight is
   its average uniqueness — the inverse of how many other labels overlap it at
   each instant. Overlapping labels get down-weighted automatically.
2. **Return attribution.** Weight also by the absolute return the label spans, so
   a bar that led somewhere counts for more than one that led nowhere.
3. **Group by mint everywhere.** Every split, every bootstrap, every interval
   resamples *tokens*, not rows. The effective sample size is the number of
   mints, and the report prints it first rather than burying it.

## 5. Validation

**Purged, embargoed, grouped, chronological.** All four words are load-bearing:

- *Grouped by mint* — a random split puts bar 400 of a token in train and bar 401
  in test, which is leakage dressed as a validation score.
- *Chronological* — you cannot train on October and test on September.
- *Purged* — a training label whose window overlaps the test period is dropped.
- *Embargoed* — a gap after the test period, so serial correlation cannot carry
  information backwards.

Five folds minimum. **Report every fold.** A strategy whose mean is positive
because one fold is spectacular is a strategy that worked once. v3's own
real-money gate says "every individual fold positive" and that is the right bar —
v1's eight tuned archetypes were all negative and that is the bar being failed.

Threshold fitting happens on train tokens and is reported on test tokens, both
numbers side by side. A rule whose test number collapses is printed as
collapsed, never quietly dropped.

## 6. Models, in order

1. **Logistic regression** on 10 features. The honest baseline. If the GBDT
   cannot beat this by a real margin, the extra complexity is costing you
   interpretability for nothing.
2. **LightGBM** — tabular, heavy-tailed, mixed missingness, modest sample. This
   is exactly where GBDTs win, including against anything deep. Monotonic
   constraints where the direction is known (more breadth is not worse). SHAP
   for every decision, because you will need to decide whether to believe it.
3. **Meta-labelling.** The primary rule (the accumulation gate) says *when to
   consider acting*; a secondary GBDT trained only on those candidate bars says
   *whether to act and how sure it is*. Its output is the probability that drives
   sizing. This decomposition is what produces a system with opinions of varying
   strength.
4. **(Track B, later)** A small temporal CNN / transformer over the last N bars.
   Only after Track A clears its gates, and only to test whether the engineered
   features are discarding information. This is the one place cloud GPU hours are
   worth buying.

## 7. Confidence machinery

**Calibration.** Isotonic regression on a held-out fold. The number that matters
is not AUC: when the model says 35%, does it win 35% of the time? Report a
reliability curve every retrain. An uncalibrated model can rank perfectly and
still be useless for sizing, which is the only thing you want it for.

**Conformal prediction.** Inductive conformal, Mondrian (calibrated separately
per band), gives every prediction a *credibility* — how typical this observation
is relative to what the model was fitted on — with distribution-free coverage
guarantees. A token in an unseen regime gets low credibility and is not traded
even when p̂ is high.

This is the mechanism that makes the bot stop when it does not know, and v3 has
no way to express it. It is also, in practice, most of the difference between
"confident" and "in the fog".

**Abstention.** Trade only when `p̂ > breakeven + margin` **and**
`credibility > floor`. Abstention is a first-class logged outcome with its own
reason code; the daily report shows the abstention rate as a health metric.

**Sizing.** `f* = (p·b − (1−p))/b`, quarter-Kelly, `b` capped by the ceiling
model (`b_capped = min(b_hist, ceiling × p_up)`), then clamped by the band's
position fraction and the pool-depth rail — whichever binds first. Until a
validated `p` exists the code falls back to the flat band fraction **and logs
that it did**.

**Drift.** Daily PSI per feature against the training distribution. Above 0.2,
halve size; above 0.3, stop and retrain. Retraining is scheduled weekly and
triggered by drift, and every retrain re-runs the full gate suite before its
artifact is allowed to go live.

## 8. Compute

A 3060 12GB is sufficient for Stages 1–4 and the bottleneck will be feature
computation, not training. Practical notes:

- LightGBM GPU histogram works on a 3060, but for a few million rows the CPU
  build is often faster; benchmark rather than assuming.
- Feature computation over the backfill is the long pole. Write it once,
  vectorised over Parquet via DuckDB, cache to disk, and never recompute a
  feature you have not changed.
- Hyperparameter search: Optuna with a purged-CV objective, `n_trials` in the
  low hundreds, pruning on the first fold. Overnight on the 3060.
- **Do not rent cloud GPUs for the tabular track.** It is money spent on the
  wrong resource. Rent only for Track B, and only after Track A earns it.
