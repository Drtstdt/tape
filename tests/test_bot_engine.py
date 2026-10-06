"""Engine: one loop for backtest and live. Determinism, the no-lookahead
guarantee, first-class abstention rows, cooldown, clock sweep, tape_end."""

import sys, os, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape.bot.engine import Engine
from tape.bot.spec import BandSpec, BotSpec, EntrySpec
from tape.schema import CanonicalSwap


def make_spec(cooldown_ms=60_000):
    band = BandSpec("B_small", 15.0, 2.0, -0.40, 45 * 60_000, 0.5, 0.3,
                    cooldown_ms=cooldown_ms)
    entry = EntrySpec(min_age_ms=0, min_bars=3, min_unique_buyers_10=1.0,
                      max_largest_buyer_share_10=0.9,
                      gate_min_liq_slope_10=-0.5)
    return BotSpec(entry=entry, bands=[band], bar_fraction_of_liquidity=0.05)


def swap(mint, ts, price, qty, side, wallet, q_res=200.0):
    return CanonicalSwap(
        mint=mint, venue="test", pool="p", ts_ms=ts, slot=ts, sig=f"s{mint}{ts}{wallet}",
        side=side, base_amount=qty / price, quote_amount=qty,
        quote_mint="SOL", price=price, wallet=wallet,
        quote_reserve_after=q_res, base_reserve_after=q_res / price, source="t")


def rising_tape(mint="M", n0=10, steps=None, base_ts=0, step_ms=1000):
    """Flat accumulation (buys dominate), then a price path to TP and a
    post-TP decline. Deterministic."""
    out = []
    ts = base_ts
    price = 1.0
    wallets = ["w1", "w2", "w3", "w4"]
    # accumulation: 3 buys (2 SOL each) + 1 sell (1 SOL) per bar
    for i in range(n0):
        for j, w in enumerate(wallets):
            side = "buy" if j < 3 else "sell"
            qty = 2.0 if side == "buy" else 1.0
            out.append(swap(mint, ts, price, qty, side, w))
            ts += step_ms
    steps = steps or [1.1, 1.25, 1.5, 1.8, 2.05, 2.1, 2.0, 1.6, 1.35, 1.3]
    for p in steps:
        for j, w in enumerate(wallets):
            side = "buy" if j < 2 else "sell"
            qty = 2.0 if side == "buy" else 1.0
            out.append(swap(mint, ts, p, qty, side, w))
            ts += step_ms
    return out


def run(swaps_by_mint, spec=None, **kw):
    eng = Engine(spec or make_spec(), base_dir=kw.pop("base_dir", "data"),
                 ledger_dir=kw.pop("ledger_dir", None))
    for mint, swaps in swaps_by_mint.items():
        eng.begin_mint(mint, created_ts_ms=swaps[0].ts_ms)
        for s in swaps:
            eng.on_swap(s)
        eng.end_tape(mint)
    return eng


class TestEngineMechanics(unittest.TestCase):
    def test_enter_partial_trail_exit_cycle(self):
        eng = run({"M": rising_tape()})
        trades = [t for t in eng._trade_rows]
        sides = [t["side"] for t in trades]
        self.assertIn("enter", sides)
        self.assertIn("exit_partial", sides)
        self.assertIn("exit", sides)
        decisions = eng._decision_rows
        self.assertTrue(any(d["action"] == "enter" and
                            d["reason"] == "no_model_flat_size"
                            for d in decisions))
        self.assertTrue(any(d["action"] == "abstain" for d in decisions)
                        or len(decisions) > 3)   # abstentions are rows too
        closed = [t for t in trades if t["side"] == "exit" and t["pnl_sol"] is not None]
        self.assertEqual(len(closed), 1)
        self.assertGreater(closed[0]["pnl_sol"], 0)   # partial + trail > entry

    def test_abstentions_are_rows_with_reasons(self):
        # rails reject young tokens -> rows, not silence
        eng = run({"M": rising_tape(n0=2, steps=[1.0])},
                  spec=make_spec())
        rows = eng._decision_rows
        self.assertTrue(rows)
        self.assertTrue(all(r["reason"] for r in rows if r["action"] != "enter"))

    def test_deterministic_same_tape_same_ledger(self):
        a = run({"M": rising_tape()})
        b = run({"M": rising_tape()})
        self.assertEqual(a._decision_rows, b._decision_rows)
        self.assertEqual(a._trade_rows, b._trade_rows)

    def test_no_lookahead_truncated_tape(self):
        full = rising_tape()
        cut = 40
        eng_full = run({"M": full})
        eng_cut = run({"M": full[:cut]})
        cut_ts = full[cut - 1].ts_ms if cut < len(full) else full[-1].ts_ms
        rows_full = [r for r in eng_full._decision_rows if r["ts_ms"] < cut_ts]
        rows_cut = [r for r in eng_cut._decision_rows if r["ts_ms"] < cut_ts]
        self.assertEqual(rows_cut, rows_full)

    def test_cooldown_blocks_immediate_rentry(self):
        eng = Engine(make_spec(cooldown_ms=10 ** 12))
        eng.begin_mint("M", created_ts_ms=0)
        for s in rising_tape():
            eng.on_swap(s)
        eng.end_tape("M")
        # feed a second tape on the same mint shortly after the exit
        t_end = eng.last_bar["M"].close_ts_ms
        eng.begin_mint("M", created_ts_ms=t_end)
        for s in rising_tape(base_ts=t_end + 1000, n0=4, steps=[1.0]):
            eng.on_swap(s)
        eng.end_tape("M")
        self.assertTrue(any(d["action"] == "abstain" and d["reason"] == "cooldown"
                            for d in eng._decision_rows))

    def test_sweep_closes_time_stop_without_tape(self):
        eng = Engine(make_spec())
        eng.begin_mint("M", created_ts_ms=0)
        for s in rising_tape(n0=6, steps=[1.05]):
            eng.on_swap(s)
        self.assertTrue(eng.positions)
        eng.sweep(10 ** 15)   # far past every horizon
        self.assertFalse(eng.positions)
        self.assertTrue(any(t["side"] == "exit" and t["reason"] == "time_stop"
                            for t in eng._trade_rows))


if __name__ == "__main__":
    unittest.main()
