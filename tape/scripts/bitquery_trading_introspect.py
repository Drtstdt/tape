"""Schema introspection for Bitquery's `Trading.Trades` cube -- a DIFFERENT
cube from `Solana.DEXTradeByTokens`, which is what D19-D24 tested and what
`tape/sources/bitquery.py` is built against.

WHY THIS EXISTS (2026-09-21, see docs/DECISIONS.md D26): the user hand-wrote
a query against `Trading.Trades` (project root `tet.py`, not part of this
package) filtered to `Pair.Market.Network.is: "Solana"` and a 25-27-days-ago
relative window, and got back real rows all dated within that window --
first real evidence Bitquery has SOME cube reaching much further back than
`DEXTradeByTokens`'s confirmed ~9-12h `realtime` retention (D21). That is a
genuinely different, promising finding, not a repeat of D21 -- but two
things are unverified before it changes anything:

  1. Whether `Trading.Trades` can be filtered to a SPECIFIC MINT (not just
     "Solana network, quote=SOL generally") -- `tet.py`'s query requests
     `Pair.Currency.Symbol` but its CSV-writing code reads
     `Pair.BaseCurrency`, a client-side bug (wrong field name, not a schema
     fact) that left every BaseToken/BaseAddress/Trader/TxHash column
     blank. The actual field names for the base currency's mint address,
     the transaction signature, the trader wallet, and the DEX/protocol
     name are still unknown -- guessing them repeats the exact D20 mistake
     (guessed `DEXTrades`/Buy-Sell field names from stale docs, got a
     silent zero-rows result) with a new cube instead of a new query type.
  2. Whether this cube covers pump.fun/PumpSwap specifically, or only
     larger/more liquid Solana DEXes -- the sample rows are all SOL/generic
     pairs with no venue visible yet.

This script only answers (1) -- the field names -- via live introspection,
the same technique that found D20's fix. It does NOT run a real trades
query and does NOT touch retention/coverage; that is
`bitquery_trading_smoketest.py`'s job, written next, once these field names
are confirmed real.

Reuses `bitquery_introspect.py`'s walking helpers rather than duplicating
them -- they are generic GraphQL introspection utilities, not specific to
`DEXTradeByTokens`.

    python -m tape.scripts.bitquery_trading_introspect
"""

from __future__ import annotations

import os
import sys

from ..env import load_project_dotenv
from .bitquery_introspect import (
    _find_field, _post, _resolve_named_type, _type_fields, _type_input_fields,
)


def main() -> int:
    load_project_dotenv()
    api_key = os.environ.get("BITQUERY_API_KEY")
    if not api_key:
        print("BITQUERY_API_KEY not set.", file=sys.stderr)
        return 2

    print("Step 1: root Query type -> 'Trading' field's type")
    query_type_name = _post("query { __schema { queryType { name } } }", api_key)["__schema"]["queryType"]["name"]
    root_fields = _type_fields(api_key, query_type_name)
    trading_field = _find_field(root_fields, "Trading")
    trading_type_name = _resolve_named_type(trading_field["type"])
    print(f"  Trading field type: {trading_type_name}")

    print("\nStep 2: 'Trades' field -- its 'where' argument type AND its return type")
    trading_fields = _type_fields(api_key, trading_type_name)
    trades_field = _find_field(trading_fields, "Trades")
    where_arg = _find_field(trades_field["args"], "where")
    where_type_name = _resolve_named_type(where_arg["type"])
    return_type_name = _resolve_named_type(trades_field["type"])
    print(f"  where: {where_type_name}")
    print(f"  returns: {return_type_name}")

    print(f"\nStep 3: filterable fields directly on '{where_type_name}' (top-level where-clause)")
    where_input_fields = _type_input_fields(api_key, where_type_name)
    print(f"  {[f['name'] for f in where_input_fields]}")

    print("\nStep 4: 'Pair' filter fields -- where the mint-address filter should live")
    pair_where_field = _find_field(where_input_fields, "Pair")
    pair_where_type_name = _resolve_named_type(pair_where_field["type"])
    pair_where_fields = _type_input_fields(api_key, pair_where_type_name)
    print(f"  Pair where-fields: {[f['name'] for f in pair_where_fields]}")

    # Step 3 found 'Currency'/'QuoteCurrency' with only Id/Symbol/Name -- no
    # obvious MintAddress field -- but ALSO 'Token'/'QuoteToken'/'Pool',
    # which weren't checked yet and are just as likely to hold the actual
    # on-chain address for a cross-chain-unified cube like this one.
    for sub_name in ("Currency", "BaseCurrency", "QuoteCurrency", "Token", "QuoteToken", "Pool"):
        try:
            sub_field = _find_field(pair_where_fields, sub_name)
        except RuntimeError:
            continue
        sub_type_name = _resolve_named_type(sub_field["type"])
        sub_fields = _type_input_fields(api_key, sub_type_name)
        print(f"  Pair.{sub_name} where-fields (looking for a MintAddress-style filter): "
              f"{[f['name'] for f in sub_fields]}")
        # 'Id' on Currency (or an Address-like field on Token/Pool) is
        # itself probably a comparator wrapper (e.g. {is: "..."} ), the
        # same pattern DEXTradeByTokens uses for MintAddress -- check its
        # own shape rather than assume it's a plain scalar.
        for leaf_name in ("Id", "Address", "SmartContract"):
            try:
                leaf_field = _find_field(sub_fields, leaf_name)
            except RuntimeError:
                continue
            leaf_type_name = _resolve_named_type(leaf_field["type"])
            leaf_fields = _type_input_fields(api_key, leaf_type_name)
            print(f"    Pair.{sub_name}.{leaf_name} -> {leaf_type_name}: "
                  f"{[f['name'] for f in leaf_fields]}")

    print("\nStep 5: fields actually returned by a 'Trades' row (THIS is what "
          "tet.py's query should be requesting, and what the CSV writer's field "
          "names need to match)")
    return_fields = _type_fields(api_key, return_type_name)
    print(f"  Top-level: {[f['name'] for f in return_fields]}")

    print("\nStep 6: 'Pair' return-type fields, then its currency sub-fields "
          "(looking for Symbol AND a mint-address-equivalent field, plus which "
          "side is base vs quote)")
    pair_return_field = _find_field(return_fields, "Pair")
    pair_return_type_name = _resolve_named_type(pair_return_field["type"])
    pair_return_fields = _type_fields(api_key, pair_return_type_name)
    print(f"  Pair return-fields: {[f['name'] for f in pair_return_fields]}")
    # 'Market' was found in Step 4's Pair where-fields (it's what
    # `Pair.Market.Network.is: "Solana"` already filters on in tet.py) but
    # never drilled into -- it's the most likely place for a DEX/protocol
    # name to live, alongside the currency/token sub-types already checked.
    for sub_name in ("Currency", "BaseCurrency", "QuoteCurrency", "Token", "QuoteToken", "Pool", "Market"):
        try:
            sub_field = _find_field(pair_return_fields, sub_name)
        except RuntimeError:
            continue
        sub_type_name = _resolve_named_type(sub_field["type"])
        sub_fields = _type_fields(api_key, sub_type_name)
        print(f"  Pair.{sub_name} return-fields: {[f['name'] for f in sub_fields]}")

    print("\nStep 7: looking for a transaction-signature field and a "
          "trader/wallet field among the top-level return fields above -- "
          "check any that look like 'Transaction', 'TransactionHeader', "
          "'Trader', 'Account', 'Signature', 'Dex', 'Exchange', 'Protocol' "
          "by introspecting each")
    for candidate in ("Transaction", "TransactionHeader", "Trader", "Account", "Dex", "Exchange", "Protocol"):
        try:
            field = _find_field(return_fields, candidate)
        except RuntimeError:
            continue
        type_name = _resolve_named_type(field["type"])
        sub_fields = _type_fields(api_key, type_name)
        print(f"  {candidate} -> {type_name}: {[f['name'] for f in sub_fields]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
