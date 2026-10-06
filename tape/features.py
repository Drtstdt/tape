"""Incremental per-token state. The live engine and the replay engine build it
the same way, from the same bars, because it is the same class.

The no-lookahead guarantee is STRUCTURAL, not a convention: `update()` receives
one bar and is never handed a list, so there is nothing future in scope to read
by accident. `tests/test_no_lookahead.py` truncates a tape and asserts every
surviving feature row is bit-identical.

A feature that cannot be computed at 3am on a token forty seconds old does not
belong here, no matter how predictive it looks offline.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional

from .schema import Bar


def _safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


# D82 (docs/DECISIONS.md): scripts/token_dna_report.py's descriptive-only
# `price_after_Ns` columns (D77) showed a Spearman rho of +0.47 to +0.52 vs.
# `max_move_30m` on real data (D79's price-plausibility fix applied first) --
# promising enough that the user's explicit choice was to add this as a real,
# causally-computed FEATURE here and let information_audit.py's actual Stage
# 1 gate (permutation null, chronological split, held-out CI) judge it,
# rather than trusting the raw correlation the way D71 already did for v3's
# flow-reversal idea. Same offsets as token_dna_report.py's EARLY_OFFSETS_S
# so the two remain directly comparable.
PRICE_AFTER_OFFSETS_S: tuple = (1, 2, 5, 10, 30)


@dataclass
class Reputation:
    """As-of-time reputation lookups, injected rather than computed here.

    STRICT as-of-time semantics: a creator's stats on day T may only use
    creations strictly before T. This is the easiest place in the whole project
    to leak the future, and the leak looks like a fantastic feature -- a
    creator's rug rate computed over the full corpus "predicts" rugs perfectly.
    `tests/test_asof.py` enforces it.
    """

    creator_launches: Optional[int] = None
    creator_graduation_rate: Optional[float] = None
    creator_rug_rate: Optional[float] = None
    creator_median_peak_multiple: Optional[float] = None
    creator_launches_last_hour: Optional[int] = None
    early_buyer_quality: Optional[float] = None     # share of first-N buyers with good history
    early_buyer_flipper_share: Optional[float] = None
    cluster_size: Optional[int] = None


@dataclass
class Regime:
    """Chain-wide aggregates, joined on time, one row per minute.

    A model fitted across regimes with no regime feature looks stable in
    backtest and swings in the fog live. v1 saw PSI 0.80 within 48 hours, and a
    large part of that is an unmodelled regime shift.
    """

    launchpad_creation_rate: Optional[float] = None
    graduation_rate: Optional[float] = None
    aggregate_dex_volume: Optional[float] = None
    sol_price_usd: Optional[float] = None
    sol_return_24h: Optional[float] = None
    median_new_pool_liquidity: Optional[float] = None


class TokenState:
    """Append-only, single-pass, O(1) per bar."""

    MAXLEN = 256

    def __init__(self, mint: str, *, created_ts_ms: Optional[int] = None,
                 reputation: Optional[Reputation] = None,
                 total_supply: Optional[float] = None) -> None:
        self.mint = mint
        self.created_ts_ms = created_ts_ms
        self.reputation = reputation or Reputation()
        self.total_supply = total_supply
        self.regime = Regime()

        self.n_bars = 0
        self.first_ts: Optional[int] = None
        self.last_ts: Optional[int] = None

        self._close = deque(maxlen=self.MAXLEN)
        self._ret = deque(maxlen=self.MAXLEN)
        self._netflow = deque(maxlen=self.MAXLEN)
        self._buyq = deque(maxlen=self.MAXLEN)
        self._sellq = deque(maxlen=self.MAXLEN)
        self._liq = deque(maxlen=self.MAXLEN)
        self._nbuyers = deque(maxlen=self.MAXLEN)
        self._nsellers = deque(maxlen=self.MAXLEN)
        self._largest = deque(maxlen=self.MAXLEN)
        self._dur = deque(maxlen=self.MAXLEN)

        self.peak_price = 0.0
        self.peak_liq = 0.0
        self.cum_creator_sold = 0.0
        self.cum_insider_sold = 0.0
        self._wallets_seen: Dict[str, int] = {}

        # Wilder RSI, incremental.
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._rsi_period = 14

        # Liquidity/price divergence sustain counter (Category A input).
        self.liq_div_consecutive = 0

        # D82: price_after_Ns state. entry_price is the FIRST bar's open --
        # the closest bar-level analog to token_dna_report.py's `swaps[0].price`
        # (a dollar-volume bar's open is literally the first trade in it).
        # `_price_after[o]` is the running "last bar close known to be at or
        # before created_ts_ms + o seconds"; once a bar arrives whose close is
        # AFTER that threshold, `_price_after_final[o]` latches True and the
        # value never changes again -- see `_update_price_after()`.
        self.entry_price: Optional[float] = None
        self._price_after: Dict[int, Optional[float]] = {o: None for o in PRICE_AFTER_OFFSETS_S}
        self._price_after_final: Dict[int, bool] = {o: False for o in PRICE_AFTER_OFFSETS_S}

    # -- ingestion ------------------------------------------------------

    def update(self, bar: Bar) -> None:
        if self.first_ts is None:
            self.first_ts = bar.open_ts_ms
            self.entry_price = bar.open
        self.last_ts = bar.close_ts_ms
        self.n_bars += 1

        prev_close = self._close[-1] if self._close else None
        prev_liq = self._liq[-1] if self._liq else None

        self._update_price_after(bar)

        self._close.append(bar.close)
        self._ret.append(bar.ret)
        self._netflow.append(bar.net_flow)
        self._buyq.append(bar.buy_quote)
        self._sellq.append(bar.sell_quote)
        self._nbuyers.append(bar.unique_buyers)
        self._nsellers.append(bar.unique_sellers)
        self._largest.append(bar.largest_buyer_share)
        self._dur.append(bar.duration_ms)
        if bar.quote_reserve_close is not None:
            self._liq.append(bar.quote_reserve_close)
            self.peak_liq = max(self.peak_liq, bar.quote_reserve_close)

        self.peak_price = max(self.peak_price, bar.high)
        self.cum_creator_sold += bar.creator_sold_quote
        self.cum_insider_sold += bar.insider_sold_quote

        self._update_rsi(prev_close, bar.close)
        self._update_liq_divergence(prev_close, bar.close, prev_liq, bar.quote_reserve_close)

    def _update_rsi(self, prev_close: Optional[float], close: float) -> None:
        if prev_close is None:
            return
        change = close - prev_close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self._avg_gain is None:
            self._avg_gain, self._avg_loss = gain, loss
            return
        n = self._rsi_period
        self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
        self._avg_loss = (self._avg_loss * (n - 1) + loss) / n

    def _update_price_after(self, bar: Bar) -> None:
        """D82: causal, bar-level analog of token_dna_report.py's swap-level
        "last observed price at or before offset X seconds since entry".

        No raw per-swap timestamps are available here (TokenState only ever
        sees closed Bars, by design -- see this module's docstring), so this
        is a deliberate approximation at bar granularity:

          * while a bar closes AT OR BEFORE the threshold, its close price is
            the best available "last known price at/before the offset" and
            keeps getting overwritten by each subsequent such bar;
          * the first bar whose close lands AFTER the threshold triggers
            finalization: if that same bar's OPEN is still at/before the
            threshold (the offset falls inside this bar), its open is used --
            an open price is a real trade that is known to have happened at
            open_ts_ms, which is <= the threshold, so it is a valid (if
            slightly stale) "at or before" observation; otherwise the value
            already recorded from a fully-earlier bar is kept as-is;
          * once finalized, the value is frozen for good -- exactly like
            every other "None until measurable, then fixed" feature in this
            module (age_ms, rsi, ...), never silently recomputed later.

        Returns None (via `_price_after_return`) until real wall-clock time
        has actually passed the offset -- there is no way to answer "what was
        the price after N seconds" before N seconds have elapsed, live or in
        replay, so guessing early would be exactly the kind of lookahead this
        module exists to prevent.
        """
        if self.created_ts_ms is None:
            return
        for o in PRICE_AFTER_OFFSETS_S:
            if self._price_after_final[o]:
                continue
            threshold = self.created_ts_ms + o * 1000
            if bar.close_ts_ms <= threshold:
                self._price_after[o] = bar.close
            else:
                if bar.open_ts_ms <= threshold:
                    self._price_after[o] = bar.open
                self._price_after_final[o] = True

    def _price_after_return(self, o: int) -> Optional[float]:
        if not self._price_after_final.get(o, False):
            return None
        p = self._price_after.get(o)
        if p is None or not self.entry_price:
            return None
        return p / self.entry_price - 1.0

    def _update_liq_divergence(self, prev_close, close, prev_liq, liq,
                               price_tol: float = -0.02, liq_tol: float = -0.05) -> None:
        """Price flat or rising while liquidity falls, sustained.

        Someone removing the floor while supporting the tape. Nothing
        legitimate looks like this, which is why it is a hard exit rather than
        a score input.
        """
        if prev_close is None or prev_liq is None or liq is None or prev_liq <= 0:
            return
        d_price = close / prev_close - 1.0 if prev_close > 0 else 0.0
        d_liq = liq / prev_liq - 1.0
        if d_price >= price_tol and d_liq <= liq_tol:
            self.liq_div_consecutive += 1
        else:
            self.liq_div_consecutive = 0

    # -- helpers --------------------------------------------------------

    def _tail(self, dq, k: int):
        if not dq:
            return []
        return list(dq)[-k:]

    def _sum(self, dq, k: int) -> Optional[float]:
        vals = [v for v in self._tail(dq, k) if v is not None]
        return sum(vals) if vals else None

    def _netflow_reversal(self, k: int) -> Optional[float]:
        """D71 (docs/DECISIONS.md): TradingRPC/v3's `STRATEGY.md` core exit
        thesis ("flowReversal": leave when accumulation reverses, never
        merely because price fell) as a genuine, testable FEATURE rather
        than v3's fixed-threshold, never-actually-run trading rule --
        recent net flow minus the flow immediately preceding it. Negative
        means flow is decelerating/reversing from accumulation toward
        distribution; positive means it is accelerating. Still causal
        (only ever reads bars already in the deque, same as every other
        feature here) and still returns None rather than a guess when
        there isn't yet a full PRIOR k-bar window to compare against --
        NOT the same as `netflow_k`, which is a level, not a change."""
        recent = self._sum(self._netflow, k)
        if recent is None:
            return None
        all_vals = list(self._netflow)
        if len(all_vals) < 2 * k:
            return None
        prior_vals = [v for v in all_vals[-2 * k:-k] if v is not None]
        if not prior_vals:
            return None
        return recent - sum(prior_vals)

    @property
    def age_ms(self) -> Optional[int]:
        if self.created_ts_ms is None or self.last_ts is None:
            return None
        return self.last_ts - self.created_ts_ms

    @property
    def price(self) -> Optional[float]:
        return self._close[-1] if self._close else None

    @property
    def liquidity(self) -> Optional[float]:
        return self._liq[-1] if self._liq else None

    @property
    def rsi(self) -> Optional[float]:
        # RSI is a VETO and a divergence source, never a trigger. On the v3
        # reference token it read 15-28 through an entire -98.4% collapse and
        # never crossed 30, so `RSI < 30 -> buy` buys all the way down.
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self.n_bars < self._rsi_period:
            return None
        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    # -- the feature row ------------------------------------------------

    def features(self) -> Dict[str, Optional[float]]:
        """One flat dict. Column names are the contract between research and
        live -- the report ranks these names and the bot reads these names."""
        f: Dict[str, Optional[float]] = {}
        liq = self.liquidity
        price = self.price

        f["n_bars"] = float(self.n_bars)
        f["age_ms"] = float(self.age_ms) if self.age_ms is not None else None
        f["price"] = price
        f["liquidity"] = liq

        # D82: early-price-trajectory features (see _update_price_after's
        # docstring). Named to match token_dna_report.py's columns exactly
        # ("Ns" suffix, never bare "_3"/"_10"/"_30") so information_audit.py's
        # `_WINDOW_SUFFIX` regex does NOT collapse these into the netflow_{k}
        # -style families -- each is its own singleton family, which is
        # correct: they are not instances of the same window-length loop.
        for o in PRICE_AFTER_OFFSETS_S:
            f[f"price_after_{o}s"] = self._price_after_return(o)

        for k in (3, 10, 30):
            nf = self._sum(self._netflow, k)
            bq = self._sum(self._buyq, k)
            sq = self._sum(self._sellq, k)
            f[f"netflow_{k}"] = nf
            f[f"netflow_{k}_over_liq"] = _safe_div(nf, liq)
            f[f"buysell_ratio_{k}"] = _safe_div(bq, sq) if sq else None
            rets = [r for r in self._tail(self._ret, k) if r is not None]
            f[f"ret_{k}"] = (math.prod(1 + r for r in rets) - 1.0) if rets else None
            f[f"vol_{k}"] = _stdev(rets)
            # D71: v3's flowReversal exit thesis, as a feature (see
            # _netflow_reversal's own docstring for why this differs from
            # netflow_{k} above -- a change, not a level).
            f[f"netflow_reversal_{k}"] = self._netflow_reversal(k)

        # Breadth. Null when unmeasured -- see bars._close().
        buyers = [b for b in self._tail(self._nbuyers, 10) if b is not None]
        f["unique_buyers_10"] = float(sum(buyers)) if buyers else None
        sellers = [s for s in self._tail(self._nsellers, 10) if s is not None]
        f["unique_sellers_10"] = float(sum(sellers)) if sellers else None
        # D71: v3's other accumulation-gate condition ("unique buyers >
        # unique sellers", STRATEGY.md section 4) as a continuous,
        # normalized feature in [-1, 1] rather than a fixed boolean gate --
        # positive means buyer breadth dominates, negative means seller
        # breadth dominates. None (not 0) when either side is unmeasured,
        # same fail-closed-on-missing-data discipline as everything else
        # here -- a missing seller count must never read as "zero sellers".
        ub, us = f["unique_buyers_10"], f["unique_sellers_10"]
        f["buyer_seller_breadth_10"] = (
            None if (ub is None or us is None or (ub + us) == 0)
            else (ub - us) / (ub + us)
        )
        largest = [l for l in self._tail(self._largest, 10) if l is not None]
        f["largest_buyer_share_10"] = (sum(largest) / len(largest)) if largest else None

        # Liquidity path.
        f["liq_slope_10"] = _slope(self._tail(self._liq, 10))
        f["liq_drawdown"] = (liq / self.peak_liq - 1.0) if (liq and self.peak_liq > 0) else None
        f["price_drawdown"] = (price / self.peak_price - 1.0) if (price and self.peak_price > 0) else None

        f["rsi"] = self.rsi
        f["liq_div_consecutive"] = float(self.liq_div_consecutive)
        f["creator_sold_over_liq"] = _safe_div(self.cum_creator_sold, liq)
        f["insider_sold_over_liq"] = _safe_div(self.cum_insider_sold, liq)

        # Flow/price divergence: the strong one, because it substitutes a
        # MEASURED quantity for an inferred one. Equities infer order flow from
        # price because they cannot see it; you can see it.
        r10 = f.get("ret_10")
        nf10 = f.get("netflow_10_over_liq")
        f["flow_price_divergence"] = (
            None if (r10 is None or nf10 is None) else _sign(r10) - _sign(nf10)
        )

        if self.total_supply and self._liq and self._close and price:
            base_reserve = _safe_div(liq, price)
            f["ceiling_ratio_R"] = _safe_div(self.total_supply, base_reserve)
        else:
            f["ceiling_ratio_R"] = None

        rep = self.reputation
        f["creator_launches"] = _as_f(rep.creator_launches)
        f["creator_graduation_rate"] = rep.creator_graduation_rate
        f["creator_rug_rate"] = rep.creator_rug_rate
        f["creator_median_peak_multiple"] = rep.creator_median_peak_multiple
        f["creator_launches_last_hour"] = _as_f(rep.creator_launches_last_hour)
        f["early_buyer_quality"] = rep.early_buyer_quality
        f["early_buyer_flipper_share"] = rep.early_buyer_flipper_share
        f["cluster_size"] = _as_f(rep.cluster_size)

        g = self.regime
        f["regime_creation_rate"] = g.launchpad_creation_rate
        f["regime_graduation_rate"] = g.graduation_rate
        f["regime_dex_volume"] = g.aggregate_dex_volume
        f["regime_sol_return_24h"] = g.sol_return_24h
        f["regime_median_new_pool_liq"] = g.median_new_pool_liquidity

        return f


def _as_f(v) -> Optional[float]:
    return None if v is None else float(v)


def _sign(x: float) -> float:
    return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)


def _stdev(xs) -> Optional[float]:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _slope(xs) -> Optional[float]:
    """OLS slope, normalised by mean so it is scale-free across bands."""
    vals = [x for x in xs if x is not None]
    n = len(vals)
    if n < 3:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(vals) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(vals))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0 or mean_y == 0:
        return None
    return (num / den) / abs(mean_y)


FEATURE_NAMES = None  # populated on first call to TokenState.features()
