"""Is pump.fun/PumpSwap even a possible value on `Trading.Trades`'s
`Pair.Market.Protocol` field, or does this cube only ever see major AMMs?

WHY THIS EXISTS (2026-09-21, see docs/DECISIONS.md D27/D28):
`bitquery_trading_smoketest.py --bisect`, run for real against
`BITQUERY_EXAMPLE_MINT`, found retention reaching back ~30 days (matches
`tet.py`'s ~26-27d finding) -- but EVERY protocol value seen across all
non-empty windows was `amm_v3` / `raydium_amm` / `whirlpool` (Raydium and
Orca). Never `pump`, `pumpfun`, `pumpswap`, or anything resembling it. That
mint has almost certainly already graduated off pump.fun's bonding curve,
so its recent history trading on Raydium/Orca proves nothing either way
about whether this cube covers pump.fun/PumpSwap at all -- it could be
that the cube just never got to see this mint's (older) bonding-curve
phase, or it could be that pump.fun isn't a program this cube indexes.

Rather than going and finding a mint still in its live bonding-curve phase
(a moving target -- whatever's fresh today won't be by the time this
script runs), ask the schema directly: if `Protocol` (or `ProtocolFamily`)
is a GraphQL ENUM, its full set of possible values is right there in the
introspection response, no live trade needed. `raydium_amm`/`whirlpool`
being lowercase-snake-case identifiers (not free-form display names) is
exactly what an enum member's name looks like, so this is a real
possibility, not a long shot.

If pump.fun/PumpSwap DOES appear in the enum: strong evidence this cube
covers it, worth then finding one live-phase mint to confirm with an
actual trade. If `Protocol`/`ProtocolFamily` is NOT an enum (plain
String/OLAP_String), this check can't answer the question either way and
the live-mint test is the only way forward. If it IS an enum and pump.fun
is ABSENT from it: strong evidence this cube cannot serve this project's
actual backfill needs, however deep its retention is elsewhere.

    python -m tape.scripts.bitquery_trading_protocol_check
"""

from __future__ import annotations

import os
import sys

from ..env import load_project_dotenv
from .bitquery_introspect import _find_field, _post, _resolve_named_type, _type_fields


def _enum_values(api_key: str, type_name: str) -> list[str] | None:
    """None if `type_name` isn't an ENUM (or doesn't exist); else its members."""
    data = _post(
        f'query {{ __type(name: "{type_name}") {{ kind enumValues {{ name }} }} }}',
        api_key,
    )
    t = data["__type"]
    if t is None or t.get("kind") != "ENUM":
        return None
    return [v["name"] for v in (t.get("enumValues") or [])]


def main() -> int:
    load_project_dotenv()
    api_key = os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    print("Step 1: resolve the return type of Pair.Market")
    query_type_name = _post("query { __schema { queryType { name } } }", api_key)["__schema"]["queryType"]["name"]
    root_fields = _type_fields(api_key, query_type_name)
    trading_type_name = _resolve_named_type(_find_field(root_fields, "Trading")["type"])
    trading_fields = _type_fields(api_key, trading_type_name)
    trades_return_type = _resolve_named_type(_find_field(trading_fields, "Trades")["type"])
    trade_fields = _type_fields(api_key, trades_return_type)
    pair_type_name = _resolve_named_type(_find_field(trade_fields, "Pair")["type"])
    pair_fields = _type_fields(api_key, pair_type_name)
    market_type_name = _resolve_named_type(_find_field(pair_fields, "Market")["type"])
    market_fields = _type_fields(api_key, market_type_name)
    print(f"  Pair.Market type: {market_type_name}")

    print("\nStep 2: is 'Protocol' an ENUM? If so, list every possible value "
          "and check for pump.fun/pumpswap")
    protocol_type_name = _resolve_named_type(_find_field(market_fields, "Protocol")["type"])
    print(f"  Protocol field type: {protocol_type_name}")
    protocol_values = _enum_values(api_key, protocol_type_name)
    if protocol_values is None:
        print(f"  NOT an enum (or introspection found no enumValues) -- this check "
              f"is inconclusive, the live-mint test is the only way to answer the "
              f"coverage question.")
    else:
        print(f"  Enum values ({len(protocol_values)}): {protocol_values}")
        hits = [v for v in protocol_values if "pump" in v.lower()]
        print(f"  Values containing 'pump': {hits if hits else 'NONE FOUND'}")

    print("\nStep 3: same check for 'ProtocolFamily' (may be a coarser bucket "
          "than Protocol, worth checking independently)")
    family_type_name = _resolve_named_type(_find_field(market_fields, "ProtocolFamily")["type"])
    print(f"  ProtocolFamily field type: {family_type_name}")
    family_values = _enum_values(api_key, family_type_name)
    if family_values is None:
        print(f"  NOT an enum (or introspection found no enumValues) -- inconclusive.")
    else:
        print(f"  Enum values ({len(family_values)}): {family_values}")
        hits = [v for v in family_values if "pump" in v.lower()]
        print(f"  Values containing 'pump': {hits if hits else 'NONE FOUND'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
