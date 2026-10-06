"""pump.fun TradeEvent decoding (Anchor event, emitted as a 'Program data:' log).

Discriminator = sha256("event:TradeEvent")[:8] = bddb7fd34ee661ee (base64 'vdt/007mYe...').
Layout (pump IDL, prefix that has been stable; newer versions only APPEND fields):
  mint pubkey | sol_amount u64 | token_amount u64 | is_buy bool | user pubkey |
  timestamp i64 | virtual_sol_reserves u64 | virtual_token_reserves u64 |
  real_sol_reserves u64 | real_token_reserves u64 |
  [fee_recipient pubkey | fee_basis_points u64 | fee u64 | creator pubkey |
   creator_fee_basis_points u64 | creator_fee u64]   (decoded when present)
Units: SOL = lamports / 1e9; tokens = raw / 1e6.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass
from typing import List, Optional

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CORE_LEN = 8 + 32 + 8 + 8 + 1 + 32 + 8 + 8 * 4
EXT_LEN = CORE_LEN + 32 + 8 + 8 + 32 + 8 + 8


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + raw


@dataclass
class Trade:
    mint: str
    sol: float              # SOL amount of the trade (event sol_amount)
    tokens: float           # tokens (event token_amount)
    is_buy: bool
    user: str
    ts: int                 # unix seconds (block time)
    vsol: float             # virtual SOL reserve AFTER the trade
    vtok: float             # virtual token reserve AFTER the trade
    real_sol: float
    real_tok: float
    fee: Optional[float] = None
    creator: Optional[str] = None
    creator_fee: Optional[float] = None
    slot: int = 0
    sig: str = ""
    idx: int = 0

    @property
    def k(self) -> float:
        return self.vsol * self.vtok


def decode_trade(data: bytes) -> Optional[Trade]:
    if len(data) < CORE_LEN or data[:8] != TRADE_DISC:
        return None
    o = 8
    mint = b58encode(data[o:o + 32]); o += 32
    sol, tok = struct.unpack_from("<QQ", data, o); o += 16
    is_buy = data[o] == 1; o += 1
    user = b58encode(data[o:o + 32]); o += 32
    ts, vs, vt, rs, rt = struct.unpack_from("<qQQQQ", data, o); o += 40
    t = Trade(mint, sol / 1e9, tok / 1e6, is_buy, user, int(ts), vs / 1e9, vt / 1e6, rs / 1e9, rt / 1e6)
    if len(data) >= EXT_LEN:
        o += 32
        _bps, fee = struct.unpack_from("<QQ", data, o); o += 16
        t.creator = b58encode(data[o:o + 32]); o += 32
        _cbps, cfee = struct.unpack_from("<QQ", data, o)
        t.fee, t.creator_fee = fee / 1e9, cfee / 1e9
    return t


def trades_from_logs(logs: List[str], slot: int = 0, sig: str = "") -> List[Trade]:
    out = []
    for line in logs or []:
        if not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[len("Program data: "):].strip())
        except Exception:  # noqa: BLE001
            continue
        t = decode_trade(raw)
        if t is not None:
            t.slot, t.sig, t.idx = slot, sig, len(out)
            out.append(t)
    return out


def encode_trade(mint: bytes, sol_l: int, tok_raw: int, is_buy: bool, user: bytes, ts: int, vs_l: int, vt_raw: int,
                 rs_l: int, rt_raw: int, ext: Optional[tuple] = None) -> bytes:
    """Test helper: the inverse of decode_trade."""
    b = TRADE_DISC + mint + struct.pack("<QQ", sol_l, tok_raw) + bytes([1 if is_buy else 0]) + user
    b += struct.pack("<qQQQQ", ts, vs_l, vt_raw, rs_l, rt_raw)
    if ext:
        fee_recipient, bps, fee_l, creator, cbps, cfee_l = ext
        b += fee_recipient + struct.pack("<QQ", bps, fee_l) + creator + struct.pack("<QQ", cbps, cfee_l)
    return b
