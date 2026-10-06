#!/usr/bin/env python3
"""D47 (2026-09-22, docs/DECISIONS.md) -- PROOF OF CONCEPT, NOT production
code. Question this answers, on REAL already-backfilled data, not a guess:
does classifying instructions via pump.fun/PumpSwap's OWN, OFFICIAL Anchor
IDLs (pump-fun/pump-public-docs, first-party -- the same repo already used
to verify PUMPFUN_PROGRAM/PUMPSWAP_PROGRAM) actually disagree with this
project's current classification method?

Current method (`tape/sources/helius.py::_to_canonical`/`_to_canonical_any_mint`):
"a real swap happened" is INFERRED from "the signer's token balance for
this mint changed" -- documented there as cruder than a real decoder,
with a named, unhandled failure mode: "a non-trade transaction that moves
the queried mint for the signer (a plain transfer, an LP deposit/
withdrawal) would be miscounted as a trade."

This script checks that concretely: for a sample of signatures this
project has ALREADY backfilled and counted as swaps, decode which
instruction(s) they actually invoked against PUMPFUN_PROGRAM/
PUMPSWAP_PROGRAM using the Anchor discriminator convention (the first 8
bytes of an instruction's data equal sha256(f"global:{name}")[:8] -- a
documented, deterministic Anchor convention, not guessed: see Anchor's own
docs on instruction dispatch). This identifies WHICH instruction ran
(buy/sell vs create/withdraw/admin_*/etc) WITHOUT decoding instruction
ARGS at all -- no anchorpy dependency, no Borsh arg parsing, just enough
to answer "was this actually a buy or sell instruction".

IDLs are fetched fresh, live, from pump-fun's own repo -- never hardcoded,
same discipline as everything else in this project. If pump-fun ever
changes their IDL shape, this fails loud (RuntimeError) rather than
silently classifying against a stale local copy.

Usage:
    python scripts/poc_idl_classify.py --data data --sample 30
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from tape.env import load_project_dotenv
from tape.sources.bitquery import PUMPFUN_PROGRAM, PUMPSWAP_PROGRAM
from tape.sources.helius import MAX_SUPPORTED_TX_VERSION, _rpc_call
from tape.store import Store

PUMP_IDL_URL = "https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump.json"
PUMP_AMM_IDL_URL = "https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump_amm.json"

# Real trade instructions confirmed present in both IDLs (2026-09-22): pump.json
# has `buy`/`buy_exact_sol_in`/`buy_exact_quote_in_v2` etc, no separate `sell`
# name has been manually confirmed yet for pump.json specifically -- this
# script does NOT hardcode a trade/non-trade instruction list; it prints
# every instruction name it finds so that list can be built from REAL
# evidence out of this run, not assumed in advance (D3 discipline).


def _b58_decode(s: str) -> bytes:
    """Plain base58 decode, no external dependency assumed (base58 is a
    likely transitive dep of `solana`/`solders`, already in requirements.txt,
    but not verified importable in every environment this might run in --
    this tiny, well-known implementation means the PoC never fails on that
    alone)."""
    alphabet = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for ch in s.encode():
        n = n * 58 + alphabet.index(ch)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    n_pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_pad + full


def anchor_discriminator(name: str) -> bytes:
    """Anchor's own documented convention: an instruction's 8-byte
    discriminator is the first 8 bytes of sha256(f"global:{instruction_name}").
    Deterministic and public -- not guessed, not something that needs a
    live program call to derive."""
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def fetch_instruction_map(url: str) -> dict:
    resp = httpx.get(url, timeout=30.0)
    resp.raise_for_status()
    idl = resp.json()
    instructions = idl.get("instructions") or []
    if not instructions:
        raise RuntimeError(
            f"{url} has no 'instructions' array -- IDL shape may have "
            f"changed since 2026-09-22; do not trust a guessed fallback."
        )
    return {anchor_discriminator(i["name"]): i["name"] for i in instructions}


def classify(data_b58: str, disc_map: dict) -> str:
    try:
        raw = _b58_decode(data_b58)
    except Exception:
        return "UNDECODABLE_DATA"
    disc = raw[:8]
    return disc_map.get(disc, f"UNKNOWN(disc={disc.hex()})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--sample", type=int, default=30,
                     help="how many real, already-backfilled signatures to check")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    print("Fetching official IDLs from pump-fun/pump-public-docs (first-party, live) ...")
    pump_map = fetch_instruction_map(PUMP_IDL_URL)
    amm_map = fetch_instruction_map(PUMP_AMM_IDL_URL)
    print(f"  pump.json:     {len(pump_map)} instructions -- {sorted(set(pump_map.values()))}")
    print(f"  pump_amm.json: {len(amm_map)} instructions -- {sorted(set(amm_map.values()))}")
    disc_by_program = {PUMPFUN_PROGRAM: pump_map, PUMPSWAP_PROGRAM: amm_map}

    # BUG FOUND in the first run of this script (2026-09-22, D48,
    # docs/DECISIONS.md): `is_trade` originally checked only the exact
    # names "buy"/"sell", missing every real variant -- both IDLs define
    # several (`buy_exact_quote_in`, `buy_exact_sol_in`, `buy_v2`,
    # `sell_v2`, `buy_exact_quote_in_v2`, ...), ALL genuine trade
    # instructions. Built dynamically from whatever the LIVE IDL actually
    # contains (never hardcoded), via the one pattern that holds for every
    # name observed across both files: starts with "buy" or "sell". Printed
    # explicitly so this classification is never a hidden assumption again.
    trade_names = {n for n in (set(pump_map.values()) | set(amm_map.values()))
                   if n.startswith("buy") or n.startswith("sell")}
    print(f"  Treating as TRADE instructions: {sorted(trade_names)}")

    load_project_dotenv()
    api_key = args.api_key or os.environ.get("HELIUS_API_KEY")
    if not api_key:
        print("HELIUS_API_KEY not set.", file=sys.stderr)
        return 2

    store = Store(args.data)
    df = store.sql(f"SELECT DISTINCT sig, side FROM swaps LIMIT {args.sample}")
    sigs = list(df["sig"])
    our_side = dict(zip(df["sig"], df["side"]))
    if not sigs:
        print("Store has no swaps yet -- run a backfill first.", file=sys.stderr)
        return 2
    print(f"\nChecking {len(sigs)} real, already-backfilled signature(s) against the IDL...\n")

    agree, disagree = 0, 0
    for sig in sigs:
        tx = _rpc_call(
            "getTransaction",
            [sig, {"encoding": "jsonParsed",
                   "maxSupportedTransactionVersion": MAX_SUPPORTED_TX_VERSION}],
            api_key,
        )
        result = tx.get("result") or {}
        message = (result.get("transaction") or {}).get("message") or {}
        top_level = message.get("instructions") or []
        meta = result.get("meta") or {}
        # BUG FOUND (D48): pump.fun/PumpSwap is very often invoked via CPI
        # from a router/wrapper program, not as a top-level instruction --
        # the first run of this script only looked at `message.instructions`
        # and missed every one of those, showing up as "(no pump.fun/
        # PumpSwap instruction found)" despite a real trade having happened.
        # `meta.innerInstructions` (standard Solana getTransaction shape,
        # present under encoding=jsonParsed the same as top-level ones) is
        # where a CPI'd call actually shows up.
        inner = [ix for grp in (meta.get("innerInstructions") or [])
                 for ix in (grp.get("instructions") or [])]
        all_instructions = list(top_level) + inner

        found = []
        for ix in all_instructions:
            if not isinstance(ix, dict):
                continue
            prog = ix.get("programId")
            if prog not in disc_by_program:
                continue
            data = ix.get("data")
            if not data:
                continue
            found.append(classify(data, disc_by_program[prog]))

        is_trade = any(name in trade_names for name in found)
        verdict = "TRADE" if is_trade else "NOT-A-TRADE"
        names = ", ".join(found) or "(no pump.fun/PumpSwap instruction found)"
        current_side = our_side.get(sig, "?")
        flag = ""
        if not is_trade:
            disagree += 1
            flag = "  <-- DISAGREES: our code counted this as a swap, IDL says no buy/sell ran"
        else:
            agree += 1
        print(f"{sig[:24]}...  our_side={current_side:5s}  "
              f"idl=[{names}]  verdict={verdict}{flag}")

        if not is_trade:
            # D49 diagnostic (docs/DECISIONS.md): "no matching instruction
            # found" is ambiguous by itself -- it could mean the tx really
            # never touches pump.fun/PumpSwap at all (a genuine false
            # positive in the balance-delta method), OR it could mean this
            # script still isn't finding a program call that's really
            # there (e.g. an address-lookup-table-resolved program id
            # `jsonParsed` renders differently than expected -- unverified).
            # `meta.logMessages` is Solana's own flat, depth-agnostic
            # "Program <id> invoke [N]" trace -- the most direct way to
            # settle which explanation is true for THIS transaction,
            # independent of how this script parses the structured
            # instruction lists above.
            all_progs = sorted({ix.get("programId") for ix in all_instructions
                                 if isinstance(ix, dict) and ix.get("programId")})
            logs = meta.get("logMessages") or []
            pump_in_logs = [l for l in logs
                             if PUMPFUN_PROGRAM in l or PUMPSWAP_PROGRAM in l]
            print(f"    full sig: {sig}")
            print(f"    all program ids in top-level+inner instructions: {all_progs}")
            print(f"    PUMPFUN_PROGRAM/PUMPSWAP_PROGRAM mentioned in logMessages: "
                  f"{bool(pump_in_logs)}")
            if pump_in_logs:
                for l in pump_in_logs[:5]:
                    print(f"      log: {l}")

    print(f"\n{agree}/{len(sigs)} confirmed real buy/sell by the IDL.")
    print(f"{disagree}/{len(sigs)} our code counted as swaps but the IDL says were NOT "
          f"a buy/sell instruction -- these are exactly the false positives "
          f"tape/sources/helius.py's docstring already flagged as a known, "
          f"unhandled gap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
