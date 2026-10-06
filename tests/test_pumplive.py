"""D144: pumplive -- decoder, metadata/rails, token state, paper fills vs the
anchored model, learner brake, and the engine end to end on synthetic events."""

import base64
import os
import struct
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tape import curve as cv  # noqa: E402
from tape.outcomes import TradeConfig, simulate_anchored  # noqa: E402
from tape.pumplive import events as ev  # noqa: E402
from tape.pumplive.engine import Engine, Settings  # noqa: E402
from tape.pumplive.learner import Learner  # noqa: E402
from tape.pumplive.meta import normalise_link, twitter_handle, twitter_kind  # noqa: E402
from tape.pumplive.paper import Config, open_position  # noqa: E402
from tape.pumplive.rails import RecentIndex, create_rails, decision_rails  # noqa: E402
from tape.pumplive.state import TokenState  # noqa: E402

MINT = bytes(range(1, 33))
CREATOR = bytes(range(40, 72))
K = 30.0 * 1.073e9


def trade_log(user: bytes, is_buy: bool, sol: float, vsol: float, ts=1_790_000_000, fee=None, creator=CREATOR):
    vtok = K / vsol
    tokens = abs(K / (vsol - (sol if is_buy else -sol)) - vtok)
    ext = None
    if fee is not None:
        ext = (bytes(32), 95, int(fee * 1e9), creator, 30, 0)
    b = ev.encode_trade(MINT, int(sol * 1e9), int(tokens * 1e6), is_buy, user, ts, int(vsol * 1e9), int(vtok * 1e6),
                        int((vsol - 30) * 1e9), int(max(vtok - 279_900_000, 0) * 1e6), ext)
    return "Program data: " + base64.b64encode(b).decode()


class TestDecoder(unittest.TestCase):
    def test_roundtrip_and_logs(self):
        logs = ["Program log: Instruction: Buy", trade_log(bytes(range(80, 112)), True, 0.5, 31.0, fee=0.006),
                "Program data: " + base64.b64encode(b"\x00" * 50).decode()]
        ts = ev.trades_from_logs(logs, slot=7, sig="S")
        self.assertEqual(len(ts), 1)
        t = ts[0]
        self.assertEqual(t.mint, ev.b58encode(MINT))
        self.assertTrue(t.is_buy)
        self.assertAlmostEqual(t.vsol, 31.0, places=6)
        self.assertAlmostEqual(t.k / K, 1.0, places=5)
        self.assertAlmostEqual(t.fee, 0.006, places=9)
        self.assertEqual(t.creator, ev.b58encode(CREATOR))
        self.assertEqual((t.slot, t.sig), (7, "S"))
        self.assertTrue(base64.b64encode(ev.TRADE_DISC).decode().startswith("vdt/007mYe"))

    def test_b58(self):
        self.assertEqual(ev.b58encode(b"\0\0\x01"), "112")
        self.assertEqual(ev.b58decode(ev.b58encode(MINT)), MINT)


class TestMetaRails(unittest.TestCase):
    def test_links(self):
        self.assertEqual(normalise_link("https://twitter.com/Foo_Bar/"), "x.com/foo_bar")
        self.assertEqual(twitter_kind("https://x.com/foo/status/123"), "tweet")
        self.assertEqual(twitter_kind("https://x.com/i/communities/123"), "community")
        self.assertEqual(twitter_kind("https://x.com/foo"), "profile")
        self.assertEqual(twitter_handle("https://x.com/foo/status/1"), "foo")
        self.assertEqual(twitter_kind(None), "none")

    def _tok(self, name="Cat", sym="CAT", creator="C1", dev=0.0):
        return TokenState("m", 0, creator, name=name, symbol=sym, dev_initial_tokens=dev)

    def test_create_rails(self):
        s = Settings()
        r = RecentIndex({"farm": {"launches": 50, "grads": 0}})
        self.assertIn("no_metadata", create_rails(s, self._tok(), None, r))
        self.assertIn("no_socials", create_rails(s, self._tok(), {"name": "x"}, r))
        meta = {"twitter": "https://x.com/catcoin"}
        self.assertEqual(create_rails(s, self._tok(), meta, r), [])
        for i in range(3):
            r.add(1000 + i, self._tok(name=f"n{i}", sym=f"s{i}", creator=f"c{i}"), meta)
        self.assertIn("reused_twitter", create_rails(s, self._tok(), meta, r))
        r.add(2000, self._tok(), {"twitter": "https://x.com/other"})
        self.assertIn("copycat_name", create_rails(s, self._tok(), {"twitter": "https://x.com/new1"}, r))
        self.assertIn("creator_farm", create_rails(s, self._tok(name="z", sym="z", creator="farm"),
                                                   {"twitter": "https://x.com/new2"}, r))
        self.assertIn("dev_initial_buy", create_rails(s, self._tok(name="q", sym="q", dev=2e8),
                                                      {"twitter": "https://x.com/new3"}, r))
        r._prune(1000 + 8 * 86400)                                    # links expire after 7 days
        self.assertEqual(r.link_count("x.com/catcoin"), 0)

    def test_decision_rails(self):
        s = Settings()
        tok = self._tok(creator="dev", dev=5e7)
        self.assertEqual(decision_rails(s, tok), [])
        tok.creator_sold_tokens = 1.0
        self.assertIn("dev_sold", decision_rails(s, tok))
        tok.creator_sold_tokens = 0.0
        tok.holders["whale"] = 6e8
        self.assertIn("top10_concentration", decision_rails(s, tok))
        tok2 = self._tok()
        tok2.k = K * 1.5
        self.assertIn("nonstandard_curve", decision_rails(s, tok2))


class TestPaperVsModel(unittest.TestCase):
    def test_position_equals_simulate_anchored(self):
        rng = np.random.default_rng(2)
        compared = 0
        for trial in range(40):
            n = 200
            slots = np.concatenate([[0], np.cumsum(rng.integers(0, 3, n - 1))]).astype(np.int64)
            ts = 3_600_000_000 + slots * 400                               # ms, ~0.4 s per slot
            q = 31.0 + np.abs(np.cumsum(rng.normal(0.05, 0.7, n)))
            tp, sl, L = [(0.2, -0.15, 2), (0.6, -0.3, 1), (1e9, -1e9, 2)][trial % 3]
            cfg = Config("c", "r", tp, sl, 60_000, 0.5, L, 0.001)
            tok = TokenState("m", 0, "dev", vsol=float(q[0]), vtok=K / float(q[0]))
            p = open_position(cfg, tok, int(slots[0]), int(ts[0]), 0.0125, 0.011)

            class T:
                pass
            for i in range(1, n):
                t = T()
                t.slot, t.vsol, t.vtok = int(slots[i]), float(q[i]), K / float(q[i])
                t.k = t.vsol * t.vtok
                p.on_trade(t, int(ts[i]))
                if p.state == "closed":
                    break
            if p.state != "closed":
                p.on_clock(int(ts[-1]) + 10 ** 9)
            o = simulate_anchored(ts, q, 0, K, 0.0125, 0.011,
                                  TradeConfig(size_sol=0.5, take_profit=tp, stop_loss=sl, horizon_ms=60_000,
                                              latency_s=9, fixed_cost_sol=0.001), None, None,
                                  slots=slots, latency_slots=L)
            if o.status in ("TP", "SL"):
                self.assertAlmostEqual(p.net, o.net_ret, places=9, msg=f"trial {trial} {o.status}")
                self.assertEqual(p.exit_reason, o.status)
                compared += 1
        self.assertGreater(compared, 15)


class TestLearner(unittest.TestCase):
    def test_abstain_and_champion(self):
        rng = np.random.default_rng(0)
        L = Learner(n_configs=100, min_trades=200)
        day = 86_400_000
        for i in range(600):
            L.add("noise", i * day // 20, rng.normal(0.0, 0.3))
        self.assertIsNone(L.champion(600 * day // 20))
        for i in range(600):
            L.add("edge", i * day // 20, rng.normal(0.08, 0.3))
        ch = L.champion(600 * day // 20)
        self.assertIsNotNone(ch)
        self.assertEqual(ch["cid"], "edge")


class TestEngine(unittest.TestCase):
    def test_end_to_end(self):
        class Rec:
            def __init__(self):
                self.rows = []

            def write(self, kind, **r):
                self.rows.append((kind, r))
        subs, unsubs = [], []
        s = Settings(min_trades=1)
        e = Engine(s, Rec(), subs.append, unsubs.append, {}, log=lambda m: None)
        mint = ev.b58encode(MINT)
        dev = ev.b58encode(CREATOR)
        t0 = 1_790_000_000_000
        e.on_create({"txType": "create", "mint": mint, "traderPublicKey": dev, "name": "A", "symbol": "A",
                     "uri": "u", "bondingCurveKey": "CURVE", "initialBuy": 1e7, "vSolInBondingCurve": 30.3,
                     "vTokensInBondingCurve": K / 30.3}, now_ms=t0)
        self.assertEqual(subs, ["CURVE"])
        e.on_meta(mint, {"twitter": "https://x.com/a"}, now_ms=t0 + 500)
        self.assertEqual(e.counts["watched"], 1)
        vs, slot, now = 30.3, 100, t0 + 1000
        for i in range(400):                                   # steady buying -> bars close -> decisions
            user = bytes([i % 200 + 1]) * 32
            vs += 0.02
            slot += 1
            now += 400
            e.on_logs("CURVE", slot, f"s{i}", [trade_log(user, True, 0.02, vs, fee=0.00025)], None, now_ms=now)
            e.on_clock(now)
        self.assertGreater(e.counts["decisions"], 0)
        self.assertTrue(e.positions.get(mint))
        e.on_clock(now + 31 * 60_000)                          # horizons over -> all closed
        self.assertTrue(all(p.state == "closed" for p in e.positions[mint]))
        rep = e.report(now + 31 * 60_000)
        self.assertGreater(rep["counts"]["closed"], 0)
        self.assertIn(rep["champion"], ["ABSTAIN"] + [c.cid for c in e.configs])
        e.on_clock(now + 62 * 60_000)                          # past the watch window -> unsubscribed
        self.assertEqual(unsubs, ["CURVE"])

    def test_rejected_at_create_unsubscribes(self):
        class Rec:
            def write(self, *a, **k):
                pass
        subs, unsubs = [], []
        e = Engine(Settings(), Rec(), subs.append, unsubs.append, {}, log=lambda m: None)
        e.on_create({"txType": "create", "mint": "M", "traderPublicKey": "D", "name": "A", "symbol": "A", "uri": "u",
                     "bondingCurveKey": "C2"}, now_ms=1)
        e.on_meta("M", None, now_ms=2)
        self.assertEqual(unsubs, ["C2"])
        self.assertEqual(e.reject_reasons.get("no_metadata"), 1)



class TestHeliusHandler(unittest.TestCase):
    def test_subscription_mapping_and_dispatch(self):
        import asyncio
        from tape.pumplive.feeds import HeliusLogs
        got = []

        class Eng:
            def on_logs(self, curve, slot, sig, logs, err):
                got.append((curve, slot, sig, len(logs), err))

        async def run():
            h = HeliusLogs("k", Eng(), lambda m: None)
            h.subscribe("CURVE")
            h.pending[1] = ("CURVE", "logsSubscribe")
            h._handle({"jsonrpc": "2.0", "id": 1, "result": 77})
            h._handle({"method": "logsNotification", "params": {"subscription": 77, "result": {
                "context": {"slot": 123}, "value": {"signature": "SIG", "err": None, "logs": ["a", "b"]}}}})
            h._handle({"method": "logsNotification", "params": {"subscription": 99, "result": {
                "context": {"slot": 1}, "value": {"signature": "X", "err": None, "logs": []}}}})
            return h
        h = asyncio.run(run())
        self.assertEqual(h.sub_of["CURVE"], 77)
        self.assertEqual(got, [("CURVE", 123, "SIG", 2, None)])

    def test_mayhem_and_duplicate_curve_skipped(self):
        class Rec:
            def write(self, *a, **k):
                pass
        subs, unsubs = [], []
        e = Engine(Settings(), Rec(), subs.append, unsubs.append, {}, log=lambda m: None)
        base = {"txType": "create", "traderPublicKey": "D", "name": "A", "symbol": "A", "uri": "u"}
        self.assertIsNone(e.on_create(dict(base, mint="MY", bondingCurveKey="SHARED", is_mayhem_mode=True), now_ms=1))
        self.assertIsNotNone(e.on_create(dict(base, mint="M1", bondingCurveKey="C1"), now_ms=1))
        self.assertIsNone(e.on_create(dict(base, mint="M2", bondingCurveKey="C1"), now_ms=1))
        self.assertEqual(subs, ["C1"])
        self.assertEqual(e.by_curve, {"C1": "M1"})
        self.assertNotIn("MY", e.tokens)
        self.assertEqual(e.reject_reasons, {"mayhem_create": 1, "dup_curve_key": 1})
        self.assertEqual(e.counts["create_skips"], 2)


if __name__ == "__main__":
    unittest.main()
