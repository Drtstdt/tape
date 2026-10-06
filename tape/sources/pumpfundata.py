"""pumpfundata.com adapter -- D93/D98 (docs/DECISIONS.md).

NOT a `SourceAdapter` subclass (see `tape/sources/__init__.py`): that ABC's
`historical(mint, start_ms, end_ms)` assumes a per-mint query API. pumpfundata
sells the opposite shape -- one bulk, market-wide Parquet file per
`(exchange, date, hour)`, covering every mint that traded in that hour. The
one rule that still holds (`tape/sources/__init__.py`'s docstring: "an
adapter may only emit CanonicalSwap") is satisfied by `load_file`/
`iter_raw_dir` below; there is just no single-mint entry point to implement.

Schema verified against the vendor's own real sample file (D93, 500 real
rows, cross-checked against pump.fun's hardcoded on-chain constants -- not
against marketing copy):

    event_type, token_mint, slot_number, can_be_frozen, signature, timestamp,
    token_creator, virtual_token_reserve, virtual_lamports_reserve,
    real_token_reserve, real_lamports_reserve, action, token_amount,
    lamports_amount, fee_lamports, user_wallet, token_total_supply,
    is_mayhem_mode

`event_type` is one of swap/create/bonding_complete. Only `swap` rows become
a `CanonicalSwap` -- `create`/`bonding_complete` don't fit CanonicalSwap's
swap-only shape (D93's own "explicitly not done" note); `create` rows are
still read, only to self-check `token_total_supply` against the known
constant (see `_UNIT_SCALE_NOTE`), never dropped silently if they look wrong.

UNITS -- the one thing most likely to silently corrupt a merge with this
project's existing (Helius-sourced) data if gotten wrong, so it is derived
here, not assumed: D93's real sample showed every `create` row with
`virtual_token_reserve=1,073,000,000,000,000`,
`token_total_supply=1,000,000,000,000,000`. Pump.fun's total supply is
publicly documented as 1 BILLION tokens -- 1,000,000,000,000,000 /
1,000,000,000 = 1,000,000 = 10**6, so pump.fun tokens use 6 decimals, exactly
like `virtual_lamports_reserve=30,000,000,000` is 30 SOL in raw lamports
(1 SOL = 10**9 lamports, a protocol-wide Solana constant, not pump.fun
specific). `tape/sources/helius.py` (this project's primary source) divides
by `10 ** decimals` for tokens and by `1e9` for SOL before anything reaches
`CanonicalSwap` (see its `_token_deltas_all`/`_native_sol_delta`) -- i.e.
`CanonicalSwap.base_amount`/`quote_amount`/`*_reserve_after` are UI-scaled
(human token count, SOL), not raw integers. This adapter MUST apply the same
two divisions or every corroborated/combined row from this source would be
10**6x / 10**9x off from the Helius-sourced rows for the same real trade.

SIDE CONVENTION -- NOT independently verified here. pumpfundata's `action`
column already reads "buy"/"sell", matching `tape.schema.BUY`/`SELL`
literally, so no translation is needed -- but per this project's own stated
rule (`tape/sources/__init__.py`: "verify side convention against
transactions you already have decoded... getting it backwards silently
inverts every flow feature and nothing will raise"), this has NOT yet been
checked against a transaction this project independently knows the true
direction of. `scripts/ingest_pumpfundata.py` does that check automatically
wherever a `signature` here matches one already in the Store from Helius --
real overlapping evidence, not an assumption -- and prints a loud warning if
no overlap was found to check against, or if any disagreement is found.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from ..schema import BUY, SELL, CanonicalSwap

# Duplicated from tape/sources/helius.py / tape/sources/bitquery.py on
# purpose -- same reasoning as tape/sanity.py: a universal Solana program id,
# not adapter-specific behaviour, so no cross-adapter import needed.
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Derived, not guessed -- see module docstring's "UNITS" section.
TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000
EXPECTED_CREATE_TOTAL_SUPPLY_RAW = 1_000_000_000 * (10 ** TOKEN_DECIMALS)

REQUIRED_COLUMNS = {
    "event_type", "token_mint", "slot_number", "signature", "timestamp",
    "action", "token_amount", "lamports_amount", "user_wallet",
}


class SchemaMismatch(RuntimeError):
    """Raised when a real file's columns don't match what D93's sample had.
    Fail loud, not a silent best-effort guess -- this adapter has only ever
    been checked against a 500-row sample, not a real bulk hourly file."""


def _parse_timestamp_ms(value) -> Optional[int]:
    """D93's sample never recorded `timestamp`'s on-disk encoding precisely
    enough to hardcode a format -- handle the plausible cases and let
    `load_file`'s filename self-check (below) catch a wrong guess, rather
    than silently trusting whichever branch happens to run.

    Handles: a pandas/python datetime-like object (has `.timestamp()` or
    `.to_pydatetime()`), or a numeric epoch in seconds/ms/us/ns (disambiguated
    by magnitude -- 2026 is ~1.77e9 in seconds, ~1.77e12 in ms, ~1.77e15 in
    us, ~1.77e18 in ns). Returns None, never a guessed value, if it's none of
    these.
    """
    if value is None:
        return None
    # pandas.Timestamp / datetime.datetime
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if hasattr(value, "timestamp") and not isinstance(value, (int, float)):
        try:
            return int(value.timestamp() * 1000)
        except (ValueError, OSError):
            return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        if v < 1e11:       # seconds (covers ~2001-2255 in this range)
            return int(v * 1000)
        if v < 1e14:       # milliseconds
            return int(v)
        if v < 1e17:       # microseconds
            return int(v / 1000)
        return int(v / 1_000_000)  # nanoseconds
    return None


def _creation_record_from_row(row: dict) -> Optional[Tuple[str, int]]:
    """(mint, ts_ms) for a `create`-type row with a usable mint + timestamp,
    or None. Pulled out of `load_file`'s per-row loop so the extraction
    logic is unit-testable without pandas/pyarrow (D101, docs/DECISIONS.md).

    This is the vendor's own ground-truth launch record -- the pump.fun
    program's `create` instruction fires exactly once per mint, at creation
    -- not a proxy like "first swap OUR OWN discovery pipeline happened to
    see" (see `tape/schema.py` / D54/D55's `created_ts_ms` confound this is
    specifically NOT the same thing as)."""
    mint = row.get("token_mint")
    ts_ms = _parse_timestamp_ms(row.get("timestamp"))
    if not mint or ts_ms is None:
        return None
    return str(mint), ts_ms


def _row_to_swap(row: dict, source: str = "pumpfundata") -> Optional[CanonicalSwap]:
    """One pumpfundata row (already known to have `event_type == "swap"`) ->
    `CanonicalSwap`, or None if a required field is missing/unparsable.
    Mirrors `tape/sources/helius.py`'s "never guess a value" discipline."""
    mint = row.get("token_mint")
    sig = row.get("signature")
    action = row.get("action")
    if not mint or not sig or action not in ("buy", "sell"):
        return None
    ts_ms = _parse_timestamp_ms(row.get("timestamp"))
    if ts_ms is None:
        return None
    slot = row.get("slot_number")
    if slot is None:
        return None
    token_amount_raw = row.get("token_amount")
    lamports_amount_raw = row.get("lamports_amount")
    if token_amount_raw is None or lamports_amount_raw is None:
        return None
    try:
        base_amount = abs(float(token_amount_raw)) / (10 ** TOKEN_DECIMALS)
        quote_amount = abs(float(lamports_amount_raw)) / LAMPORTS_PER_SOL
    except (TypeError, ValueError):
        return None
    price = quote_amount / base_amount if base_amount else 0.0

    def _reserve(field: str) -> Optional[float]:
        v = row.get(field)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    virtual_token_reserve = _reserve("virtual_token_reserve")
    virtual_lamports_reserve = _reserve("virtual_lamports_reserve")
    base_reserve_after = (virtual_token_reserve / (10 ** TOKEN_DECIMALS)
                          if virtual_token_reserve is not None else None)
    quote_reserve_after = (virtual_lamports_reserve / LAMPORTS_PER_SOL
                           if virtual_lamports_reserve is not None else None)

    return CanonicalSwap(
        mint=str(mint),
        venue=PUMPFUN_PROGRAM,
        pool=str(row.get("token_creator") or "unknown"),  # no pool/bonding-curve
                                                           # address column in
                                                           # D93's sample -- the
                                                           # creator address is
                                                           # the closest stable
                                                           # per-mint grouping key
                                                           # available; revisit
                                                           # if a real bulk file
                                                           # turns out to have an
                                                           # actual pool column
                                                           # D93's sample didn't.
        ts_ms=ts_ms,
        slot=int(slot),
        sig=str(sig),
        side=BUY if action == "buy" else SELL,
        base_amount=base_amount,
        quote_amount=quote_amount,
        quote_mint="So11111111111111111111111111111111111111112",
        price=price,
        wallet=row.get("user_wallet") or None,
        base_reserve_after=base_reserve_after,
        quote_reserve_after=quote_reserve_after,
        source=source,
    )


def load_file(path: Path, verify_against_filename: bool = True,
              creation_times: Optional[Dict[str, int]] = None) -> List[CanonicalSwap]:
    """Read one downloaded pumpfundata Parquet file -> list of CanonicalSwap
    (swap rows only; create/bonding_complete rows are read for the self-check
    below, then dropped -- except their timestamp, see `creation_times`).

    `verify_against_filename`: when the path matches the
    `.../date=YYYY-MM-DD/hour=HH.parquet` layout `scripts/pumpfundata_fetch_plan.py`
    downloads into, every derived `ts_ms` is checked to fall within that UTC
    hour. This is the real check on `_parse_timestamp_ms`'s guessed encoding
    -- the filename IS ground truth (it's what was actually requested from
    the vendor), so a mismatch here means the timestamp parsing is wrong, not
    that the data is.

    `creation_times` (D101, docs/DECISIONS.md): if given, a dict this
    function UPDATES IN PLACE with `{mint: ts_ms}` for every `create` row
    seen in this file (keeping the EARLIEST timestamp if a mint somehow
    shows up more than once). This is the vendor's own ground-truth launch
    timestamp -- straight from the pump.fun program's `create` instruction,
    not this project's usual `created_ts_ms` proxy ("first swap OUR OWN
    discovery pipeline happened to see", known since D54/D55 to sometimes be
    REAL LAUNCH + weeks/months, because Bitquery's discovery can resurface an
    old "survivor" token). `scripts/ingest_pumpfundata.py` merges this into
    `data/real_creation_times.json`, the cache `scripts/information_audit.py`
    / `scripts/paper_trade_replay.py` already use to filter those survivors
    out -- previously populated only by `scripts/fetch_real_creation_times.py`'s
    slow, unofficial, ~35%-failure-rate per-mint API calls, which had never
    touched any pumpfundata-sourced mint. Passing `None` (the default) keeps
    `load_file`'s old behaviour exactly -- existing callers are unaffected.

    Raises SchemaMismatch if a required column is missing -- this has only
    been checked against D93's 500-row sample, never a real bulk hourly
    file, so a shape surprise should stop the run, not silently produce
    wrong swaps.
    """
    import pandas as pd

    df = pd.read_parquet(path)
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise SchemaMismatch(
            f"{path}: missing columns {sorted(missing)} -- this adapter was "
            f"written against D93's sample schema; a real file differing "
            f"from it needs the adapter updated, not silently ignored."
        )

    swaps: List[CanonicalSwap] = []
    create_rows_checked = 0
    for row in df.to_dict("records"):
        if row.get("event_type") != "swap":
            if row.get("event_type") == "create":
                tts = row.get("token_total_supply")
                if tts is not None and int(tts) != EXPECTED_CREATE_TOTAL_SUPPLY_RAW:
                    print(f"  WARNING {path}: a create row has "
                          f"token_total_supply={tts}, expected "
                          f"{EXPECTED_CREATE_TOTAL_SUPPLY_RAW} (D93's verified "
                          f"constant) -- TOKEN_DECIMALS=6 may not hold for "
                          f"every mint; do not trust this file's scaling "
                          f"blindly.")
                create_rows_checked += 1
                if creation_times is not None:
                    rec = _creation_record_from_row(row)
                    if rec is not None:
                        mint, ts_ms = rec
                        prev = creation_times.get(mint)
                        if prev is None or ts_ms < prev:
                            creation_times[mint] = ts_ms
            continue
        swap = _row_to_swap(row)
        if swap is not None:
            swaps.append(swap)

    if verify_against_filename:
        expected = _expected_hour_from_path(path)
        if expected is not None:
            note = _check_timestamps_against_hour(swaps, expected, path)
            if note:
                print(note)

    return swaps


# Thresholds for _check_timestamps_against_hour, D98 real-data follow-up: a
# real file (85,472 rows) came back with 177 (0.21%) outside the window, the
# worst by only 6 SECONDS -- not the hours/days a wrong epoch-unit guess
# would produce (ms parsed as seconds, say, is off by a factor of 1000, i.e.
# decades, not seconds). This is boundary noise -- plausibly the vendor
# buckets by block time vs. ingestion time slightly differently right at the
# hour edge -- not a parsing bug, so it must not raise. Only raise when the
# mismatch is big enough (either a large fraction of rows, or any row wildly
# outside the hour) to actually indicate _parse_timestamp_ms guessed the
# wrong unit for this file.
MAX_BOUNDARY_FRACTION = 0.02
MAX_BOUNDARY_SECONDS = 300

# D106 (docs/DECISIONS.md): a real, very-low-volume hour (1134 total swaps --
# a quiet period, far below the usual 76k-150k/hour) hit MAX_BOUNDARY_FRACTION
# (45/1134 = 3.97%) even though its absolute bad-row count (45) is SMALLER
# than D98's already-accepted 177-bad-row case above, and its worst offset
# (121s) stayed nowhere near MAX_BOUNDARY_SECONDS (300s) -- the actual
# signature of a wrong-unit encoding bug is hours/days of drift, not two
# minutes. A fixed percentage threshold is statistically unreliable on a
# small sample: the same underlying RATE of boundary noise produces a much
# larger percentage when the denominator (total swaps that hour) is tiny.
# Require a minimum absolute bad-row count before the fraction threshold can
# raise on its own, so a quiet hour's small denominator can't manufacture a
# false positive -- calibrated with real margin below D98's 177 (clearly
# fine, should keep being a NOTE) and above this file's 45 (now also fine).
# The MAX_BOUNDARY_SECONDS check (which actually distinguishes "boundary
# noise" from "wrong encoding") is UNCHANGED and still fires regardless of
# how few rows are affected.
MIN_BOUNDARY_BAD_COUNT = 100


def _check_timestamps_against_hour(swaps: List[CanonicalSwap], expected: Tuple[int, int],
                                    path) -> Optional[str]:
    """Returns a NOTE string to print if a small, boundary-sized mismatch was
    found (swaps are still kept -- Store dedup on `sig` makes a stray
    neighboring-hour row harmless even if a later file re-reports it, D98).
    Raises SchemaMismatch if the mismatch is too large/frequent to be
    boundary noise -- real evidence of a wrong `_parse_timestamp_ms` guess
    for this file. Returns None if every timestamp was inside the window.

    D106 (docs/DECISIONS.md): the fraction check only fires alongside a
    minimum absolute bad-row count (`MIN_BOUNDARY_BAD_COUNT`) -- a percentage
    computed over a very small total (a quiet, low-volume hour) is noisy on
    its own and can cross `MAX_BOUNDARY_FRACTION` from ordinary boundary
    noise alone. The offset check (`max_delta_s > MAX_BOUNDARY_SECONDS`) is
    unaffected -- it's what actually distinguishes boundary noise from a
    genuinely wrong encoding, regardless of row count.

    D107 (docs/DECISIONS.md): both the NOTE and the raised message now
    include the bad-offset DISTRIBUTION (median/p90/max in seconds), not
    just the single worst value -- a real case (1226/60888, worst 72s) sat
    right at the fraction edge (2.01% vs the 2% cutoff) with a worst offset
    12-36x every other real normal-volume noise sample seen so far (all
    2-6s). A single worst-case number alone couldn't say whether that 72s
    was one rare straggler in an otherwise-ordinary noise cluster, or
    representative of the whole bad set -- this distribution is what settles
    that, here and for whoever reads the raised message next time."""
    lo_ms, hi_ms = expected
    bad = [s for s in swaps if not (lo_ms <= s.ts_ms < hi_ms)]
    if not bad:
        return None
    frac = len(bad) / len(swaps) if swaps else 1.0
    deltas_s = sorted(
        max(lo_ms - s.ts_ms, s.ts_ms - (hi_ms - 1), 0) / 1000.0
        for s in bad
    )
    max_delta_s = deltas_s[-1]
    median_delta_s = deltas_s[len(deltas_s) // 2]
    p90_delta_s = deltas_s[int(len(deltas_s) * 0.9)]
    dist_str = (f"offset distribution: median={median_delta_s:.0f}s "
                f"p90={p90_delta_s:.0f}s max={max_delta_s:.0f}s")
    if (frac > MAX_BOUNDARY_FRACTION and len(bad) >= MIN_BOUNDARY_BAD_COUNT) \
            or max_delta_s > MAX_BOUNDARY_SECONDS:
        raise SchemaMismatch(
            f"{path}: {len(bad)}/{len(swaps)} ({frac:.2%}) parsed "
            f"timestamps fall outside the hour this filename says it is "
            f"({lo_ms}..{hi_ms}), worst by {max_delta_s:.0f}s -- too "
            f"large/too frequent to be hour-boundary noise; "
            f"_parse_timestamp_ms's guessed encoding is almost certainly "
            f"wrong for this file. {dist_str}. First bad: "
            f"ts_ms={bad[0].ts_ms} sig={bad[0].sig}"
        )
    return (f"  NOTE {path}: {len(bad)}/{len(swaps)} ({frac:.2%}) swaps fall "
            f"within {max_delta_s:.0f}s of the hour boundary but outside it "
            f"-- kept (Store dedup on `sig` makes this harmless even if a "
            f"neighboring hour's file re-reports the same trade; see D98). "
            f"{dist_str}.")


def _expected_hour_from_path(path: Path) -> Optional[Tuple[int, int]]:
    """(lo_ms, hi_ms) for a `.../date=YYYY-MM-DD/hour=HH.parquet` path, or
    None if the path doesn't match that layout (e.g. a file moved/renamed --
    skip the self-check rather than guess)."""
    import re
    from datetime import datetime, timezone

    m_date = re.search(r"date=(\d{4}-\d{2}-\d{2})", str(path))
    m_hour = re.search(r"hour=(\d{2})", str(path))
    if not m_date or not m_hour:
        return None
    y, mo, d = (int(x) for x in m_date.group(1).split("-"))
    h = int(m_hour.group(1))
    lo = datetime(y, mo, d, h, tzinfo=timezone.utc)
    lo_ms = int(lo.timestamp() * 1000)
    return lo_ms, lo_ms + 3_600_000


def iter_raw_dir(raw_dir: Path, exchange: str = "pump_fun") -> Iterator[Tuple[Path, List[CanonicalSwap]]]:
    """Yield (path, swaps) for every `date=*/hour=*.parquet` file under
    `raw_dir/<exchange>/`, in filename-sorted (= chronological) order. A file
    that fails `load_file` (SchemaMismatch) is NOT caught here -- let it
    raise, and let the caller (scripts/ingest_pumpfundata.py) decide whether
    to stop the whole run or skip just that file; silently swallowing a
    schema surprise is exactly the failure mode this module exists to avoid.
    """
    base = Path(raw_dir) / exchange
    for date_dir in sorted(base.glob("date=*")):
        for file_path in sorted(date_dir.glob("hour=*.parquet")):
            yield file_path, load_file(file_path)
