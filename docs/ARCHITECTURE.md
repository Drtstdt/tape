# Architecture

## The one rule

**There is exactly one decision function, and the backtester and the live bot
both call it.**

```python
decide(state: TokenState, model: Model | None, spec: Spec) -> Decision
```

Pure. Same state + same model + same spec = same decision, on any machine, in
any year. The only difference between backtest and live is which
`SourceAdapter` is producing the bars that built the state.

Every expensive bug in v3's history was an instance of "the live path does
something the tested path does not": the `decodeTradeEvent`/`decodeTradeEvents`
import that silently emptied every flow window; the flow window that summed all
trades ever instead of a window; the one-shot entry decision that was fixed in
the backtest and never in the bot; the Category-A exit that sold the position
twice. A single code path does not remove bugs, but it removes that entire
class of them.

## Layers

```
sources/       venue → CanonicalSwap. Nothing else leaves this directory.
store/         Parquet + DuckDB. Append-only for swaps; rebuilt for graphs.
bars/          CanonicalSwap stream → Bar stream. Dollar, volume, time.
features/      Bar stream → TokenState. Incremental, single-pass, no future.
labels/        Bar stream → labels. CANNOT import features.
cv/            Purged, embargoed, grouped splits.
model/         Train, calibrate, conformalise, persist an artifact.
policy/        decide(): rails → p̂ → credibility → abstain/size.
exec/          AMM math, cost model, fill simulation.
engine/        Replay engine and live engine. Both drive the same loop.
ledger/        Every fill, every decision, every abstention. Synchronous writes.
report/        Daily: PnL, calibration curve, drift, abstention rate, rail tally.
```

### Enforced boundaries

| boundary | enforced by |
|---|---|
| features never see the future | `test_no_lookahead`: truncate a tape, assert every surviving feature row is bit-identical |
| labels never see features | import check in `test_layering`; the two modules share no symbols |
| adapters emit only `CanonicalSwap` | adapter return type test per source |
| `decide()` is pure | called twice with the same inputs, asserted equal; no I/O in the module |
| reputation graphs are as-of-time | `test_asof`: a creator's day-T stats computed from a corpus truncated at T must equal the full-corpus value for day T |

That last one is the sneakiest leak available in this project, and the one that
produces the most convincing fake edge.

## The engine loop

Identical in both engines:

```
for swap in source:
    bars = bar_builder.push(swap)          # 0 or 1 closed bars
    for bar in bars:
        state.update(bar)                  # incremental, cheap
        if position_open(bar.mint):
            action = step_position(state, spec)
            if action: executor.apply(action)
        else:
            d = decide(state, model, spec)
            if d.action == "enter": executor.enter(d)
            ledger.record_decision(d)       # including abstentions
    clock.sweep(now)                        # wall-clock exits, independent of tape
```

Two details that are not incidental:

**`clock.sweep` is separate from the tape.** In v3, `stepPosition` was only ever
called from a curve-update callback, so a token that stopped trading produced no
ticks and therefore could never hit a time stop — 224 positions died at
subscription expiry at −14.8%. Exits that are about the passage of time must be
driven by the passage of time.

**Abstentions are recorded.** The tally of *why the bot did not act* is the most
informative output of any given day, more than the PnL, and it is the first
thing to read after a run.

## Spec

`config/spec.yaml` is the single source of truth for every threshold, resolved
`base → preset → band`. Loaded by both engines. Overriding a path absent from
the base spec is an error, never a silent no-op.

Every value carries `status: unvalidated | fitted | frozen` and, once fitted,
the run id that fitted it. A parameter nobody can trace to a measurement is
marked as such, so it cannot quietly acquire authority by being old.

## Why Python only

v3 keeps the strategy in JavaScript and Python and holds them together with
golden fixtures. That machinery is genuinely impressive and it has still leaked
real behavioural drift.

The justification for two runtimes would be latency. But `minAgeMs` exists
*precisely to remove this strategy from the latency race*, decision windows are
20 s to 15 min, and the fastest exit budget is minutes. There is nothing in the
strategy that a millisecond can express, so the second language buys nothing and
costs duplicated logic, a fixture apparatus, and a permanent class of drift bug.

The verified JS decoders are kept as an optional corroboration sidecar that
emits `CanonicalSwap` JSON on stdout. They are a data-integrity check against
the vendor feed, not a dependency of the strategy.
