#!/usr/bin/env python3
"""PROOF OF CONCEPT -- pure schema introspection, zero real-data cost, same
discipline as `tape/scripts/bitquery_introspect.py` /
`bitquery_trading_protocol_check.py` (ask the live schema, never trust
scraped docs or memory).

WHY THIS EXISTS (direct follow-up to D56/D57): the user chose to pursue
genuine new-launch discovery via decoding on-chain `create` instructions
(over a bigger Bitquery-realtime backfill or stopping at D56's
underpowered result). `poc_pumpfun_create_discovery.py` tried the most
direct route -- Helius gTFA signature enumeration + per-signature
`getTransaction` decode -- and found, LIVE, that PUMPFUN_PROGRAM alone
sees ~184,000 signatures per 15 minutes (~736k/hour extrapolated): brute-force
per-signature decoding is not viable at ANY practically useful window, not
just a multi-day one. That rules out the direct-decode path as built.

This script checks whether Bitquery -- already paid for, already the
backbone of `tape/sources/bitquery.py::discover()` -- exposes a
DECODED-INSTRUCTION-LEVEL cube (commonly `Solana.Instructions` in
Bitquery's public Solana API, used in their own published examples for
"new token launch" trackers, but NEVER VERIFIED against this project's own
schema/entitlement, so not assumed here) that can filter server-side on
`Instruction.Program.Method` == "create" AND
`Instruction.Program.Address` == PUMPFUN_PROGRAM. If that field exists,
returns the accounts touched (ideally including the new mint), and a real
block time, this could replace BOTH prior discovery attempts at once:
a single cheap, server-side-filtered query gets exactly the rare `create`
events directly, with their real timestamp attached, no per-signature RPC
decode of a firehose of buy/sell traffic required.

If `Instructions` (or an equivalent) does NOT exist in this account's
schema, or exists but lacks the fields needed, this fails loud with the
schema's own field list -- never guesses a fallback.

    python scripts/poc_bitquery_instructions_schema.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.env import load_project_dotenv
from tape.scripts.bitquery_introspect import (
    _find_field, _post, _resolve_named_type, _type_fields, _type_input_fields,
)


def main() -> int:
    load_project_dotenv()
    api_key = os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    print("Step 1: root Query type -> 'Solana' field's type")
    query_type_name = _post("query { __schema { queryType { name } } }", api_key)["__schema"]["queryType"]["name"]
    root_fields = _type_fields(api_key, query_type_name)
    solana_field = _find_field(root_fields, "Solana")
    solana_type_name = _resolve_named_type(solana_field["type"])
    print(f"  Solana field type: {solana_type_name}")

    print("\nStep 2: EVERY field available on Solana (looking for an "
          "instruction/event-level cube, name not assumed in advance)")
    solana_fields = _type_fields(api_key, solana_type_name)
    all_names = sorted(f["name"] for f in solana_fields)
    print(f"  {len(all_names)} field(s): {all_names}")

    candidates = [n for n in all_names if "instruction" in n.lower() or "event" in n.lower()]
    print(f"\n  Candidate instruction/event-level field(s): {candidates or '(none found)'}")

    if not candidates:
        print("\nNo instruction-level field found on this account's Solana schema.")
        print("This means Bitquery (at least on this plan/dataset) cannot serve a")
        print("cheap server-side create-only filter the way DEXTradeByTokens serves trades.")
        print("Do NOT guess a different field name -- the full field list above is the")
        print("real answer. Next step would be checking Helius's Enhanced Transactions")
        print("'type' classification instead (separate, unverified path).")
        return 0

    for field_name in candidates:
        print(f"\n{'=' * 78}")
        print(f"Inspecting '{field_name}'")
        print("=" * 78)
        field = _find_field(solana_fields, field_name)
        args = field.get("args") or []
        print(f"  args: {[a['name'] for a in args]}")
        where_arg = next((a for a in args if a["name"] == "where"), None)
        if where_arg is None:
            print("  (no 'where' argument -- cannot filter server-side on this field)")
            continue
        where_type_name = _resolve_named_type(where_arg["type"])
        print(f"  where type: {where_type_name}")
        where_input_fields = _type_input_fields(api_key, where_type_name)
        where_names = [f["name"] for f in where_input_fields]
        print(f"  filterable on (top level): {where_names}")

        # The top level is Transaction/Instruction/ChainId/Block/any -- the
        # real filter fields (Program, Accounts, Data, ...) are ONE LEVEL
        # DEEPER, inside 'Instruction'. Descend into it explicitly rather
        # than searching for 'Program' at the wrong level (a real mistake
        # this script's first version made -- fixed here against the ACTUAL
        # first-run output, not guessed a second time).
        instr_where_field = next((f for f in where_input_fields
                                    if f["name"] == "Instruction"), None)
        if instr_where_field is not None:
            instr_where_type = _resolve_named_type(instr_where_field["type"])
            instr_where_fields = _type_input_fields(api_key, instr_where_type)
            instr_where_names = [f["name"] for f in instr_where_fields]
            print(f"  filterable on, inside 'Instruction': {instr_where_names}")

            program_field = next((f for f in instr_where_fields
                                   if f["name"].lower() == "program"), None)
            if program_field:
                program_type_name = _resolve_named_type(program_field["type"])
                program_input_fields = _type_input_fields(api_key, program_type_name)
                print(f"  'Instruction.Program' filter sub-fields: "
                      f"{[f['name'] for f in program_input_fields]}")
                method_field = next((f for f in program_input_fields
                                      if f["name"].lower() == "method"), None)
                if method_field:
                    method_type = _resolve_named_type(method_field["type"])
                    print(f"    Method's type: {method_type}")
                    enum_data = _post(
                        f'query {{ __type(name: "{method_type}") {{ kind enumValues {{ name }} }} }}',
                        api_key)
                    et = enum_data["__type"]
                    if et and et.get("kind") == "ENUM":
                        vals = [v["name"] for v in (et.get("enumValues") or [])]
                        has_create = [v for v in vals if "create" in v.lower()]
                        print(f"    Method IS an enum, {len(vals)} values. "
                              f"Values containing 'create': {has_create or '(none)'}")
                    else:
                        print(f"    Method is NOT an enum (kind={et.get('kind') if et else None}) "
                              f"-- it's a free string; exact value must be confirmed with a "
                              f"real query against a known-recent pump.fun mint.")

        # Output shape: the field's own return type -> what can be SELECTED,
        # not just filtered. This is what tells us whether the created mint
        # is actually retrievable (e.g. an 'Accounts' list) without a
        # separate RPC call. Same one-level-deeper correction as above: the
        # cube's top-level selectable fields are Block/Instruction/Transaction/
        # stats-functions -- the real payload is inside 'Instruction'.
        return_type_name = _resolve_named_type(field["type"])
        print(f"\n  Return (cube) type: {return_type_name}")
        return_fields = _type_fields(api_key, return_type_name)
        return_names = [f["name"] for f in return_fields]
        print(f"  selectable top-level fields: {return_names}")

        instr_return_field = next((f for f in return_fields
                                     if f["name"] == "Instruction"), None)
        if instr_return_field is None:
            print("  (no 'Instruction' field on the return cube -- unexpected, stopping here)")
            continue
        instr_return_type = _resolve_named_type(instr_return_field["type"])
        print(f"\n  'Instruction' return type: {instr_return_type}")
        instr_return_fields = _type_fields(api_key, instr_return_type)
        instr_return_names = [f["name"] for f in instr_return_fields]
        print(f"  selectable fields inside 'Instruction': {instr_return_names}")

        for sub_name in instr_return_names:
            if sub_name.lower() in ("program", "accounts"):
                sub_field = _find_field(instr_return_fields, sub_name)
                sub_type_name = _resolve_named_type(sub_field["type"])
                print(f"\n    '{sub_name}' return type: {sub_type_name}")
                try:
                    sub_fields = _type_fields(api_key, sub_type_name)
                except RuntimeError:
                    sub_fields = _type_input_fields(api_key, sub_type_name)
                print(f"      sub-fields: {[f['name'] for f in sub_fields]}")

    print("\n" + "=" * 78)
    print("Interpretation")
    print("=" * 78)
    print("  If a candidate field has a 'Program' where-filter with 'Method' and")
    print("  'Address' sub-fields, AND an 'Accounts'-like selectable output field:")
    print("  a single query filtering Program.Address=PUMPFUN_PROGRAM,")
    print("  Program.Method=\"create\" could return real create events directly,")
    print("  with Block.Time as the authoritative timestamp -- no Helius RPC decode")
    print("  needed at all. Confirm the ACTUAL method name (\"create\" vs something")
    print("  else) before trusting it -- if Method is an enum, list its values; if")
    print("  it's a free string, a small real query (not run by this script) against")
    print("  a known-recent pump.fun mint would confirm the exact string used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
