# `tape` — the plan

A rebuild of TradingRPC as one system instead of three, designed around a single
idea: **act only where information exists, attach a number to how sure you are,
and size by that number.**

Written 2026-09-20 against the measured state of `v3`. Every claim about the
current system is backed by its own ledger and tapes (see `CODEBASE_REVIEW_2026-09-20.md`).

---

## 0. The diagnosis, stated once

v3 is a well-built rule engine that has now measured its own edge twice, on two
independent populations, and got the same answer both times: **the round-trip
fee, and nothing else.**

- Live ledger, 2,056 positions: **−1.71%** against a −1.99% round trip.
- Research corpus, 8,276 decision points: **−2.085%** against the same −1.99%.

That is not a bug and not bad luck. It is what an efficient market looks like at
the timescale the bot is operating on. The reason it "swings in the fog" is
structural, and there are exactly three causes:

**1. It decides from a 5-second-old snapshot of a pool holding 0.1 SOL.**
There is almost no information in that observation. Median token in today's
sample peaks at 0.86 SOL of liquidity; the median 10-second flow bar contains
*one* trade from *one* wallet. You cannot be confident about a token that has
existed for five seconds, and neither can anyone else. Confidence is not a
modelling problem here — it is an information problem, and the fix is to trade
somewhere information has had time to accumulate.

**2. Every decision is binary and every position is the same size.**
Enter or don't. 0.006 SOL either way. So a trade the system half-believes in
costs exactly as much as one it is sure about, and the book's outcome is the
unweighted average of 2,000 coin flips. That is the definition of swinging in
the fog: no opinion is ever expressed with more force than any other.

**3. Its exits fire on single observations.**
One creator sell → full exit (428 times, −14.8%). One negative 20-second flow
window → full exit (1,174 times, −8.5%, half the book's basis). A rule that acts
on n=1 has maximum variance by construction.

So the rebuild is not "a better model bolted onto v3". It is three inversions:

| v3 | `tape` |
|---|---|
| trade the newest, thinnest thing on chain | trade where there is a tape to read |
| binary enter/don't, flat size | calibrated probability → conviction-weighted size |
| rules decide, ML is an optional gate at the end | model decides, rules are safety rails only |

---

## 1. The uncomfortable part: trade *fewer* tokens

You asked for "collect lots of tokens and quick trade on them". Half of that is
right and half of it is what the data says is losing money.

**Collect lots — yes, absolutely.** Collection is nearly free, and it is the
only way to build the creator/wallet reputation graph that is your one
defensible edge. Collect everything, forever.

**Trade lots — no.** 2,056 positions returned −1.71%. 15.8% of them reached cost
recovery and those carried the entire positive contribution; the other 84%
bled the fee. A bot that takes 30 high-conviction positions a day at 20× the
size will beat one that takes 2,000 at −2% each, and it will do it with a
tighter confidence interval on whether it is working.

This is also the only route to the thing you actually asked for. "Feels
confident" and "takes every trade" are mutually exclusive: a system that acts on
everything has, by definition, no threshold, and a system with no threshold has
no confidence. **Selectivity is what confidence looks like from the outside.**

Concretely: `tape` is designed to reject ~99% of what it sees and to be *right*
about the 1%, with a number attached to how right it expects to be.

---

## 2. Four structural decisions

### 2.1 One implementation, in Python

v3 keeps the strategy in two languages and holds them together with golden
fixtures. That apparatus is impressive and it has still leaked real bugs — the
one-shot entry decision was fixed in `backtest.py` and never in the bot, so live
and research ran structurally different logic for weeks, with the live one
strictly worse.

`tape` is Python only. The justification is your own: `minAgeMs` exists
*precisely to remove you from the latency race*. Your windows are 20 seconds to
15 minutes. Nothing in the strategy can express a millisecond. Paying for a
second language to buy latency the strategy cannot use, and paying for it in
duplicated logic and drift bugs, is a bad trade.

What you give up: nothing measurable. What you gain: half the code, no fixture
mirror, and the model lives in the same process as the decision that uses it.

### 2.2 The live bot *is* the backtester

Not "mirrors". **Is.** One decision function:

```
decide(state: TokenState, model: Model, spec: Spec) -> Decision
```

It is called by the replay engine with bars from disk, and by the live engine
with bars from a websocket. The only difference between backtest and live is
which `SourceAdapter` is feeding the bars. There is no second code path, so
there is no class of bug where they disagree.

This single change kills the failure mode that has cost you the most: the
`decodeTradeEvent` import bug, the cumulative-flow-window bug, the one-shot
entry bug, and the phantom double-sale were all "the live path does something
the tested path doesn't."

### 2.3 Buy parsed swaps; write zero decoders

You chose an all-launchpad, all-major-AMM universe. Doing that with hand-written
decoders is 8+ bespoke binary parsers, each needing empirical verification
against live transactions, each versioning independently, and each a candidate
for the exact silent-failure mode that voided your 2026-09-18 run.

At your budget you can simply buy the parsing. See `DATA.md`. This is the single
highest-leverage purchase in the project and it is what makes the wide universe
affordable at all.

The verified pump.fun and PumpSwap decoders in v3 stay, as a **free
corroboration source**: when both the vendor and your own decoder see the same
pool, they must agree on side, amount and wallet. A mismatch is an alarm. That
is worth more as a data-integrity check than as a primary feed.

### 2.4 Event bars, not time snapshots

v3 samples every 10 seconds. Memecoin activity is violently non-uniform: the
same 10 seconds can contain 200 trades or zero. Time bars therefore oversample
dead periods and undersample exactly the moments that matter — which is
mechanically why "median time-to-peak = 0 seconds" showed up in your v1
analysis.

`tape` builds **volume bars and dollar bars** — a bar closes when a fixed amount
of quote volume has traded, not when a clock ticks. Properties that matter here:

- bar returns are much closer to IID and far less heteroskedastic, which is what
  every statistical test and every model downstream assumes;
- a quiet token produces few bars and therefore contributes few (correctly
  down-weighted) training rows, instead of thousands of near-duplicate ones;
- "N bars of confirmation" becomes a statement about *activity*, not about the
  wall clock, which is what you actually mean by "wait for confirmation".

Time bars are still produced alongside, for liquidity/holder state that only
makes sense on a clock. Both go in the store.

---

## 3. Architecture

```
                    ┌──────────────── sources ────────────────┐
  vendor websocket ─┤ BitquerySource                          │
  helius websocket ─┤ HeliusLogSource (corroboration)         │──► CanonicalSwap
  parquet on disk  ─┤ ReplaySource  ◄── the one used offline  │    (one dataclass)
                    └─────────────────────────────────────────┘
                                      │
                                      ▼
                            BarBuilder  (dollar / volume / time)
                                      │
                                      ▼
                       TokenState   incremental, never sees the future
                         ├ flow, breadth, concentration
                         ├ liquidity path, depth
                         ├ price/indicator stack
                         ├ creator + wallet reputation  (cross-token graph)
                         └ market regime  (chain-wide aggregates)
                                      │
                    ┌─────────────────┴─────────────────┐
             offline│                                   │live
                    ▼                                   ▼
            labels ─► weights ─► purged CV ─► model ─► Model artifact
                                                        │
                                                        ▼
                                                 decide(state, model, spec)
                                                   ├ hard rails (fail closed)
                                                   ├ p̂ calibrated
                                                   ├ conformal credibility
                                                   ├ abstain if uncertain
                                                   └ size = f(p̂, depth, Kelly)
                                                        │
                                                        ▼
                                              Simulator / Executor
                                                        │
                                                        ▼
                                                     Ledger
```

Layer rules, enforced by tests:

- A source adapter may only emit `CanonicalSwap`. Venue specifics stop there.
- `TokenState` is append-only and single-pass. It physically cannot read a
  future bar, because it is never handed one. `test_no_lookahead` truncates a
  tape and asserts every surviving feature row is bit-identical.
- Labels are computed in a module that **cannot import features**, and vice
  versa. This is v3's best idea and it is kept verbatim.
- `decide()` is pure. Same state + same model + same spec = same decision,
  always, on any machine.

---

## 4. Data

Full detail in `DATA.md`. The summary:

**Buy (~$128/mo):**

| what | why | cost |
|---|---|---|
| Bitquery **Pro** (annual) | parsed DEX trades across pump.fun, PumpSwap and major AMMs, with wallet + side + amounts, over GraphQL and websocket; one schema for live and historical | $79/mo |
| Helius **Developer** | your existing RPC/WS path, pool account state, creation events, and the free corroboration decoder | $49/mo |

**Add when the backfill is needed (from $100/mo, one or two months, then cancel):**
Bitquery's live dataset is a ~30-day rolling window; anything older needs
`dataset: archive`, a separate add-on. Buy it for the months you backfill, then
drop back to Pro. This is what turns "wait two weeks for tapes" into "have six
months of history by Tuesday".

(A brief detour through Birdeye, while Bitquery's account auth was broken, is
documented in `docs/DECISIONS.md` D16/D17 and kept as a reasoned-through
fallback in `tape/sources/birdeye.py` — not used as primary, since it costs
more once WebSocket is counted and has no backfill endpoint at all.)

**Free and worth having:**
- Google BigQuery's public Solana dataset — raw, so you decode it yourself, but
  1 TB/month of query is free and it is date-partitioned, so targeted historical
  pulls are genuinely cheap. Good archive fallback and a cross-check on vendor
  data.
- RugCheck, as today. Named risks and `lpLockedPct` only — never the aggregate
  danger flag, which v1 measured as a non-signal.

**Not worth buying, at any point on the current roadmap:**
Yellowstone gRPC ($400–999/mo). It buys sub-second latency. Your fastest decision
window is 20 seconds. You would be paying four figures a month for a property
the strategy cannot express. Revisit only if you ever build a component that
acts inside a second — and `minAgeMs` says you deliberately won't.

**Storage:** partitioned Parquet + DuckDB, not JSONL. Your 20 MB/day of JSONL
becomes hundreds of millions of swaps once the universe widens; DuckDB reads
partitioned Parquet lazily and will let you run "every swap in September where
the buyer later became a top-decile wallet" on a laptop, in seconds, without a
database server. This is the difference between iterating on features in minutes
and in hours, and you will do it thousands of times.

---

## 5. The ML programme

Full detail in `ML.md`. The shape, with pre-registered gates — a stage that
fails its gate does not proceed, and the answer is a different universe or
horizon, never a bigger model.

**Stage 0 — infrastructure & backfill.**
Store, bars, features, replay. *Gate:* replaying a recorded live day through
`decide()` reproduces that day's ledger within rounding. If it doesn't, the
research path and the live path measure different things and nothing after this
means anything.

**Stage 1 — the information audit. Do this before any model.**
For each feature family, measure out-of-sample AUC and mutual information
against the model-free excursion labels (MFE/MAE), on tokens the threshold was
never fitted on.
*Gate:* at least one family clears **AUC 0.55** on held-out tokens.
This is the cheapest possible kill test — a few hours of compute — and it
answers the only question that matters: is there forward information in this
data at all? If nothing predicts maximum favourable excursion, nothing will
predict profit, and no amount of gradient boosting will conjure it. Most
projects skip this and spend six months finding out the expensive way.

**Stage 2 — primary model.**
Gradient-boosted trees (LightGBM), target = *does this bar reach the
cost-recovery multiple before the adverse barrier, inside the horizon*. Your
measured live base rate for that event is **15.8%**; breakeven at a 2.38 payoff
is **29.6%**.
*Gate:* top-decile hit rate exceeds breakeven on held-out days, in **at least 4
of 5 purged folds**. Report every fold. A model that works in one fold is noise.

**Stage 3 — meta-labelling and confidence.**
The primary model (or the accumulation rule) proposes; a **secondary model**
decides whether to act on the proposal, and its output is the probability used
for sizing. This is the architecture that produces a system with opinions of
varying strength instead of a binary one — it is the direct answer to "confident,
not swinging in the fog".
*Gate:* calibration. When it says 35%, it must win 35 ± 5% of the time on
held-out data, shown as a reliability curve. An uncalibrated model can rank
perfectly and still be useless for sizing.

**Stage 4 — conformal abstention.**
Inductive conformal prediction, Mondrian (per-band), gives a distribution-free
credibility for each prediction. Low credibility → abstain, regardless of p̂.
*Gate:* realised coverage matches nominal on held-out days.
This is the piece that makes the bot *stop trading when it doesn't know*, which
is the behaviour you described wanting and which v3 has no way to express.

**Stage 5 — paper-live with the model.**
Two weeks minimum.
*Gate:* realised hit rate inside the walk-forward CI. A gap larger than the
interval means the live and research paths still disagree — go back to Stage 0.

**Stage 6 — real money, small, with a hard daily loss cap.**

### Compute

Your 3060 12GB is **more than enough** for Stages 1–4. Tabular GBDTs on a few
million rows are CPU-bound; a 3060 will handle LightGBM's GPU histogram fine and
the bottleneck will be feature computation, not training. Do not rent cloud GPUs
for this — it is money spent on the wrong resource.

Rent GPU only if Track B (below) earns its way in.

**Track A (primary): GBDT on engineered features.** Tabular, heavy-tailed, mixed
missingness, modest sample — this is precisely where GBDTs beat everything,
including anything deep. Interpretable, fast to iterate, and SHAP tells you
*why* it is confident, which matters when you have to decide whether to believe
it.

**Track B (optional, later): sequence model on the raw tape.** A small temporal
CNN or transformer over the last N bars of (price, flow, breadth, liquidity),
trained per-band. Justified only if Track A clears its gates and you want to
know whether the hand-engineered features are throwing information away. This is
where renting an A100 for a weekend makes sense — and not before.

---

## 6. What "confident, not in the fog" means, operationally

Six concrete mechanisms. Each is a file in this repo, not an aspiration.

1. **Calibrated probability on every decision.** Isotonic regression fitted on a
   held-out fold. Reliability curve reported every retrain.
2. **Conformal credibility.** A second number: how *typical* this observation is
   relative to the training distribution. Novel token, weird pool, unseen
   regime → low credibility → no trade, even if p̂ is high. This is the
   difference between "the model says 40%" and "the model says 40% and has seen
   this situation before".
3. **Explicit abstention.** `Decision.abstain` is a first-class outcome with its
   own reason code, logged and tallied. The daily report shows how often the bot
   *chose not to know*, which is a health metric, not a failure.
4. **Conviction sizing.** `size = kelly_fraction(p̂, payoff) × capital`, capped by
   pool depth and a per-band maximum. A 45% signal gets several times the size
   of a 31% one. This is where two points of hit rate turn into money instead of
   a statistic.
5. **Drift gate.** Daily PSI on every feature against the training distribution.
   v1 saw PSI 0.80 within 48 hours — that is a model predicting a world that no
   longer exists. Above threshold: halve size; well above: stop and retrain.
6. **Pre-registered kill criteria** (§9). Written before the results, so they
   cannot be moved after seeing them.

A decision log line in `tape` looks like this, and every field is a number you
can argue with:

```
mint=…  band=B_mid  p=0.38  cal=0.36  cred=0.81  size=0.42 SOL  (cap: depth 0.61)
   rails: passed    regime: pf_creation_rate p62, sol_24h +2.1%
   top features: creator_prior_grad_rate +0.09, buyer_breadth_60s +0.07, liq_slope_5m +0.05
```

---

## 7. Strategy changes carried over from the review

These are independent of the ML and should ship first, because they are spec
edits and they are worth more than a model:

| change | evidence |
|---|---|
| Stop trading the bonding curve; floor at ~$13k one-sided liquidity, denominated in **USD** | round trip 3.50% on the curve vs 2.00% in a real pool — 1.5pp bought by venue, larger than any edge in the repo |
| Demote `flow_reversal` from full exit to partial (or to a feature) | −8.5% on 50.6% of basis live; paired ablation says removing it is +2.42pp CI [+1.16, +3.91] |
| `coordinatedSell` requires ≥2 cluster wallets **and** ≥2% of pool liquidity | fires on any creator dust sale; −14.8% on 18% of basis |
| `adaptiveStopLoss` off everywhere | −59.8% realised against a configured 10–35% |
| Keep `costRecovery` at 1.8× and the profit-only trailing arm | +68.0% and +57.6% — the only two things making money |
| Add `candidate_partial_exit_2x`: sell half at +100% (recovers capital exactly), ride the rest to breakeven — same mechanism as `costRecovery`, run as a separate `unvalidated` candidate next to the fitted 1.8× trigger, not a replacement for it | D18, `docs/DECISIONS.md` — untested against the fitted value; `tape/costs.py::partial_exit_*` prices the extra fixed-cost leg |
| Add a wall-clock exit sweep | exits only fire on curve updates today; a token that stops trading can't be exited |
| Add `fixedCostSol` per transaction to the cost model | until this exists every backtest here is optimistic by an unmeasured amount |
| Wire a live SOL/USD feed | `solPriceUsd` is null; every band boundary runs on a hardcoded $101.75 |

---

## 8. Migration

`MIGRATION.md` has the file-by-file manifest. Headline:

**Port, with respect** — the exit-ordering logic and its hard-won invariants
(trail arms only on profit-taking partials; adaptive stop fires once; Category A
zeroes the remaining fraction), the ceiling model, the AMM execution math, the
research pipeline's leak discipline, `importIntegrity`-style tests, and the
comment culture. These cost real money to learn and they are correct.

**Rewrite** — ingestion (vendor-first), storage (Parquet/DuckDB), features
(incremental, cross-token), the decision layer (model-first).

**Burn** — `index_old.js`, `js/`, `python/`, `js/src/v2/`. Three dead
generations, ~9,500 files, and a root README that describes the oldest of them.
Tag the commit and delete. Anything you might want is in git history.

---

## 9. Pre-registered kill criteria

Written now, while it is cheap to be honest:

- **Stage 1 fails** (no feature family clears AUC 0.55 out-of-sample on any
  horizon or band) → there is no forward information at this timescale in this
  data. Stop, or change the universe — do not reach for a bigger model.
- **Stage 2's top decile stays below 29.6%** → there is no selection to be had.
  The same conclusion.
- **The edge dies under 2× cost sensitivity, or under the real fixed fee** → it
  was a cost artefact, not an edge.
- **Live hit rate lands materially below walk-forward** → the two paths measure
  different things; fix that before anything else.
- **Six months in, with all gates passed, the book is inside its own CI of
  zero** → the edge is too small to be worth the operational risk of real money.

The honest prior: memecoin trading against faster, better-capitalised
participants is close to zero-sum, and your measured baseline landing *exactly*
on the fee is what that looks like. The three places an edge can still come from
are, in order:

1. **lower cost per trade** — venue choice, bought outright;
2. **information others don't compute** — the cross-token creator and wallet
   reputation graph, which needs history rather than speed and which nobody can
   copy without building it;
3. **acting only when the evidence is strong** — which is the entire point of
   the confidence machinery above.

Not faster reactions to the same public tape. v3 already knew that; `tape` is
built so that everything else follows from it.

---

## 10. Build order

| # | work | days | unblocks |
|---|---|---:|---|
| 1 | store, canonical schema, Bitquery adapter, backfill 3 months | 3–4 | everything |
| 2 | bar builder + `TokenState` + no-lookahead test | 3 | features |
| 3 | labels, uniqueness weights, purged CV, cost model | 2 | any honest number |
| 4 | **Stage 1 information audit** | 1 | go / no-go on the whole project |
| 5 | creator + wallet reputation graph from backfill | 2 | the defensible feature family |
| 6 | GBDT + calibration + conformal + `decide()` | 4 | Stage 2–4 gates |
| 7 | live engine on the same `decide()`, paper only | 3 | Stage 5 |
| 8 | ledger, drift monitor, daily report | 2 | knowing whether it works |

About four focused weeks to a system that can answer the question. Step 4 is the
one that matters: it is one day of work and it tells you whether the other
twenty-seven are worth doing.
