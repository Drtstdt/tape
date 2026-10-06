# Pipeline execution plan — D39 onward: spending the $100/mo Bitquery archive well

Written 2026-09-22, after the archive add-on was purchased. This is the
concrete, sequenced "what to actually run, in what order, and what you'll
see at each step" — `docs/PLAN.md` is the architecture and the why,
`docs/DECISIONS.md` is the verified-fact ledger, this is the execution
checklist that ties both to visible bot progress.

> **STATUS UPDATE, same day (D40, docs/DECISIONS.md): Phase 0's smoketest
> came back CONFIRMED-BLOCKED, not confirmed-working.** Bitquery's own
> error message is unambiguous: `"access restricted: your plan only
> allows \"realtime\", but the request uses
> \"archive:solana:DEXTradeByTokens\""`. The purchased $100/mo plan does
> not include archive access to the query type this whole plan depends on
> — this needs to be resolved with Bitquery support/billing (quote them
> that exact permission string) before Phases 1-2 below can run at all.
> **This does not have to block all progress** — §10 below is the interim
> plan for what to run in the meantime.

## 0. Honest checkpoint — what's already built vs. what isn't

More of the ML programme (`docs/PLAN.md` §5) is already **written** than
the recent conversation focused on data plumbing might suggest:

| piece | status |
|---|---|
| `Store` (Parquet+DuckDB), `CanonicalSwap` schema | done, in use |
| `BarBuilder` (dollar/volume bars), `TokenState` (features) | done (`tape/bars.py`, `tape/features.py`) |
| Labels (triple-barrier), purged CV, cost model | done (`tape/labels.py`, `tape/cv.py`, `tape/costs.py`) |
| `decide()`, `Rails`, `ConfidencePolicy` | done (`tape/policy.py`) — the actual decision function the whole plan is built around |
| `ModelArtifact` / train-calibrate-conformalize scaffolding | done (`tape/model.py`) — not yet run against real data |
| **Stage 1 gate script** (`scripts/information_audit.py`) | done, fully wired to `Store.mints()` — has never been run for real, blocked only on data volume |
| Bitquery adapter (`historical()`, now `discover()`) | done, `archive`-aware as of D39 — **not yet run against a live response** |
| Helius adapter (`historical()`, `from_signatures()`) | done, verified (D34) |
| **Stage 0 gate** (replay a recorded live day through `decide()`, confirm it reproduces that day's ledger) | **not built** — no script does this yet |
| Live engine, ledger, drift monitor, daily report (`docs/PLAN.md` §10 items 7-8) | not built |

The practical read: this project is much closer to "run the Stage 1 gate
for the first time" than to "still writing infrastructure". The paid
archive's whole job is removing the one blocker in front of that —
`store.mints()` returning at least 30 mints with at least 50 swaps each.

## 1. Two paid/free resources, used for what each is actually good at

- **Bitquery (archive, $100/mo, paid)** — the PRIMARY, wide-universe path
  from here on. One schema, one vendor, discovery AND backfill in the same
  query type (`DEXTradeByTokens`). This is what the plan always wanted
  Bitquery for (`docs/DATA.md` §1) — the free-source detour (D25-D38:
  BigQuery, `Trading.Trades`, SolArchive, the Helius-as-primary workaround)
  was necessary *because archive was declined*, not because it was ever
  the better path. Now it isn't declined.
- **Helius (free tier)** — demoted back to its originally-intended role
  (`docs/PLAN.md` §2.3): a free **corroboration** source, not the primary
  backfill engine. Note this is the *free* tier, not the $49/mo Developer
  plan `docs/DATA.md` budgeted — expect the 429 rate limiting
  `tape/sources/helius.py` already handles (a free key hits it after ~70
  unthrottled calls) if it's leaned on harder than a light sample.

## 2. Phase 0 — verify before spending a single real query (today, ~30-60 min)

**Non-negotiable, same discipline as every other adapter in this
project.** Nothing about `dataset: archive` on `DEXTradeByTokens`
specifically has been tested — D21's ~9-12h `realtime` boundary and
D26-D30's "archive reaches ~26+ days" evidence are both about *different*
cubes/datasets than the one this plan now depends on.

```
python -m tape.scripts.bitquery_archive_smoketest \
    --mint 3tUn8dGmDceJ9pCwFLUVo2jkD6FLWaZ78YEpAa3bpump \
    --days-ago 1,3,7,14,30,60,90
```

This answers two things, live:
1. How far back does `archive` actually reach on this cube? (bisects the
   same way D21 pinned `realtime`'s boundary)
2. Does `discover()`'s program-id filter actually return real rows?

**Log the result as D40 in `docs/DECISIONS.md` before Phase 2.** If archive
reaches less far than hoped, that changes `--since` in every step below —
better to know now than after a large backfill run assumes a window that
was never really being served.

Also worth 5 minutes in Bitquery's own dashboard: confirm what the $100/mo
actually includes (row/query allowance, not just "archive access") so
Phase 2's batch sizing has a real budget to plan against instead of an
assumption.

## 3. Phase 1 — universe discovery (cheap; the actual unlock)

```
python scripts/backfill_bitquery.py --since <N months ago> --until today \
    --mints-file data/bitquery_discovered_mints.txt --limit 0   # limit=0: discover only, see below
```

(If `--limit 0` isn't convenient, run with a small `--limit` — discovery
always runs to completion regardless of `--limit`, which only caps how
many discovered mints get backfilled *this run*.)

**This is the first visible number**, likely within the hour: "N mints
discovered trading pump.fun/PumpSwap in this window" — almost certainly
in the hundreds to low thousands, not 50. That number alone is progress
you can show yourself before a single swap gets backfilled.

## 4. Phase 2 — bulk backfill (where the $100 actually gets spent)

```
python scripts/backfill_bitquery.py --since <same> --until <same> \
    --mints-file data/bitquery_discovered_mints.txt --limit 200 --corroborate-every 20
```

Batch it (`--limit 200`, then raise) rather than pointing it at the full
discovered universe on the first run — same reasoning `scripts/backfill.py`
always used: prove the pipeline end to end on a small batch (watch the
pace/ETA output, check a few mints' swap counts look sane) before
committing hours of quota to the rest.

`--corroborate-every 20` spends a little of the free Helius allowance to
spot-check every 20th mint against the already-verified `HeliusSource` —
real, ongoing data-integrity insurance, not just a one-time proof (D34).

## 5. Phase 3 — Stage 1 gate: the first real answer (bot progress, milestone #1)

```
python scripts/information_audit.py --data data
```

This has been ready and waiting the entire time — it only needed data.
Once Phase 2 clears 30+ mints at 50+ swaps each (should happen well before
the full discovered universe is backfilled), this produces the
**pre-registered verdict** `docs/PLAN.md` §9 committed to before seeing any
results: `signal_present` (AUC ≥ 0.55 on held-out tokens, on at least one
feature family) or `no_edge_found`. This is genuinely "progress in the
bot" in the sense that matters — not more infrastructure, an actual
empirical answer, honestly gated, that determines whether Stage 2 (training
a real model) is worth doing at all.

Per `docs/PLAN.md` §10: this step is about one day of work once data is in
place. It is the one that tells you whether the rest is worth doing.

## 6. Phase 4 — Stage 0 gate: build it now, run it once real breadth exists

**Not yet built.** `docs/PLAN.md`'s own Stage 0 gate ("replay a recorded
live day through `decide()`, confirm it reproduces that day's ledger
within rounding") has no script yet — everything built so far (bars,
features, labels, `decide()` itself) has never been run end-to-end against
a real recorded day. v3's actual ledger data is on disk already
(`D:\TradingRPC\ledger`, `D:\TradingRPC\history`) — this is a genuinely
small, high-value script: load one day's v3 ledger, replay the matching
window through `ReplaySource` → bars → features → `decide()`, diff the
resulting decisions against what the ledger actually did. Worth doing in
parallel with Phase 2's backfill (it doesn't need the new Bitquery data at
all — it uses v3's own recorded history), not after it.

## 7. Phase 5 — if Stage 1 passes: train the first real model (bot progress, milestone #2)

`tape/model.py`'s train → calibrate → conformalize pipeline is written but
has never been run against real labeled data. Once Stage 1 says
`signal_present`, this is the next concrete, visible artifact: a real
`ModelArtifact` on disk, with a reliability curve and per-fold hit rates —
`docs/PLAN.md` §5's Stage 2 gate (top-decile hit rate beats the 29.6%
breakeven in ≥4 of 5 purged folds).

## 8. Sequencing summary

```
Phase 0  bitquery_archive_smoketest.py           ~30-60 min   →  D40 logged
Phase 1  backfill_bitquery.py (discover only)     minutes-hours →  universe size known
Phase 2  backfill_bitquery.py (batched backfill)  hours, batched →  Store has real breadth
Phase 3  information_audit.py                     ~1 day        →  signal_present / no_edge_found
Phase 4  (new) Stage 0 replay-vs-ledger script     parallel to Phase 2, uses v3's own ledger
Phase 5  tape/model.py training run (if Phase 3 passes)          →  first real ModelArtifact
```

Phases 0-2 are the direct answer to "use the $100 to its fullest". Phase 3
is the direct answer to "see progress in the bot" — it is the first point
in this entire rebuild where a real, pre-registered, honest verdict about
whether there is anything to trade on gets produced.

## 9. Open verification items (do not skip, do not assume)

- Archive's real retention depth on `DEXTradeByTokens` (Phase 0).
- `discover()`'s `Trade.Dex.ProgramAddress` where-filter actually working
  as written (Phase 0) — if it errors, the where-clause nesting needs
  fixing before Phase 1 can run at all.
- Bitquery's OAuth client_credentials response shape (`_fetch_oauth_token`,
  D39) — untested; matters most for a long unattended run past ~5h, not a
  single batched invocation, so it's fine to defer confirming this until a
  run actually spans that long.
- Whatever row/query allowance the $100/mo plan actually carries — check
  Bitquery's own dashboard before assuming Phase 2 can run the full
  discovered universe in one pass.

## 10. Interim plan while Bitquery archive access is being sorted out (D40)

Phase 0 (§2) came back CONFIRMED-BLOCKED: `"your plan only allows
\"realtime\", but the request uses \"archive:solana:DEXTradeByTokens\""`
— a real Bitquery entitlement gap, not a bug here. Resolving it is a
billing/support conversation, not something to route around in code.
**Two things still move forward today without waiting on that:**

**10.1 — Bitquery on `realtime` (already paid for, already verified,
zero new cost).** `discover()` and `historical()` both work fine on
`dataset: realtime` (D19-23) — just can't reach back more than ~9-12h
(D21). Running discovery now starts building the universe FORWARD from
today at no extra cost:

```
python scripts/backfill_bitquery.py --last-hours 8 --skip-discover \
    --mints-file data/bitquery_discovered_mints.txt --limit 50 --dataset realtime
```

(`--dataset realtime` is the fallback flag added for exactly this
situation — the window must fall inside `realtime`'s ~9-12h reach, D21, or
`historical()` raises loud rather than silently returning nothing.
`--last-hours` (D45, docs/DECISIONS.md) is REQUIRED here, not
`--since`/`--until`: those are date-only, and a bare date can already be
many hours stale by the time the command runs — confirmed live when a
`--since <today's date>` backfill failed the retention check because it
was ~15h+ old the moment `historical()` ran. `--last-hours` computes the
window to the second, at run time, so it's always inside the real reach.
`--skip-discover` reuses an already-discovered mints file, since
`discover()` itself has no such floor — it's only `historical()`'s
per-mint backfill that's this tightly bounded.) Re-run this every few
hours to keep extending real, verified coverage going forward — not a
substitute for deep history, but genuinely free progress.

**10.2 — The free BigQuery+Helius pipeline (D38), unaffected by any of
this.** Built and unit-tested before the Bitquery purchase, still fully
usable:

```
python -m tape.scripts.bigquery_discover --since <N months ago> --until today --print-query
# run the printed dry-run, check bytes, then the real query, export JSON
python -m tape.scripts.bigquery_discover --load discovery_export.json
python scripts/backfill_wide.py --signatures data/discovered_signatures.txt --limit 5000
```

This is the path to real historical depth (months back, not just the last
9h) while Bitquery support sorts out the archive entitlement. Once that's
resolved, Phases 1-2 (§3-4) take over as the primary path and this becomes
the free cross-check `docs/PLAN.md` §2.3 always intended Helius/BigQuery
for, rather than the main route.

**10.3 — Phase 3's Stage 1 gate (§5) doesn't care which of the above fed
the Store.** `information_audit.py` just needs ≥30 mints × ≥50 swaps —
run it against whatever §10.1/§10.2 have produced so far to see where
things stand, then re-run once the archive backfill lands a much wider
corpus.
