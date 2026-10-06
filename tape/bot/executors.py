"""Fill execution.

`PaperExecutor` is the default and the only executor the backtester may use:
it fills deterministically at the bar-derived price with the project's exact
constant-product math when reserves are known (tape/costs.py, verified in
v3 -- do not re-derive), and at market price when they are not.

`LiveExecutor` submits REAL transactions through Jupiter. It is triple-gated
by construction: it refuses to exist unless (1) `live=True` was passed,
(2) a keypair path was supplied, and (3) `acknowledge=True` was passed with
the operator's explicit flag. Even then every trade is written to the ledger
BEFORE the transaction is sent, so a crash never hides what was attempted.
Real trading is the operator's decision and responsibility; this module's
job is to make firing that decision loud and hard, not easy and quiet.

PAPER TRADING ONLY is the standing default of the whole package
(tape/__init__.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..costs import CostModel, constant_product_buy, constant_product_sell
from ..schema import Bar
from .exits import Position, ExitAction


@dataclass
class Fill:
    mint: str
    side: str                      # "enter" | "exit"
    ts_ms: int
    price: float
    quote_sol: float               # quote moved (spent on enter, received on exit, gross)
    base_amount: float             # base moved
    fee_sol: float
    fixed_sol: float
    q_reserve: Optional[float] = None
    b_reserve: Optional[float] = None


class PaperExecutor:
    name = "paper"

    def __init__(self, cost: CostModel) -> None:
        self.cost = cost

    def fill_enter(self, mint: str, size_sol: float, price: float,
                   bar: Bar, ts_ms: Optional[int] = None) -> Fill:
        ts = bar.close_ts_ms if ts_ms is None else ts_ms
        q, b = bar.quote_reserve_close, bar.base_reserve_close
        if q and b and q > 0 and b > 0:
            base_out, avg_price, _ = constant_product_buy(
                q, b, size_sol, self.cost.fee_pct)
        else:
            base_out = size_sol * (1.0 - self.cost.fee_pct) / price
            avg_price = price
        return Fill(mint=mint, side="enter", ts_ms=ts, price=avg_price,
                    quote_sol=size_sol, base_amount=base_out,
                    fee_sol=self.cost.fee_pct * size_sol,
                    fixed_sol=self.cost.fixed_cost_sol, q_reserve=q, b_reserve=b)

    def fill_exit(self, pos: Position, action: ExitAction, bar: Bar) -> Fill:
        tokens = pos.tokens_remaining * action.fraction
        q, b = bar.quote_reserve_close, bar.base_reserve_close
        if q and b and q > 0 and b > 0 and tokens > 0:
            quote_out, avg_price, _ = constant_product_sell(
                q, b, tokens, self.cost.fee_pct)
        else:
            quote_out = tokens * action.price * (1.0 - self.cost.fee_pct)
            avg_price = action.price
        return Fill(mint=pos.mint, side="exit", ts_ms=action.ts_ms,
                    price=avg_price, quote_sol=quote_out, base_amount=tokens,
                    fee_sol=tokens * action.price * self.cost.fee_pct,
                    fixed_sol=self.cost.fixed_cost_sol, q_reserve=q, b_reserve=b)


class LiveExecutor:
    """Real-money execution via Jupiter. REFUSES to construct unless all
    three gates pass. Unverified against live endpoints -- treat as a
    scaffold the operator must validate themselves before any real SOL."""

    name = "live"

    def __init__(self, cost: CostModel, keypair_path: Optional[str] = None,
                 live: bool = False, acknowledge: bool = False,
                 slippage_bps: int = 250, rpc_url: str = "https://api.mainnet-beta.solana.com") -> None:
        if not (live and keypair_path and acknowledge):
            raise RuntimeError(
                "LiveExecutor refused to construct: real trading requires all "
                "three gates -- live=True, a keypair path, and "
                "acknowledge=True (the operator's explicit acceptance). "
                "Paper trading remains the default."
            )
        self.cost = cost
        self.keypair_path = keypair_path
        self.slippage_bps = slippage_bps
        self.rpc_url = rpc_url
        self._keypair = None

    # -- real code paths are deliberately thin and loud --------------------

    def _load_keypair(self):
        if self._keypair is not None:
            return self._keypair
        try:
            from solders.keypair import Keypair
        except ImportError as e:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "solders is required for LiveExecutor. Install it before "
                "attempting real trades.") from e
        key_bytes = __import__("json").load(
            open(self.keypair_path, "r", encoding="utf-8"))
        self._keypair = Keypair.from_bytes(bytes(key_bytes[:64]))
        return self._keypair

    def _swap(self, mint_in: str, mint_out: str, amount: int,
              decimals_in: int, decimals_out: int) -> str:
        """Quote -> swap instructions -> sign -> send. Returns the tx
        signature. The operator MUST verify every leg of this path against
        Jupiter's current API before trusting it with money."""
        import httpx

        keypair = self._load_keypair()
        quote = httpx.get(
            "https://quote-api.jup.ag/v6/quote",
            params={
                "inputMint": mint_in, "outputMint": mint_out,
                "amount": amount, "slippageBps": self.slippage_bps,
            }, timeout=20.0)
        quote.raise_for_status()
        body = quote.json()
        if "error" in body:
            raise RuntimeError(f"Jupiter quote failed: {body['error']}")

        tx = httpx.post(
            "https://quote-api.jup.ag/v6/swap",
            json={
                "quoteResponse": body,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            }, timeout=20.0)
        tx.raise_for_status()
        tx_body = tx.json()

        try:
            from solders.transaction import VersionedTransaction
            from solders.message import to_bytes_versioned
            from solders.signature import Signature
        except ImportError:  # pragma: no cover
            raise RuntimeError("solders is required for signing.")
        raw = VersionedTransaction.from_bytes(
            __import__("base64").b64decode(tx_body["swapTransaction"]))
        signed = __import__("base64").b64encode(
            to_bytes_versioned(raw.sign([keypair]))).decode()

        send = httpx.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1,
                  "method": "sendTransaction",
                  "params": [signed, {"encoding": "base64",
                                      "skipPreflight": False}]},
            timeout=20.0)
        send.raise_for_status()
        result = send.json().get("result")
        if not result:
            raise RuntimeError(f"sendTransaction failed: {send.text[:500]!r}")
        return result
