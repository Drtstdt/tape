"""One-shot GraphQL schema introspection against Bitquery -- ask the live
schema for the exact field names instead of trusting scraped docs, which
have disagreed with each other on `since`/`till` vs `after`/`before` vs
`since_relative` across different pages during this project's own probe
debugging (see D19/D20, docs/DECISIONS.md).

Walks: Query -> `Solana` field's type -> `DEXTradeByTokens` field's `where`
argument's input type -> its `Block` field's type -> its `Time` field's
type -> that type's own input fields, which ARE the allowed time-range
operator names (e.g. "since", "till", "after", "before" -- whichever this
account's schema version actually accepts).

    python -m tape.scripts.bitquery_introspect

Prints each step so a schema-shape surprise (e.g. `DEXTradeByTokens` renamed,
or nested one level differently) is visible immediately rather than crashing
opaquely three calls in.
"""

from __future__ import annotations

import json
import os
import sys

from ..env import load_project_dotenv

ENDPOINT = "https://streaming.bitquery.io/graphql"


def _post(query: str, api_key: str) -> dict:
    import httpx

    resp = httpx.post(
        ENDPOINT,
        json={"query": query},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body and body["errors"]:
        raise RuntimeError(f"Introspection query failed: {json.dumps(body['errors'], indent=2)}")
    return body["data"]


# A GraphQL field's type can be wrapped up to 3 deep in practice --
# `[Trading_Trade!]!` is NON_NULL(LIST(NON_NULL(Trading_Trade))), three
# wrappers before the real name. The original version of this constant only
# unwrapped ONE level of `ofType`, which silently returned `name: None` for
# any field wrapped deeper than that (found live, 2026-09-21, on
# `Trading.Trades`'s own return type -- not a Bitquery-specific quirk, a
# generic GraphQL introspection gap in this helper). Four levels of `ofType`
# leaves a spare level of margin over the realistic maximum.
_DEEP_TYPE_REF = (
    "name kind ofType { name kind ofType { name kind ofType { "
    "name kind ofType { name kind } } } }"
)


def _type_fields(api_key: str, type_name: str) -> list[dict]:
    """Fields of an OBJECT type (has args)."""
    data = _post(
        f'query {{ __type(name: "{type_name}") {{ name kind fields '
        f'{{ name args {{ name type {{ {_DEEP_TYPE_REF} }} }} '
        f'type {{ {_DEEP_TYPE_REF} }} }} }} }}',
        api_key,
    )
    t = data["__type"]
    if t is None:
        raise RuntimeError(f"Type '{type_name}' does not exist in this schema.")
    return t.get("fields") or []


def _type_input_fields(api_key: str, type_name: str) -> list[dict]:
    """Fields of an INPUT_OBJECT type (no args, just name+type)."""
    data = _post(
        f'query {{ __type(name: "{type_name}") {{ name kind inputFields '
        f'{{ name type {{ {_DEEP_TYPE_REF} }} }} }} }}',
        api_key,
    )
    t = data["__type"]
    if t is None:
        raise RuntimeError(f"Type '{type_name}' does not exist in this schema.")
    return t.get("inputFields") or []


def _resolve_named_type(type_ref: dict) -> str:
    """A GraphQL type ref can be wrapped in NON_NULL/LIST; unwrap to the name."""
    while type_ref.get("name") is None and type_ref.get("ofType"):
        type_ref = type_ref["ofType"]
    return type_ref.get("name")


def _find_field(fields: list[dict], name: str) -> dict:
    for f in fields:
        if f["name"] == name:
            return f
    available = ", ".join(f["name"] for f in fields)
    raise RuntimeError(f"Field '{name}' not found. Available: {available}")


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

    print("Step 2: 'DEXTradeByTokens' field's 'where' argument type")
    solana_fields = _type_fields(api_key, solana_type_name)
    dex_field = _find_field(solana_fields, "DEXTradeByTokens")
    where_arg = _find_field(dex_field["args"], "where")
    where_type_name = _resolve_named_type(where_arg["type"])
    print(f"  where: {where_type_name}")

    print("Step 3: 'Block' field's type inside the where input")
    where_input_fields = _type_input_fields(api_key, where_type_name)
    block_field = _find_field(where_input_fields, "Block")
    block_type_name = _resolve_named_type(block_field["type"])
    print(f"  Block: {block_type_name}")

    print("Step 4: 'Time' field's type inside Block")
    block_input_fields = _type_input_fields(api_key, block_type_name)
    time_field = _find_field(block_input_fields, "Time")
    time_type_name = _resolve_named_type(time_field["type"])
    print(f"  Time: {time_type_name}")

    print("Step 5: allowed operators on Time -- THIS IS THE ANSWER")
    time_input_fields = _type_input_fields(api_key, time_type_name)
    operator_names = [f["name"] for f in time_input_fields]
    print(f"  Time filter operators: {operator_names}")

    print()
    print("Also checking 'Currency'/'MintAddress' and 'Side' shape under Trade "
          "(the other thing this project has had to guess at):")
    trade_field = _find_field(where_input_fields, "Trade")
    trade_type_name = _resolve_named_type(trade_field["type"])
    trade_input_fields = _type_input_fields(api_key, trade_type_name)
    print(f"  Trade where-fields: {[f['name'] for f in trade_input_fields]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
