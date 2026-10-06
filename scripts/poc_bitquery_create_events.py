#!/usr/bin/env python3
"""PROOF OF CONCEPT -- first REAL (non-introspection) query against
Bitquery's `Instructions` cube, following D57's schema-introspection
result: `Solana.Instructions` exists, is filterable on
`Instruction.Program.Address` and `Instruction.Program.Method` (a free
string -- `Solana_Instruction_Filter`'s `Program.Method` field is typed
`OLAP_String`, not an enum, per the live schema), and its return shape
includes `Instruction.Accounts { Address IsWritable Token }` AND
`Instruction.Program.AccountNames` (a parallel list of account NAMES, in
the same order) -- meaning, if this works, a single cheap query gives the
new mint's address directly (by matching "mint" in AccountNames to its
positional Accounts entry) with ZERO Helius RPC decode needed, fixing
`poc_pumpfun_create_discovery.py`'s cost/scale dead end (D57: ~736k
signatures/hour makes brute-force decode infeasible) at the root.

`Method`'s exact string value has never been confirmed against this
project's data -- not guessed, since D3 forbids that. First real run
(2026-09-22, D58 follow-up): `Method: {is: "create"}` returned ZERO rows
over a 2-hour window, and an unfiltered 2-minute sample of the SAME
program showed real `Method` values of `buy`, `buy_exact_quote_in_v2`,
`buy_exact_sol_in`, `sell`, `sell_v2` -- AND `TradeEvent`, which is not an
instruction name in pump.fun's IDL at all. `TradeEvent` matches a very
common Anchor pattern this project hadn't previously encountered: a
"self-CPI logged event" (the program invokes itself via CPI with a
synthetic instruction whose 8-byte discriminator is
`sha256(f"event:{EventName}")[:8]`, distinct from the
`sha256(f"global:{InstructionName}")[:8]` scheme `poc_idl_classify.py`
already uses for real instructions) -- Anchor IDLs declare these under a
separate top-level `"events"` array, which no script in this project has
ever looked at (`poc_idl_classify.py` only ever reads `idl["instructions"]`).
If pump.fun's IDL has an event named e.g. `CreateEvent` (symmetric with
the observed `TradeEvent`), THAT -- not the instruction name `"create"`
-- is very likely what Bitquery's `Method` field reports for a token
launch, since Bitquery appears to be classifying by whichever
discriminator matched, instruction or event alike.

This script therefore does not stop at one guess: it fetches the live IDL
and builds its OWN candidate list from real evidence (every instruction
name AND every event name that contains "create", case-insensitively --
not asserting `CreateEvent` is the right name in advance, just proposing
it as a candidate to actually test), tries `--method` first, then each
auto-derived candidate in turn, and reports the hit count for each. Only
if every candidate comes back empty does it fall back to an unfiltered
recent sample, exactly as the first version did.

Kept deliberately narrow in row count (small `limit`, `dataset: realtime`
only) -- `create`/`CreateEvent`-type events are rare relative to the
~736k/hour buy/sell firehose D57 found, so even a couple of hours of
window should be enough without pulling anything expensive.

Writes nothing to the Store. Read-only.

    python scripts/poc_bitquery_create_events.py --minutes-back 120
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.env import load_project_dotenv
from tape.sources.bitquery import PUMPFUN_PROGRAM, _ms_to_bq_iso
from tape.scripts.bitquery_introspect import _post, _type_input_fields

ENDPOINT = "https://streaming.bitquery.io/graphql"
PUMPFUN_API_BASE = "https://frontend-api-v3.pump.fun"
PUMP_IDL_URL = "https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump.json"
MAX_RETRIES = 5
RETRY_BACKOFF_CAP_S = 30.0


def fetch_create_method_candidates(user_method: str) -> list:
    """Real evidence, not a second blind guess: pull BOTH `instructions`
    AND `events` (the latter never checked by any prior script in this
    project -- see module docstring's `TradeEvent` finding) from pump.fun's
    live official IDL, and propose every name containing "create"
    (case-insensitive) from either list as a candidate `Method` value.
    `user_method` is always tried first regardless of what's found here."""
    candidates = [user_method]
    try:
        resp = httpx.get(PUMP_IDL_URL, timeout=30.0)
        resp.raise_for_status()
        idl = resp.json()
    except Exception as e:
        print(f"  (could not fetch live IDL for candidate discovery: {e} -- "
              f"only trying --method={user_method!r})")
        return candidates
    instr_names = [i.get("name") for i in (idl.get("instructions") or [])]
    event_names = [e.get("name") for e in (idl.get("events") or [])]
    print(f"  IDL instructions ({len(instr_names)}): {sorted(instr_names)}")
    print(f"  IDL events ({len(event_names)}): {sorted(event_names)}")
    for n in instr_names + event_names:
        if n and "create" in n.lower() and n not in candidates:
            candidates.append(n)
    return candidates


def _post_query(query: str, variables: dict, api_key: str) -> dict:
    """Same 429 retry/backoff discipline as `tape/sources/bitquery.py`'s
    `_post_with_retry` (D44) -- kept as its own small copy, same reasoning
    as every other per-script helper in this project (scripts/ doesn't
    share code with sources/, sources/ doesn't share code between
    adapters)."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = httpx.post(
            ENDPOINT,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30.0,
        )
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait_s = float(retry_after) if retry_after else min(2 ** attempt, RETRY_BACKOFF_CAP_S)
            print(f"  429 rate limited, waiting {wait_s:.0f}s (retry {attempt}/{MAX_RETRIES})",
                  file=sys.stderr)
            time.sleep(wait_s)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:2000]!r}")
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Bitquery returned errors: {body['errors']}")
        return body["data"]
    raise RuntimeError(f"Still 429 after {MAX_RETRIES} retries.")


CREATE_QUERY = """
query CreateEvents($program: String!, $method: String!, $since: DateTime!, $until: DateTime!, $limit: Int!) {
  Solana(dataset: realtime) {
    Instructions(
      limit: {count: $limit}
      orderBy: {ascending: Block_Time}
      where: {
        Instruction: { Program: { Address: {is: $program}, Method: {is: $method} } }
        Block: { Time: {after: $since, before: $until} }
      }
    ) {
      Block { Time }
      Transaction { Signature }
      Instruction {
        Accounts { Address IsWritable }
        Program { Address Method Name AccountNames }
      }
    }
  }
}
"""

SAMPLE_QUERY = """
query RecentSample($program: String!, $since: DateTime!, $until: DateTime!, $limit: Int!) {
  Solana(dataset: realtime) {
    Instructions(
      limit: {count: $limit}
      orderBy: {descending: Block_Time}
      where: {
        Instruction: { Program: { Address: {is: $program} } }
        Block: { Time: {after: $since, before: $until} }
      }
    ) {
      Block { Time }
      Instruction { Program { Method } }
    }
  }
}
"""


def cross_check_pumpfun_api(mint: str) -> dict:
    try:
        resp = httpx.get(f"{PUMPFUN_API_BASE}/coins/{mint}", timeout=15.0,
                         headers={"accept": "application/json"})
        if resp.status_code != 200:
            return {"status": "error", "created_timestamp": None, "error": f"HTTP {resp.status_code}"}
        return {"status": "ok", "created_timestamp": resp.json().get("created_timestamp")}
    except Exception as e:
        return {"status": "error", "created_timestamp": None, "error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes-back", type=float, default=120.0)
    ap.add_argument("--method", default="create",
                     help="the Program.Method string to filter on -- 'create' is "
                          "informed by the live IDL (D47), not confirmed against "
                          "Bitquery's own decoding until this script runs")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--cross-check-sample", type=int, default=5)
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    print("Step 0: confirm OLAP_String's real comparison operators (free introspection,")
    print("        so 'is' below isn't itself a guess)")
    ops = _type_input_fields(api_key, "OLAP_String")
    op_names = [f["name"] for f in ops]
    print(f"  OLAP_String operators: {op_names}")
    if "is" not in op_names:
        print(f"  WARNING: 'is' is not among the real operators above -- the queries below "
              f"will likely fail with a schema error naming the real one; do not silently "
              f"keep retrying 'is'.")

    now_ms = int(time.time() * 1000)
    since_ms = now_ms - int(args.minutes_back * 60_000)
    since_iso = _ms_to_bq_iso(since_ms)
    until_iso = _ms_to_bq_iso(now_ms)

    print(f"\n{'=' * 78}")
    print("STEP 0.5: build Method candidates from the live IDL (instructions AND "
          "events -- see module docstring's TradeEvent finding)")
    print("=" * 78)
    candidates = fetch_create_method_candidates(args.method)
    print(f"\n  Trying, in order: {candidates}")

    winning_method = None
    rows = []
    for method in candidates:
        print(f"\n{'=' * 78}")
        print(f"STEP 1: filtered query -- Program.Address={PUMPFUN_PROGRAM}, "
              f"Program.Method={method!r}, last {args.minutes_back} min")
        print("=" * 78)
        data = _post_query(CREATE_QUERY, {
            "program": PUMPFUN_PROGRAM, "method": method,
            "since": since_iso, "until": until_iso, "limit": args.limit,
        }, api_key)
        candidate_rows = data["Solana"]["Instructions"]
        print(f"  {len(candidate_rows)} row(s) returned for Method={method!r}")
        if candidate_rows:
            winning_method = method
            rows = candidate_rows
            break

    if not rows:
        print(f"\n  Zero rows for every candidate tried ({candidates}). Falling back to an "
              f"UNFILTERED small recent sample (Program.Address only) to see the REAL "
              f"Method values actually present -- not guessing a different string blind.")
        print("\n" + "=" * 78)
        print("STEP 2 (fallback): unfiltered recent sample")
        print("=" * 78)
        sample_since_iso = _ms_to_bq_iso(now_ms - 2 * 60_000)  # last 2 min, unfiltered is high-volume
        sdata = _post_query(SAMPLE_QUERY, {
            "program": PUMPFUN_PROGRAM, "since": sample_since_iso, "until": until_iso,
            "limit": 30,
        }, api_key)
        srows = sdata["Solana"]["Instructions"]
        methods_seen = sorted({(r.get("Instruction") or {}).get("Program", {}).get("Method")
                                for r in srows})
        print(f"  {len(srows)} row(s) in the last 2 minutes (unfiltered)")
        print(f"  distinct Method values seen: {methods_seen}")
        print("\n  Re-run with --method set to whichever of the above looks like a real")
        print("  token-creation event (or widen --minutes-back if none of these are it --")
        print("  create is rare, 2 minutes of unfiltered sampling may simply have missed it).")
        return 0

    print(f"\n  >>> Method={winning_method!r} is the one that worked. <<<")

    print("\n  Sample hits:")
    hits_for_crosscheck = []
    for r in rows[:20]:
        block_time_ms = None
        bt = (r.get("Block") or {}).get("Time")
        sig = (r.get("Transaction") or {}).get("Signature")
        instr = r.get("Instruction") or {}
        accounts = instr.get("Accounts") or []
        program = instr.get("Program") or {}
        account_names = program.get("AccountNames") or []
        mint = None
        if "mint" in [n.lower() for n in account_names]:
            idx = [n.lower() for n in account_names].index("mint")
            if idx < len(accounts):
                mint = (accounts[idx] or {}).get("Address")
        print(f"    sig={str(sig)[:16]}...  block_time={bt}  mint={mint}  "
              f"n_accounts={len(accounts)}  n_names={len(account_names)}")
        if mint and bt:
            hits_for_crosscheck.append((mint, bt))

    print(f"\n  {len(rows)} total {winning_method!r} event(s) in the window -- extrapolated rate: "
          f"~{len(rows) / args.minutes_back * 60:.1f}/hour "
          f"(compare to D57's ~736,000/hour TOTAL signature volume: creates are a tiny "
          f"fraction, exactly why this server-side filter is worth using instead of "
          f"brute-force decode).")

    print("\n" + "=" * 78)
    print(f"STEP 3: cross-check up to {args.cross_check_sample} hit(s) against pump.fun's "
          f"OWN frontend API")
    print("=" * 78)
    for mint, bt in hits_for_crosscheck[: args.cross_check_sample]:
        api_rec = cross_check_pumpfun_api(mint)
        api_ts = api_rec.get("created_timestamp")
        our_dt = dt.datetime.fromisoformat(bt.replace("Z", "+00:00")) if bt else None
        our_ms = int(our_dt.timestamp() * 1000) if our_dt else None
        if api_rec["status"] == "ok" and isinstance(api_ts, (int, float)) and our_ms is not None:
            delta_s = (our_ms - int(api_ts)) / 1000.0
            print(f"  {mint}  our_block_time={bt}  pumpfun_api_created_timestamp={int(api_ts)}  "
                  f"delta={delta_s:+.1f}s")
        else:
            print(f"  {mint}  our_block_time={bt}  pump.fun API: status={api_rec['status']} "
                  f"({api_rec.get('error', 'no created_timestamp field')})")
        time.sleep(0.15)

    print("\nInterpretation:")
    print(f"  - Non-zero rows + mint successfully extracted -> Method={winning_method!r} is")
    print("    confirmed correct, and Bitquery's Instructions cube can fully replace the")
    print("    Helius brute-force decode path for discovery.")
    print("  - delta close to 0s in Step 3 for every hit -> Block.Time is genuinely the")
    print("    real creation time, same conclusion D57's PoC was trying to reach, now via")
    print("    a query that's actually affordable to run over a wide calendar window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
