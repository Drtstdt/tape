# tape-one: the trading bot

A trading layer built on the verified `tape` research pipeline. This
document describes the strategy, the exits, and the optional self-correcting
layer, with every parameter's status. It is MY strategy -- the project's
own docs explicitly forbid treating an unvalidated model as validated, so
every number below is marked `unvalidated` until a measurement says
otherwise, and the gates that would validate them are pre-registered in
`docs/PLAN.md`.

Paper trading by default. Real trading needs three independent gates
(`--live --keypair <path> --acknowledge`) and remains the operator's
responsibility.

---

## 1. The thesis: ACCUMULATE-AND-CONFIRM

v3's measured edge was the round-trip fee and nothing else, twice, on two
independent populations. The diagnosis in `docs/PLAN.md` is structural:
decisions from a 5-second-old snapshot of a 0.1 SOL pool, binary decisions
at flat size, exits on single observations.

My strategy is three inversions of that, in one sentence:

> **Buy only where a tape exists and accumulation is OBSERVED in it --
> positive net flow, buyer breadth, liquidity not draining, price moving
> WITH flow, nothing overextended, no creator dump -- then ask a calibrated
> model whether THIS kind of tape has won before, require conformal
> credibility ("have I seen this before"), and size by conviction.**

The two layers answer two different questions. The gate answers "is
accumulation happening right now" from MEASURED quantities only -- never
inferred ones (equities infer order flow from price because they cannot
see it; we can see it, so inferring it is strictly worse, `docs/ML.md`).
The model answers "given tapes like this one, how often did this band's
take-profit get hit before its stop" -- calibrated, so the number means
what it says, and conformal, so an unseen regime produces an abstention
instead of a guess wearing a probability's clothes.

Selectivity is what confidence looks like from the outside: the system is
designed to reject ~99% of what it sees and to be RIGHT about the 1%,
with a number attached to how right it expects to be.

## 2. Entry: `decide_entry()` (tape/bot/strategy.py)

One pure function, called identically by backtest and live. Order is
load-bearing:

```
1 rails    fail closed on any missing safety-critical field
2 gate     the accumulation thesis, on measured quantities only
3 model    calibrated probability + conformal credibility
4 sizing   Kelly, conviction-weighted, capped by depth and band
```

Rails can only reject; the gate can only veto; the model's probability is
the only number that sizes. Nothing the model says may overrule a rail --
a model fitted on a corpus where a rail was always true has no opinion
about what happens when it is false.

### Rails (`entry.*` in config/bot.yaml, status unvalidated)

| rail | value | why |
|---|---|---|
| liquidity >= band floor | 15 / 75 / 300 SOL | no tape, no trade (bonding-curve-only tokens are below B_small) |
| position <= 1% of pool depth | 1.0% | on a thin pool your own exit is the rug |
| age >= 150s | 150_000 ms | never buy the listing event: the reference token gapped -32.4% in 8 s and no stop could save it |
| bars >= 5 | 5 | enough tape for features to mean anything |
| unique buyers(10 bars) >= 3 | 3 | breadth needs real wallets |
| largest buyer share <= 0.6 | 0.6 | ten wallets buying beats one wallet buying ten times |
| top holder <= 80% | 80% | enforced when measured (TokenMeta); absent = not enforced, NOT passed |
| cost floor | 3x round trip | expected move must clear costs 3x (`tape/costs.py::cost_floor_ok`) |

Missing safety field = reject, always. `None` is not `0` (`tape/schema.py`).

### The accumulation gate (`entry.gate_*`)

Each check is a veto on a measured quantity; missing core flows fail
closed:

1. `netflow_10_over_liq > 0` -- the last 10 bars bought more than they sold,
   relative to depth. THE thesis condition.
2. `buyer_seller_breadth_10 > 0` -- more distinct buyers than sellers.
3. `flow_price_divergence <= 0` -- price is not rising while flow is
   negative (a substitute for a measured quantity; with the netflow
   requirement on, this is implied -- kept as a veto for configs that
   relax #1).
4. `liq_slope_10 >= -0.05` -- liquidity is not draining: the floor is not
   quietly leaving while you hold.
5. `rsi <= 85` -- not overextended. RSI is a veto, never a trigger: on the
   v3 reference token RSI read 15-28 through an entire -98.4% collapse,
   so `RSI<30 -> buy` buys all the way down; the symmetric error is
   buying the top of the spike.
6. `creator_sold_over_liq <= 0.10` -- creator not dumping (enforced when
   the field is measured).

### Model gate (`entry.probability_margin`, `min_credibility`)

With an artifact (`tape/model.py::ModelArtifact`, one per band):

- `credibility >= 0.15` -- inductive conformal, Mondrian: the fraction of
  calibration-set nonconformity scores at least as strange as this one.
  Low = "I have not seen this before" = abstain, regardless of p.
- `p_calibrated >= band.breakeven_win_rate + 0.04` -- breakeven is
  `|lower| / (upper-1 + |lower|)` per band (0.286 / 0.333 / 0.385 for
  B_small / B_mid / B_deep). The margin demands a real edge, not a coin
  flip.

Without an artifact the bot runs RULE-ONLY: rails + gate, flat sizing,
and every entry is logged `no_model_flat_size` -- a fallback that LOOKS
like a model decision is how an unvalidated number acquires authority, so
the fallback says what it is.

### Sizing (`entry.kelly_fraction`, `max_position_fraction`)

`f* = (p·b − (1−p))/b`, tenth-Kelly, then clamped by the band's max
position fraction and the pool-depth rail -- whichever binds first.
Kelly assumes you KNOW p; here p is estimated from a model fitted on a
market that changes regime in days, and overbetting is punished
superlinearly while underbetting costs only linearly. The asymmetry is
the whole argument for tenth-Kelly.

## 3. Exits (tape/bot/exits.py)

One position at a time per mint. Order inside a bar is load-bearing:

```
1 Category A   structural, overrides everything
2 hard stop    same-bar ambiguity resolves DOWN (before any TP)
3 take-profit partial   at band.upper_multiple, sells tp_fraction
4 trail        armed ONLY by the partial; floored at breakeven
5 time stop    clock-driven, independent of the tape
```

The three invariants ported from v3 (`docs/MIGRATION.md`), each one paid
for in real money:

- **The trail arms only on the profit-taking partial.** Arming on any
  partial measured -51.22% over 23 positions vs +3.71% over the other 316.
- **Category-A closes the position before returning** -- the phantom
  double-sale class of bug cannot happen.
- **Same-bar ambiguity resolves DOWN** -- bars carry no intra-bar path.

And two v3 lessons applied:

- **Exits fire on sustained observations.** Category A = liquidity/price
  divergence sustained over 2 bars, OR liquidity down 35% from its own
  peak. v3's single-observation exits fired 428 and 1,174 times at -14.8%
  and -8.5%; a rule that acts on n=1 has maximum variance by construction.
- **Time stops are driven by the clock** (`engine.sweep`), not by the
  tape: a token that stops trading still gets exited. v3's 224 positions
  died at -14.8% when the tape went quiet and nothing could sell them.

After the partial, the remainder rides the trail with a floor at
breakeven -- a winner never becomes a loser (`exits.remainder_floor_multiple`).

### Bands (exit economics per liquidity)

| band | floor (SOL) | TP | stop | horizon | trail |
|---|---|---|---|---|---|
| B_small | 15 | 2.0x | -40% | 45 min | 30% |
| B_mid | 75 | 1.6x | -30% | 3 h | 25% |
| B_deep | 300 | 1.4x | -25% | 12 h | 20% |

Deeper pools get longer horizons and smaller barriers; a thin pool must
pay a lot to justify the noise (all `unvalidated` starting values).

## 4. The engine (tape/bot/engine.py)

ONE loop, driven identically by backtest and live -- the only difference
is which source hands swaps to `on_swap` (`docs/PLAN.md` Sec 2.2, the
"live bot IS the backtester" rule). Per bar: state update, then exit step
or entry decision; every decision -- enter, abstain, reject -- is a
first-class ledger row with a reason code. The tally of WHY the bot did
not act is the most informative output of any run, more than the PnL.

Ledgers (CSV, append-only, `data/bot/`): `decisions.csv`, `trades.csv`,
`equity.csv`, `autocorrect_log.csv`.

## 5. Auto-correct (tape/bot/autocorrect.py, opt-in: `--autocorrect`)

Four mechanisms, each BOUNDED and each LOGGED with old value, new value,
and reason. A bot that changes its own mind must leave a paper trail at
least as good as a human's.

1. **Drift (always worth having, enabled by the flag).** Per-feature PSI
   of the live stream against the training quantiles (`tape/model.py::psi`;
   out-of-range values are clipped into the edge bins -- they are the
   strongest drift signal and the stock histogram would drop them). Above
   0.20: sizes halved. Above 0.30: new entries stop, retrain flagged. v1
   saw PSI 0.80 within 48 hours -- a model predicting a world that no
   longer exists must stop spending money in it.
2. **Rolling recalibration.** The isotonic mapping is refit on the union
   of the original calibration points and a rolling buffer of recent
   (p_raw, outcome) pairs (min 100 in buffer, 250 new since last refit),
   so "when it says 35%, it wins 35%" keeps meaning live. Conformal scores
   are NOT touched: credibility is a property of the fitting distribution,
   not of the market's mood.
3. **Bounded auto-tuning.** After every 30 closed trades (min 30 before
   the tuner speaks at all), the realized win rate over the window is
   compared to the band breakeven with +-0.02 hysteresis. Below breakeven:
   `probability_margin` +0.01 and `kelly_fraction` x0.8. Above: eased
   back. Bounds are pre-registered in the spec (`margin_bounds`,
   `kelly_bounds`) and can NEVER be crossed; the hysteresis + minimum
   sample are the anti-churn devices, the bounds are the anti-suicide
   device. Every change bumps the spec version and is logged.
4. **Stop-only safety (ALWAYS on, even without the flag).** Daily loss
   cap (10 SOL default: no new entries for the day), drawdown halt
   (25% from peak realized: halt until manual reset), and a kill-switch
   file (`data/bot/kill_switch` -- deleting it is the manual reset).
   These can only STOP trading, never loosen anything.

## 6. Running it

```
python scripts/run_bot.py doctor      # environment + keys + data + cache
python scripts/run_bot.py train       # parquet -> artifact per band (gated)
python scripts/run_bot.py backtest    # replay the store through the engine
python scripts/run_bot.py live        # Bitquery discovery + polling (paper)
python scripts/run_bot.py report      # the tally that matters
```

`live` uses `tape/sources/bitquery.py::historical()` polling (the realtime
dataset retains ~9h; `dataset: archive` when backfilling) plus
`discover()` for new pump.fun/PumpSwap mints. The websocket `stream()` is
still unverified in that adapter, so live = polling; the engine is
identical either way.

Real money: `--live --keypair <json> --acknowledge` (LiveExecutor, Jupiter
swap API, signed with solders). Unverified against live endpoints; treat
as a scaffold to validate yourself. The default is paper, forever, unless
all three gates are passed.

## 7. Honest limitations (read before believing any output)

- Per-token sequential replay: the backtest does not model cross-token
  capital lockup; positions are sized as small fractions of capital by
  construction, and the daily loss cap DOES operate on calendar days.
- `fixed_cost_sol` is unmeasured (the whole point of `tape/costs.py`'s
  docstring). Every PnL is optimistic by that amount until it is measured.
- The creator-reputation feature family needs the cross-token graph wired
  (`TokenMeta` / `Reputation`), which this corpus does not yet populate --
  the gate's creator check therefore only fires when the field exists.
- All thresholds are `unvalidated`. The fold gate on training is the first
  real filter; a model that fails it is not even written to disk. The
  rest of the validation ladder is `docs/PLAN.md` Sec 5 (live paper
  trading, calibration, drift) -- none of it is skipped here.

## 8. What would make me stop trading it (pre-registered)

1. An artifact fails its fold gate -- it is not saved, rule-only mode
   continues with everything logged.
2. Live hit rate lands materially below walk-forward expectation --
   the two paths measure different things; fix that before anything else.
3. The realized edge dies under 2x cost sensitivity (printed in every
   report) -- it was a cost artefact.
4. Daily loss cap / drawdown halt / kill switch -- automatic, stop-only.
