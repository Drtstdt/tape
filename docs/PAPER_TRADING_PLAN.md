# Paper trading with an online-learning policy — the plan

Written 2026-09-24, in response to the explicit request: run something that
makes trades on paper (no real money), starts out making dumb/random-looking
decisions, and gets better over time from its own results. This is a plan
document only — nothing described here is built yet. `docs/PLAN.md` remains
the architecture doc for the batch-trained model track (Stage 2-4); this is
a parallel, complementary track, not a replacement for it. See §7 for why
both can coexist honestly.

---

## 0. Where this sits relative to what's already been measured

`information_audit.py`'s Stage 1 gate has now run three times (D70, D72,
D83) on the same narrow, largely-single-day, 61-65-token corpus and come
back `no_edge_found` every time, most recently even with `price_after_Ns`
(the most promising raw idea found so far) included. `docs/PLAN.md`'s own
pre-registered kill criteria say plainly: *"Stage 1 fails → stop, or change
the universe... do not reach for a bigger model."*

Training and deploying a real GBDT (`tape/model.py`'s Stage 2) right now
would violate that discipline outright — there is no validated edge to
deploy. **This plan does not do that.** Paper trading with an online model
is a different thing, and it earns its place for a different reason:

- It costs **zero real capital** — "idiotic mistakes" are safe by
  construction, not despite the discipline above but because of it.
- It is itself a way to **attack D56/D83's real problem** — the sample is
  too narrow and too calendar-concentrated to trust *any* verdict, positive
  or negative. A paper-trading policy running continuously accumulates a
  labeled, calendar-diverse decision log for free, as a side effect of
  running at all.
- An online policy is evaluated on **its own accumulating record**, with
  pre-registered success criteria (§6), the same honesty discipline as
  every Stage in `docs/PLAN.md` — not on a vibe about whether the equity
  curve looks like it's going up.

This is explicitly **not** a claim that an edge exists. It's a cheap,
safe, honest way to keep testing for one while the real bottleneck (D56:
not enough calendar-diverse data) fixes itself over time.

---

## 1. Inventory — what's already built and gets reused as-is

| piece | status | role here |
|---|---|---|
| `tape/store.py`, `Store` | done, in use | historical replay source (Phase A) |
| `tape/bars.py::BarBuilder` | done, in use | identical bar construction, live and replay |
| `tape/features.py::TokenState` | done, in use (53 features incl. `price_after_Ns`, D82) | the policy's input vector — no new features needed to start |
| `tape/labels.py::triple_barrier` | done, in use | the fixed EXIT rule for v1 (see §3) |
| `tape/costs.py` | done, in use | round-trip cost floor, applied to every paper decision so PnL isn't fantasy-fee-free |
| `tape/cv.py` (`token_bootstrap` etc.) | done, in use | the evaluation script's CIs (§6), same token-cluster-resampling discipline as `information_audit.py` |
| `tape/policy.py::Rails`, `evaluate_rails` | done, in use **unmodified** | hard safety filters (liquidity floor, age floor, renounced authorities, etc.) — these encode structural safety, not model opinion, so they apply to a random policy exactly as they would to a validated one |
| `tape/policy.py::decide()`, `ConfidencePolicy`, `kelly_size` | done, **not reused as-is** | built around an already-fitted `ModelArtifact` (isotonic calibration + conformal credibility over a fixed training distribution) — an online model has neither on day one. New, simpler decision glue is needed for paper mode (§4). |
| `scripts/live_paper_monitor.py` | done, in use as a base | already does exactly the live plumbing needed — discovery, polling, bar/feature building, concurrency cap, retirement — but deliberately makes no decisions and tracks no PnL (its own docstring: "WHAT THIS IS NOT: a trading bot"). This plan extends it rather than duplicating it. |
| `scripts/scenario_backtest.py` | done, in use as reference | already proved the DEPLOYABLE-framing and price-move-triggered-exit machinery (D73-D76) — not imported directly (v1's exit is the simpler fixed triple-barrier, §3), but its dilution/tautology lessons directly inform how paper-trading PnL must be measured. |

**New pieces this plan calls for** (none built yet): an online policy module,
a replay-mode paper trader, a live-mode paper trader, and an evaluation
report. All detailed in §4-6.

---

## 2. What "idiotic mistakes, learns progressively" means, concretely

This is a **contextual bandit** problem, not a supervised-learning problem,
and the distinction matters for the design:

- The policy only ever observes the outcome of the action it actually took
  (entered or abstained) — never the counterfactual ("what if I had entered
  the ones I abstained on"). That's exploration/exploitation, not batch
  training on a fixed labeled set.
- Decisions must be causal and made bar-by-bar in real time, from whatever
  `TokenState.features()` knows *at that moment* — same structural
  no-lookahead contract as everything else in `tape/`.
- "Idiotic at first" is not a bug to hide — it's the deliberate result of a
  high initial exploration rate. It should be visible, logged, and clearly
  separated from "the model's actual opinion once it has one" (§6).

**Model family: a small, hand-rolled, incrementally-updated logistic model
— not a library, not a GBDT.** Reasoning:

- Every existing model-shaped thing in this repo (`tape/model.py`'s GBDT) is
  a *batch*-trained artifact, fit once on a fixed corpus. An online model
  needs `partial_fit`-style incremental weight updates after every single
  resolved trade, which is a genuinely different code path, not a
  configuration of the existing one.
- `information_audit.py`'s own printed verdict text is explicit project
  doctrine: *"Do NOT reach for a bigger model."* A hand-rolled online
  logistic regression (a weight vector, a sigmoid, an SGD update rule) is
  auditable line-by-line the same way `tape/features.py` is, needs no new
  dependency, and is trivial to unit test deterministically (seed the RNG,
  assert the weights move in the expected direction after a known outcome)
  — the same testing discipline every other module here already has.
  A library (river, vowpalwabbit) is not ruled out later, but starting
  there means trusting an opaque update rule before trusting a transparent
  one has even been shown to wire together correctly.

**Exploration: epsilon-greedy**, the simplest policy that actually
guarantees "makes dumb mistakes on purpose, on a schedule, decaying
over time":

- `epsilon` = probability of taking a *uniformly random* action (enter or
  abstain) regardless of what the model currently believes.
- Starts high (proposed default: 0.90 — the first several dozen decisions
  are close to a coin flip by design) and decays toward a floor that never
  reaches zero (proposed default: 0.15 — some exploration always continues,
  so the policy can keep noticing if the world changes, matching the
  project's own drift-awareness principle from `docs/PLAN.md` §6.5).
- Every decision is logged with whether it was an **explore** or an
  **exploit** step. This split is not cosmetic — it's what makes §6's
  evaluation honest (an "idiotic" explore-phase loss is not evidence the
  model is bad; an exploit-phase loss is).

---

## 3. Decision scope for v1: learn ENTRY only, fix the EXIT

The user's original request (D73 onward) was about *exit* scenarios
(hold / sell 10% / sell 50% / sell 100%, racing them against each other).
This new request is about a model that *learns*. Trying to learn both the
entry rule and the exit rule online, at once, from day one, means the
reward signal for "was entering here a good idea" is entangled with "was
the exit rule that happened to be active a good one" — two unknowns
confounding one signal, which is a much harder bandit problem and much
slower to get an honest read on.

**v1 proposal: only the entry decision (enter vs. abstain) is learned
online. The exit is the same fixed `triple_barrier` rule
`information_audit.py` and `live_paper_monitor.py` already use** (same
upper multiple / lower pct / horizon defaults). This keeps v1's PnL
directly comparable to the audit's own label definition — "did this entry
resolve UP, DOWN, or TIMEOUT" is a question the whole project already knows
how to ask honestly — and isolates the one new thing (does an online policy
learn anything from its own entries) from a second new thing (does an
online policy also learn a good exit) that D73-D76 already showed is easy
to get tautologically wrong.

**Learning the exit online too is the natural v2**, once v1 has
mechanically proven an online policy can update itself correctly over a
real, resolving decision stream. Not in scope here.

Position size in v1 is a **fixed nominal amount** (e.g. "1 unit"), not
Kelly-sized — there is no real capital, and sizing by a confidence number
that hasn't itself been validated would just be inventing a second unproven
thing on top of the first.

---

## 4. New component: `tape/online_policy.py`

A single class, `OnlinePolicy`:

- **State:** a weight vector over `TokenState.features()`'s keys (missing
  features handled the same "None means unmeasured, never imputed as 0"
  way as the rest of the project — likely mean/zero-centered + a learned
  per-feature scale, updated online too, so an early wildly-scaled feature
  doesn't dominate the dot product), a bias term, `epsilon` and its decay
  schedule, and a decision counter.
- **`decide(features) -> (action, p_raw, explore_flag)`:** with probability
  `epsilon`, pick uniformly at random; otherwise take the sigmoid of the
  dot product and threshold it. Rails (`tape/policy.py::evaluate_rails`,
  unmodified) are checked **first** and can still hard-reject regardless of
  what the policy would have chosen — a safety floor the model never
  overrides, same principle as the existing `decide()`.
- **`update(features, outcome_y)`:** one SGD step (logistic loss) — called
  only once a decision's `triple_barrier` outcome has actually resolved
  (UP=1 / DOWN,TIMEOUT=0), never before, so there is no lookahead in the
  learning signal either.
- **`save(path)` / `load(path)`:** a small JSON checkpoint (weights, bias,
  feature scaling state, epsilon state, decision counter, a version tag) —
  same pattern as `real_creation_times.json`'s cache file, so training
  state survives a restart and every decision is reproducible from the
  checkpoint that was live at the time.

Tested the same way `tape/features.py`'s D71/D82 additions were: a
hand-built synthetic feature/outcome stream where one feature is
constructed to genuinely predict the outcome, asserting the learned weight
on that feature moves in the correct direction and the model's accuracy on
held-out synthetic decisions improves as more updates accumulate — proving
the *mechanism* learns, on a case where the right answer is known by
construction, before ever pointing it at real, noisy token data.

---

## 5. New scripts

**`scripts/paper_trade_replay.py`** (Phase A — build and validate this
first). Walks the *already-collected* Store data chronologically (same
`chronological_universe()` used everywhere else), replaying it as if it
were arriving live: for each token, in bar order, ask `OnlinePolicy` for a
decision, evaluate rails, and if entered, resolve the outcome with
`triple_barrier` against that same tape and feed the result back into
`update()` the moment it resolves. Cheap (uses data already on disk),
fully reproducible (same seed → same decisions), and safe to iterate on
fast. Its purpose is **not** to find a profitable policy — 61-65 mostly
single-day tokens is far too little to trust either a positive or a
negative reading (same lesson as D83) — its purpose is to prove the
mechanics are wired correctly and to *watch* a policy visibly change its
behavior as decisions accumulate, before ever touching live infrastructure.

**`scripts/paper_trade_live.py`** (Phase B — after Phase A looks right).
Extends `live_paper_monitor.py`'s existing discovery/polling/bar-building
loop (reused, not rewritten) with the same decide → simulate-fill → wait
for `triple_barrier` to resolve → `update()` → log loop as the replay
version, using the exact same `OnlinePolicy` checkpoint format so the two
modes are interchangeable and directly comparable. Runs alongside the
standing `discover_pumpfun_launches.py` / `backfill_discovered_launches.py`
loop, not instead of it.

**A parallel random-policy baseline runs at the same time as the real
one, on the same token population, logged separately.** This is the
control group: at report time we need to be able to ask "did the
online policy actually do better than pure epsilon=1 random", not just
"did the online policy's number go up," because on a small, noisy sample a
random policy's number moves around too. Cheap to run (same rails, same
exit rule, epsilon locked at 1.0, its own independent log file).

**`scripts/paper_trading_report.py`** (the verification step). Reads the
decision ledger (§5.1) and reports, split explicitly by explore/exploit:
resolved-decision count, realized PnL (net of `tape/costs.py`), a
token-cluster bootstrap CI (`tape/cv.py::token_bootstrap`, same machinery
`information_audit.py` and `scenario_backtest.py` already use) on the
exploit-only subset, and a head-to-head comparison against the parallel
random-policy baseline's own resolved decisions over the same calendar
window. Never eyeballs a raw equity curve as the verdict.

### 5.1 The decision ledger

One row per decision, appended as it resolves (`data/paper_trades.parquet`,
same partitioned-Parquet-via-`Store`-adjacent convention as the rest of the
project, or a flat CSV to start if that's simpler to inspect by eye early
on): `decision_id, mint, ts_ms, mode (replay|live), policy (online|random),
action (enter|abstain), explore_flag, p_raw, model_version, entry_price,
size_nominal, exit_ts_ms, exit_outcome (UP|DOWN|TIMEOUT), realized_pnl_pct,
cost_adjusted_pnl_pct, features_snapshot (all 53, for later audit)`.

---

## 6. Evaluation discipline — pre-registered now, not after looking

Same culture as `docs/PLAN.md` §9's kill criteria, written before there's
anything to be tempted by:

- **Minimum sample before ANY verdict is drawn:** at least 300 resolved
  **exploit-phase** decisions, spanning at least 5 genuinely distinct
  calendar days (directly targets D56/D83's standing weakness — a verdict
  from one day is not a verdict). Explore-phase decisions are logged and
  reported but excluded from the success/failure judgment by design (§2).
- **Success criterion, fixed now:** the exploit-phase, cost-adjusted PnL's
  token-cluster bootstrap CI excludes zero on the positive side, **and**
  the online policy's exploit-phase mean PnL exceeds the parallel random
  baseline's mean PnL over the same window. Both conditions, not either —
  a positive CI that a random policy would have matched just as well is
  not evidence of learning.
- **Failure is a real, useful answer, not a dead end:** if the criterion
  isn't met after the minimum sample, that's the same honest kind of
  negative result D83 already produced — keep running (more calendar days
  is cheap and safe, per §0), or revisit the feature set / exit rule, never
  loosen the success criterion to make an unclear result look like a pass.
- **No mid-run hyperparameter changes** (epsilon schedule, learning rate,
  feature scaling) once a run is being evaluated against the criterion
  above — change them, if needed, only between pre-registered evaluation
  windows, and log the change like every other real decision in this
  project (a new D-number in `docs/DECISIONS.md`).

---

## 7. Why this doesn't contradict the project's own "no edge yet" verdict

`docs/PLAN.md`'s kill criteria are about not pretending a *validated,
capital-risking* model exists when Stage 1 hasn't cleared its gate. Nothing
here risks capital, and nothing here claims a model is validated — the
online policy's whole premise is "we don't know yet, so let it find out
safely, honestly scored." If it clears §6's bar, that *is* new evidence
worth taking seriously (a live-conditions, calendar-diverse, pre-registered
result the size of a full Stage 5 in `docs/PLAN.md` — real hit rate against
its own honestly-computed CI). If it doesn't, that's consistent with D83
and costs nothing but compute and time to have found out.

---

## 8. Proposed build order

1. `tape/online_policy.py` + unit tests (synthetic data only, no real
   tokens touched yet) — prove the learning mechanism itself is correct.
2. `scripts/paper_trade_replay.py` against the already-collected ~61-65
   token Store — prove the whole decide → resolve → update loop is wired
   correctly end to end, cheaply and reproducibly. Not expected to find an
   edge (§0) — a mechanical dry run.
3. `scripts/paper_trading_report.py` — build the evaluation machinery and
   pre-registered criteria (§6) *before* there's a live result to be
   tempted by.
4. `scripts/paper_trade_live.py`, extending `live_paper_monitor.py` —
   only after (1)-(3) look mechanically correct in replay. Runs continuously
   alongside the standing discovery/backfill loop.
5. Periodic reporting on the schedule in §6, no changes to the success
   criterion once a run is under evaluation.

---

## 9. Open decisions — resolved 2026-09-24 (D84, docs/DECISIONS.md)

All four confirmed as proposed, with one clarification:

- **v1 scope:** entry-only learning with a fixed exit (§3). Confirmed.
- **Exploration schedule:** `epsilon: 0.90 → 0.15`, decay scale 60 decisions.
  Confirmed.
- **Model family:** hand-rolled online logistic regression (§2), zero new
  dependencies. Confirmed.
- **Start mode:** "both" — build Phase A (replay) and Phase B (live)
  together in the same pass, rather than gating Phase B's construction on
  Phase A's results. Phase A still runs and gets checked first in practice
  (§8's build order is unchanged) — "both" changed what gets *built* now,
  not the order anything gets *run* in.

**A design refinement found while building, worth recording:** this turned
out not to be a classic partial-feedback bandit. A public blockchain's true
outcome is observable whether or not a paper position was taken, so the
online model is updated on EVERY resolved decision (entered or abstained),
not only on ones that were paper-traded. Epsilon-greedy exploration
controls which decisions get PAPER-TRADED (and therefore appear as PnL in
the ledger, which is what makes "idiotic mistakes, improves over time"
visible) rather than controlling what the model is allowed to learn from.
See `tape/online_policy.py`'s module docstring for the full reasoning.

**Built:** `tape/online_policy.py` (`OnlinePolicy`, `PaperRails`,
`evaluate_paper_rails`), `scripts/paper_trade_replay.py`,
`scripts/paper_trade_live.py`, `scripts/paper_trading_report.py`, and full
unit test coverage for all four (`tests/test_online_policy.py`,
`tests/test_paper_trade_replay.py`, `tests/test_paper_trade_live.py`). See
D84 in `docs/DECISIONS.md` for the complete record.
