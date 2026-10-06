# Migration from TradingRPC

## Port — these cost real money to learn and they are correct

| from | to | note |
|---|---|---|
| `v3/js/src/pipeline/positionStateMachine.js` | `tape/policy/exits.py` | the 8-step order and, critically, its invariants: the trail arms **only** on a profit-taking partial (arming on any partial was −51.22% over 23 positions vs +3.71% over the other 316); the adaptive stop fires at most once; a Category-A full exit zeroes `remaining_fraction` before returning |
| `v3/js/src/pipeline/ceiling.js` | `tape/policy/ceiling.py` | `R = S/B`, `P_target/P_now = (R_target/R_now)²`, the at-or-above-ceiling screen, the ceiling-anchored fib ladder, the Kelly `b` cap |
| `v3/js/src/exec/executionModel.js` | `tape/exec/amm.py` | exact constant-product fills. Verified. Do not re-derive |
| `v3/js/src/blockchain/*`, `ingest/ammSwapDecoder.js` | `sidecar/` (optional) | empirically verified decoders → corroboration source, not the primary feed |
| `v3/python/research/*` leak discipline | `tape/` layering + tests | features and labels in modules that cannot see each other; block bootstrap over tokens; the survivorship warning in `queries/graduated_mints.graphql` |
| `v3/js/test/importIntegrity.test.js` | `tests/test_layering.py` | the test that would have caught the bug that voided a whole run |
| `v3/js/src/ingest/DeepPoolCollector.js` — the *design* | `tape/sources/` budget logic | three-tier discovery/watch/attribute is the right answer to an RPC budget. Mostly moot once swaps are bought parsed, but keep the reasoning for anything you still self-ingest |
| every `WHY` comment in v3 | keep the habit | this is the most valuable non-code asset in the repo |

## Adapt — right idea, wrong implementation

| from | change |
|---|---|
| `shared/pipeline_spec.json` | → `config/spec.yaml`; add `status` + `fitted_by_run` to every value |
| `DatasetRecorder.js` (JSONL) | → Parquet writer + DuckDB views |
| `flowWindow.js` | → `features/flow.py`, over bars rather than a rolling ms window; keep the `None ≠ 0` and `wallet=None` invariants verbatim |
| `entryGate.js` | split: hard rails stay as rails; the soft score is replaced by the model |
| `creatorStatsStore`, `entityClustersStore` | same tables, actually populated, with strict as-of-time semantics |
| `research/ablate.py` | keep the paired-bootstrap machinery; it is the right tool. Fix `ABLATION.md`'s headline first — `category_a_only` is byte-identical to `no_exits_at_all` (Category A never fires offline), so the top-line conclusion is an artifact of an arm that isn't what its name says |

## Burn

- `index_old.js`, `js/`, `python/`, `js/src/v2/` — three dead generations,
  ~9,500 files, and the root README describes the oldest of them.
- The archetype / sep-CMA-ES / Species machinery. Eight tuned archetypes, all
  negative. It was a thorough answer to the wrong question.
- `v1_snapshots`, `flowonly`, `deep_v1` research corpora **as training data**.
  All bonding-curve S_curve tapes — the band you are about to stop trading. Keep
  them for pipeline correctness tests; never fit anything on them.

Tag the commit before deleting. Everything is in git history.

## Order

1. `tape/` store + schema + Bitquery adapter, standalone. Nothing depends on v3.
2. Backfill. Verify vendor swaps against your own decoder on mints you already
   have tapes for, field by field.
3. Port exits, ceiling, AMM math (~600 lines of Python, mostly mechanical).
4. Bars, features, labels, CV. Run the Stage 1 information audit.
5. Only if Stage 1 passes: model, policy, live engine.
6. Run both systems in parallel for a week; compare rail tallies on the same
   tokens. Then archive v3.
