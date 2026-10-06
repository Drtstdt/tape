"""D79: a price-plausibility filter for the exploratory scripts under
scripts/ (scenario_backtest.py, token_dna_report.py, diag_price_anomaly.py)
-- NOT wired into tape/bars.py or information_audit.py's build_one().

Why this exists (docs/DECISIONS.md D78): tape/sources/helius.py's own
docstring already documents that it is "cruder than a real decoder: a
non-trade transaction that moves the queried mint for the signer (a plain
transfer, an LP deposit/withdrawal) would be miscounted as a trade. Not
handled here." Real evidence (D78's diag_price_anomaly.py output) found
exactly this: a swap whose base_amount collapses to a few hundred/thousand
tokens -- three-plus orders of magnitude below every neighboring swap in the
same tape -- while quote_amount stays a normal size, producing an implied
price 1,000x-30,000x its neighbors. That is not a bonding-curve move; it is
almost certainly a fee claim, dust transfer, or similar non-trade balance
change getting priced as if it were a trade.

Why NOT in tape/bars.py or information_audit.py: the user explicitly chose
the narrow fix (docs/DECISIONS.md D79) -- filtering here, in the two new
exploratory scripts only, is fast and contained. Reaching into
information_audit.py's build_one() would touch already-run, already-decided
audit machinery (D70/D72) and require re-running and re-litigating those
verdicts, which is a bigger, separate decision the user did not make here.
If this filter proves itself useful, promoting it into the shared pipeline
is a future, deliberate decision -- not a side effect of this one.
"""

from __future__ import annotations

import statistics
from typing import List, Sequence, Tuple

DEFAULT_MAX_RATIO = 50.0
DEFAULT_WINDOW = 5


def filter_implausible_swaps(
    swaps: Sequence, max_ratio: float = DEFAULT_MAX_RATIO, window: int = DEFAULT_WINDOW
) -> Tuple[List, List]:
    """(kept, dropped) -- one token's already-time-sorted swap list, split by
    whether each swap's price is within `max_ratio` of the median price of
    its `window` nearest neighbors (both directions, single pass over the
    ORIGINAL prices -- not recomputed after drops, which would let one drop
    cascade into dropping its own neighbors).

    A real bonding-curve trade, even at its most extreme in the very first
    blocks of a token's life (D69), should not move price by 50x relative to
    swaps immediately around it in the same short tape -- that is a
    deliberately large margin above any plausible real within-block
    variance, chosen so this only catches the kind of multi-order-of-
    magnitude jump D78 found, not genuine (if extreme) price action.

    Never drops when it can't judge: fewer than 3 swaps, a non-positive
    price on either side of the comparison, or no neighbors in range all
    fail closed to "keep" -- this is a blunt outlier screen, not a claim
    about which specific swaps are real trades.
    """
    n = len(swaps)
    if n < 3:
        return list(swaps), []
    prices = [s.price for s in swaps]
    kept: List = []
    dropped: List = []
    for i in range(n):
        lo = max(0, i - window)
        hi = min(n, i + window + 1)
        neighbor_prices = [prices[j] for j in range(lo, hi) if j != i]
        if not neighbor_prices or prices[i] <= 0:
            kept.append(swaps[i])
            continue
        local_median = statistics.median(neighbor_prices)
        if local_median <= 0:
            kept.append(swaps[i])
            continue
        ratio = max(prices[i] / local_median, local_median / prices[i])
        if ratio > max_ratio:
            dropped.append(swaps[i])
        else:
            kept.append(swaps[i])
    return kept, dropped


# The same two program ids as `tape/sources/helius.py`/`tape/sources/bitquery.py`
# (deliberately duplicated here rather than imported -- these are universal
# Solana program ids, not adapter-specific behaviour, and importing a core
# module from one specific source adapter would be a layering inversion this
# project otherwise avoids; see tests/test_cv_and_layering.py).
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def filter_post_migration_swaps(swaps: Sequence) -> Tuple[List, List]:
    """(kept, dropped) -- cuts a mint's already-time-sorted swap list at the
    first swap whose `venue` touches PumpSwap (`PUMPSWAP_PROGRAM`), the venue
    a token migrates to once its pump.fun bonding curve completes. Everything
    from that swap onward is dropped, including a mixed "PUMPFUN+PUMPSWAP"
    venue swap (the literal on-chain migration transaction touches both
    programs atomically) -- the cut is deliberately conservative about where
    bonding-curve data stops being trustworthy, not about exactly which swap
    is "the" migration.

    Why this exists (docs/DECISIONS.md D94/D95): `Store.iter_swaps(mint)` has
    no venue filter, and `BarBuilder` stamps a bar's `venue` once at open and
    never checks it again on later swaps -- a mint's bonding-curve-oriented
    bars/features/policy decision can silently run on post-migration PumpSwap
    trading with nothing downstream aware anything changed. Real evidence
    (D95): of 12 mints in a real 431-mint corpus that showed ANY PumpSwap-
    touching swap, all 12 had ZERO pure pump.fun swaps before it -- i.e. the
    Store's captured history for them starts AT OR AFTER migration, not
    during real bonding-curve trading. 10 of those 12 had already been scored
    by the online policy; 6 were actually ENTERED, and 5 of those 6 lost
    (-32% each) -- a concrete, already-realized cost, not a hypothetical one.

    Deliberately simple: cut, don't try to model the two regimes together. A
    mint with no swaps before the cut returns `([], swaps)` and falls through
    to whatever "too few swaps" gate the caller already has one level up --
    there is no PumpSwap-aware model to hand it to instead, and guessing at
    post-migration behaviour from pre-migration training data would be a
    different, bigger, undecided scope (see `tape/sources/birdeye.py`'s note
    that a past strategy review considered PumpSwap itself the venue worth
    trading post-graduation -- not this pipeline's current scope).
    """
    kept: List = []
    dropped: List = []
    migrated = False
    for s in swaps:
        if not migrated and PUMPSWAP_PROGRAM in s.venue:
            migrated = True
        if migrated:
            dropped.append(s)
        else:
            kept.append(s)
    return kept, dropped
