# tape

One canonical stream of swaps, replayed live and offline by the same code, with
a calibrated probability and an explicit "I don't know" attached to every
decision.

**Paper trading only.** No code path here submits a transaction or touches a
wallet key, and none will until every gate in `docs/PLAN.md` §5 has been cleared.

## Read these first

| doc | what it answers |
|---|---|
| `docs/PLAN.md` | why this exists, what changes, build order, kill criteria |
| `docs/ARCHITECTURE.md` | how it is built and which boundaries are enforced by tests |
| `docs/DATA.md` | what to buy, what to collect, the canonical schema, storage |
| `docs/ML.md` | target, features, bars, weighting, validation, confidence machinery |
| `docs/MIGRATION.md` | what to port from v3, what to adapt, what to burn |
| `docs/DECISIONS.md` | every design call made here, with its reason, so it can be revisited |
| `docs/BOT.md` | the trading bot built on top of this (`tape/bot/`): the ACCUMULATE-AND-CONFIRM strategy, exits, auto-correct, runbook |

## The thesis, in one paragraph

v3 measured its own edge twice, on two independent populations, and got the
round-trip fee both times (−1.71% live against a −1.99% round trip; −2.085%
in research against the same). That is not bad luck, it is what an efficient
market looks like at the timescale it was trading. `tape` moves to where
information exists (deeper pools, longer tapes), attaches a calibrated
probability and a conformal credibility to each decision, abstains when either
is weak, and sizes by conviction so the book's outcome is dominated by the
trades the model was actually sure about.

## Run the tests

```bash
./run_tests.sh          # stdlib only, no install needed
```

43 tests across five files. They are not decoration — each one encodes a bug
that cost real money in a previous generation:

- `test_bars` — unattributed trades count for volume and *not* for breadth,
  and `None` is never `0`.
- `test_labels` — barrier ambiguity inside one bar resolves DOWN; entry is the
  close, not the open; overlapping labels are down-weighted.
- `test_no_lookahead` — truncate a tape, assert every surviving feature row is
  bit-identical. Plus a sanity check that the test itself is not vacuous.
- `test_costs_and_policy` — the fixed fee doubles a small position's round
  trip; unknown is never safe; rails cannot be overruled by a confident model;
  `decide()` is pure.
- `test_cv_and_layering` — no mint in both train and test; labels cannot import
  features; policy does no I/O.

## Layout

```
tape/
  schema.py      canonical Swap / Bar / TokenMeta / Decision
  store.py       Parquet + DuckDB
  bars.py        dollar / volume / time bars from a swap stream
  features.py    TokenState -- incremental, single-pass, cannot see the future
  labels.py      triple barrier + uniqueness weights. Cannot import features.
  cv.py          purged, embargoed, grouped splits + token bootstrap
  costs.py       proportional AND fixed cost, exact constant-product fills
  model.py       train -> isotonic calibration -> conformal -> artifact
  policy.py      decide(): rails -> p -> credibility -> abstain / size
  sources/       venue -> CanonicalSwap. Nothing downstream branches on venue.
config/spec.yaml every threshold, with a status and the run that fitted it
```

## Status

Scaffold; probe-ready. The contracts, the invariants and 74 tests are passing
(6 bars, 24 costs/policy, 8 CV, 7 env, 9 labels, 2 no-lookahead, 18 probe).
The Bitquery adapter (primary vendor — briefly swapped for Birdeye over an
account auth failure, reverted once that resolved; see D16/D17 in
`docs/DECISIONS.md`) awaits schema verification via `probe`.

**Next:** Run `PROBE_VERIFICATION_CHECKLIST.md` on your machine to verify
Bitquery's schema, side convention, and price calculations against v3 tapes.
Mint `3wTTDiEzMvBiy7Ue6b1L5Msa1Fgm5YSqTVp9cbmM3Ujf` is pre-selected with
207 windows of attributed volume for comparison. Once verified, implement
`historical()` and `stream()` in `tape/sources/bitquery.py`.

Nothing here has traded. The correct description is "the machinery is built and
proven against its own invariants", not "this trades".
