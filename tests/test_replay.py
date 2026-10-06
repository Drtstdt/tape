"""D145: replay of the live bot on history (tape/pumplive/replay.py, scripts/replay_pumplive.py)."""

import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from tape import curve as cv  # noqa: E402
from tape.fastexit import exit_grid  # noqa: E402
from tape.pumplive import replay as rp  # noqa: E402
from tape.pumplive.engine import Settings, build_configs  # noqa: E402
from tape.pumplive.learner import Learner  # noqa: E402
from tape.pumplive.paper import Config, open_position  # noqa: E402
from tape.pumplive.state import TokenState  # noqa: E402

K = cv.K_STD


def synth_token(rng, n=400, creator="DEV", dev_sells_at=None, bundle=0.0, t0=1_770_000_000_000, slot0=1000):
    """Standard-curve swaps (rows as stored: ts, slot, side, amounts, real reserve after)."""
    import pandas as pd
    rows, real = [], 0.0
    slot = slot0
    ts = t0
    # create slot: dev buys 0.5 SOL, then optional bundle buys by others in the same slot
    first = [("buy", 0.5, creator)] + ([("buy", bundle, "SNIPER")] if bundle else [])
    for i in range(n):
        if i < len(first):
            side, amt, w = first[i]
        else:
            if i > len(first) and rng.random() < 0.5:
                slot += int(rng.integers(1, 3))
                ts = t0 + (slot - slot0) * 400
            side = "buy" if rng.random() < 0.6 else "sell"
            w = f"W{rng.integers(0, 40)}"
            amt = float(rng.uniform(0.05, 0.8))
            if dev_sells_at is not None and i == dev_sells_at:
                side, w = "sell", creator
        fee = 0.0125 * amt
        q_pre = cv.V_STD + real
        if side == "buy":
            q_post = q_pre + amt
        else:
            amt = min(amt, max(real - 0.01, 0.0))
            if amt <= 0:
                side, amt, q_post = "buy", 0.1, q_pre + 0.1
                fee = 0.0125 * amt
            else:
                fee = 0.0125 * amt
                q_post = q_pre - amt - fee
        toks = abs(K / q_pre - K / q_post)
        real = q_post - cv.V_STD
        rows.append({"mint": "MINT", "ts_ms": ts, "slot": slot, "sig": f"s{i:05d}", "side": side,
                     "base_amount": toks, "quote_amount": amt, "wallet": w, "real_quote_reserve_after": real,
                     "fee_sol": fee})
    return pd.DataFrame(rows)


def paths_for(df, ks=(5, 10, 20), path_ms=31 * 60_000):
    """Same construction as scripts/build_paths.py for one token."""
    import pandas as pd
    import build_research_set_v2 as b2
    g, q_mkt, params, _ = b2.prepare_token(df)
    ts = g["ts_ms"].to_numpy(dtype="int64")
    slots = g["slot"].to_numpy(dtype="int64")
    closes = b2.bar_close_indices(g["quote_amount"].to_numpy(dtype=float),
                                  float(g["quote_reserve_after"].to_numpy()[0]))
    meta, rel, sl, q, off = [], [], [], [], 0
    for Kb in ks:
        if len(closes) < Kb:
            continue
        d = closes[Kb - 1]
        end = int(np.searchsorted(ts, ts[d] + path_ms, side="right"))
        rel.append(ts[d:end] - ts[d]); sl.append(slots[d:end] - slots[d]); q.append(q_mkt[d:end])
        meta.append({"mint": "MINT", "K": Kb, "create_ts": int(ts[0]), "decision_ts": int(ts[d]),
                     "day": int(ts[d]) // rp.DAY_MS, "week": 0, "k": float(params["k"]), "fee_b": 0.0125,
                     "fee_s": 0.0125, "grad": end - d, "offset": off, "length": end - d})
        off += end - d
    return (pd.DataFrame(meta), np.concatenate(rel), np.concatenate(sl), np.concatenate(q).astype(np.float32),
            g, q_mkt, closes)


INFO = {"creator": "DEV", "create_ts": 1_770_000_000_000, "create_slot": 1000, "prior_launches": 0.0,
        "prior_grads": 0.0, "launches_24h": 0.0}


class TestReplayToken(unittest.TestCase):
    def test_bars_rails_and_grid(self):
        import replay_pumplive as rpl
        rng = np.random.default_rng(5)
        s = Settings()
        df = synth_token(rng, dev_sells_at=15)
        meta, rel, sl, q, g, q_mkt, closes = paths_for(df)
        rows, mism = rpl.process_bucket_frames(s, meta, {"MINT": INFO}, df, rel, sl, q)
        self.assertEqual(mism, {"decision": 0, "bars": 0, "no_swaps": 0, "no_info": 0})
        self.assertEqual(sorted(r["K"] for r in rows), sorted(meta["K"].tolist()))
        by_k = {r["K"]: r for r in rows}
        # dev sells at swap 15: rail off for K5/K10 (swaps 4, 10), on for K20 (swap 23)
        for Kb, r in by_k.items():
            self.assertEqual(r["r_dev_sold"], closes[Kb - 1] >= 15, Kb)
            self.assertFalse(r["r_nonstandard_curve"])
            self.assertFalse(r["r_creator_farm"])
        # 0.5 SOL dev buy = ~16 M tokens = 1.6% < 10%
        self.assertAlmostEqual(by_k[5]["dev_initial_share"], (K / 30.0 - K / 30.5) / 1e9, places=9)
        self.assertFalse(by_k[5]["r_dev_initial_buy"])
        # nets equal exit_grid on the path, in exit_combos order
        m0 = meta.iloc[0]
        G = exit_grid(rel[:m0.length].astype(np.int64), sl[:m0.length].astype(np.int64), q[:m0.length].astype(float),
                      int(m0.grad), float(m0.k), 0.0125, 0.0125, s.size_sol, s.latency_slots, s.tip_sol,
                      np.array(s.tps), np.array(s.sls), np.array(s.horizons_min, dtype=np.int64) * 60_000).reshape(-1)
        r0 = by_k[int(m0.K)]
        np.testing.assert_allclose([r0[f"n{j:02d}"] for j in range(len(G))], G)

    def test_bundle_and_creator_rails(self):
        import replay_pumplive as rpl
        rng = np.random.default_rng(6)
        s = Settings()
        df = synth_token(rng, bundle=12.0)                       # sniper buys ~30% of supply in the create slot
        meta, rel, sl, q, *_ = paths_for(df)
        info = dict(INFO, prior_launches=25.0, prior_grads=0.0, launches_24h=5.0)
        rows, _ = rpl.process_bucket_frames(s, meta, {"MINT": info}, df, rel, sl, q)
        for r in rows:
            self.assertTrue(r["r_bundle_first_slot"])
            self.assertTrue(r["r_creator_farm"])
            self.assertTrue(r["r_serial_creator_24h"])
            self.assertGreater(r["first_slot_share"], 0.25)

    def test_config_order_matches_live(self):
        s = Settings()
        self.assertEqual(rp.config_ids(s), [c.cid for c in build_configs(s)])


class TestReplayExitsEqualLivePosition(unittest.TestCase):
    def test_tp_sl_exits(self):
        rng = np.random.default_rng(9)
        s = Settings()
        compared = 0
        for trial in range(60):
            n = 300
            slots = np.concatenate([[0], np.cumsum(rng.integers(0, 3, n - 1))]).astype(np.int64)
            ts = slots * 400
            q = 31.0 + np.abs(np.cumsum(rng.normal(0.03, 0.6, n)))
            G = exit_grid(ts, slots, q, n, K, 0.0125, 0.0125, s.size_sol, s.latency_slots, s.tip_sol,
                          np.array(s.tps), np.array(s.sls), np.array([10 ** 9], dtype=np.int64))
            for a, tp in enumerate(s.tps):
                for b, sl in enumerate(s.sls):
                    cfg = Config("c", "r", tp, sl, 10 ** 9, s.size_sol, s.latency_slots, s.tip_sol)
                    tok = TokenState("m", 0, "dev", vsol=float(q[0]), vtok=K / float(q[0]))
                    p = open_position(cfg, tok, int(slots[0]), int(ts[0]), 0.0125, 0.0125)

                    class T:
                        pass
                    for i in range(1, n):
                        t = T()
                        t.slot, t.vsol, t.vtok = int(slots[i]), float(q[i]), K / float(q[i])
                        t.k = t.vsol * t.vtok
                        p.on_trade(t, int(ts[i]))
                        if p.state == "closed":
                            break
                    if p.state == "closed" and p.exit_reason in ("TP", "SL"):
                        self.assertAlmostEqual(p.net, G[a, b, 0], places=9)
                        compared += 1
        self.assertGreater(compared, 300)


class TestLearnerReplay(unittest.TestCase):
    def test_champion_equals_live_learner(self):
        rng = np.random.default_rng(11)
        C, n = 6, 3000
        nets = rng.normal(0, 0.2, (n, C))
        nets[:, 2] += 0.08                                       # a real edge in config 2
        nets[rng.random((n, C)) < 0.05] = np.nan
        close = np.sort(rng.integers(0, 40 * rp.DAY_MS, n))[:, None].repeat(C, axis=1)
        elig = rng.random((n, C)) < 0.7
        now = 40 * rp.DAY_MS
        L = Learner(C, window_days=30, min_trades=200)
        for i in range(n):
            for c in range(C):
                if elig[i, c]:
                    L.add(f"c{c}", int(close[i, c]), nets[i, c])
        live = L.stats(now)
        z = rp.learner_z(C)
        ci, rows = rp.champion_at(now, close, nets, elig, z, 30 * rp.DAY_MS, 200)
        self.assertEqual([f"c{r[0]}" for r in rows], [r["cid"] for r in live])
        for r, l in zip(rows, live):
            self.assertEqual(r[1], l["n"])
            self.assertAlmostEqual(r[5], l["lcb"], places=10)
        champ = L.champion(now)
        self.assertEqual(ci, 2)
        self.assertEqual(champ["cid"], "c2")

    def test_abstain_on_noise(self):
        rng = np.random.default_rng(12)
        C, n = 20, 4000
        nets = rng.normal(-0.01, 0.2, (n, C))
        close = np.sort(rng.integers(0, 30 * rp.DAY_MS, n))[:, None].repeat(C, axis=1)
        ci, _ = rp.champion_at(30 * rp.DAY_MS, close, nets, np.ones((n, C), bool), rp.learner_z(C),
                               30 * rp.DAY_MS, 200)
        self.assertIsNone(ci)


def _has_parquet():
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_has_parquet(), "needs pyarrow")
class TestAnalyzeEndToEnd(unittest.TestCase):
    def test_noise_abstains_and_registers_once(self):
        import tempfile
        import pandas as pd
        import replay_pumplive as rpl
        from tape import pf_store as ps
        from tape.registry import Registry
        rng = np.random.default_rng(21)
        s = Settings()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            os.makedirs(os.path.join(out, "discovery"))
            t0 = 2900 * rp.DAY_MS
            for b in range(ps.N_BUCKETS):
                n = 120
                dts = np.sort(t0 + rng.integers(0, 70 * rp.DAY_MS, n))
                d = {"mint": [f"m{b}_{i}" for i in range(n)], "K": rng.choice([5, 10, 20], n),
                     "create_ts": dts - 60_000, "decision_ts": dts, "day": dts // rp.DAY_MS,
                     "week": (dts - rp.MONDAY0) // rp.WEEK_MS, "q_gain": rng.uniform(0, 6, n)}
                for r in rp.ALL_RAILS:
                    d[f"r_{r}"] = rng.random(n) < 0.05
                for j in range(36):
                    d[f"n{j:02d}"] = rng.normal(-0.02, 0.2, n)
                pd.DataFrame(d).to_parquet(os.path.join(out, "discovery", f"part-b{b:02d}.parquet"))
            reg_dir = os.path.join(tmp, "reg")
            os.makedirs(reg_dir)
            args = ["--analyze-only", "--out", out, "--registry", reg_dir]
            self.assertEqual(rpl.main(args), 0)
            tr = {t["name"]: t for t in Registry(reg_dir).trials().values()}
            self.assertEqual(len(tr), 3)
            r0 = tr["R learner walk-forward (on-chain rails)"]
            self.assertEqual(r0["status"], "screened_out")
            self.assertEqual(rpl.main(args), 0)                  # re-analysis does not register again
            self.assertEqual(len(Registry(reg_dir).trials()), 3)
            log = open(os.path.join(out, "discovery", "replay_log.txt"), encoding="utf-8").read()
            self.assertIn("ABSTAIN", log)
            self.assertIn("already registered", log)

    def test_stage2_end_to_end(self):
        import json as _j
        import tempfile
        import pandas as pd
        import replay_stage2 as rs2
        from tape import pf_store as ps
        from tape.registry import Registry
        rng = np.random.default_rng(31)
        with tempfile.TemporaryDirectory() as tmp:
            rep = os.path.join(tmp, "rep")
            meta = os.path.join(tmp, "meta")
            os.makedirs(rep)
            os.makedirs(meta)
            t0 = 2900 * rp.DAY_MS
            das, ipfs = open(os.path.join(meta, "das.jsonl"), "w"), open(os.path.join(meta, "ipfs.jsonl"), "w")
            for b in range(ps.N_BUCKETS):
                n = 100
                dts = np.sort(t0 + rng.integers(0, 60 * rp.DAY_MS, n))
                mints = [f"m{b}_{i}" for i in range(n)]
                d = {"mint": mints, "K": rng.choice([5, 10, 20], n), "create_ts": dts - 60_000, "decision_ts": dts,
                     "day": dts // rp.DAY_MS, "week": (dts - rp.MONDAY0) // rp.WEEK_MS, "q_gain": rng.uniform(0, 6, n)}
                for r in rp.ALL_RAILS:
                    d[f"r_{r}"] = rng.random(n) < 0.03
                tw = rng.random(n) < 0.5
                for j in range(36):
                    d[f"n{j:02d}"] = rng.normal(-0.02, 0.2, n) + 0.08 * tw * (j == 7)
                pd.DataFrame(d).to_parquet(os.path.join(rep, f"part-b{b:02d}.parquet"))
                for i, m in enumerate(mints):
                    das.write(_j.dumps({"mint": m, "status": "ok", "uri": f"u{m}", "name": m, "symbol": m}) + "\n")
                    st = "fail" if i % 25 == 0 else "ok"
                    ipfs.write(_j.dumps({"mint": m, "status": st, "twitter": f"https://x.com/h{m.replace('_', '')}"
                                         if tw[i] else None, "telegram": None, "website": None}) + "\n")
            das.close(); ipfs.close()
            reg = os.path.join(tmp, "reg"); os.makedirs(reg)
            args = ["--replay", rep, "--meta", meta, "--registry", reg]
            self.assertEqual(rs2.main(args), 0)
            t = [x for x in Registry(reg).trials().values() if x["name"] == rs2.R003_NAME]
            self.assertEqual(len(t), 1)
            self.assertEqual(t[0]["status"], "passed_discovery")   # planted edge on tokens WITH a social
            self.assertEqual(rs2.main(args), 0)
            self.assertEqual(len(Registry(reg).trials()), 1)


class TestStage2Meta(unittest.TestCase):
    def test_parsers(self):
        from fetch_token_meta import parse_das, parse_meta_json
        self.assertEqual(parse_das(None)["status"], "missing")
        r = parse_das({"id": "M", "content": {"json_uri": " https://ipfs.io/ipfs/Qm1 ",
                                              "metadata": {"name": "Moat", "symbol": "MOAT"}}})
        self.assertEqual((r["status"], r["uri"], r["name"], r["symbol"]), ("ok", "https://ipfs.io/ipfs/Qm1", "Moat", "MOAT"))
        self.assertIsNone(parse_das({"id": "M", "content": {"json_uri": ""}})["uri"])
        st, f = parse_meta_json(b'{"name":"A","twitter":"https://x.com/abc","telegram":"","website":null}')
        self.assertEqual((st, f), ("ok", {"twitter": "https://x.com/abc", "telegram": None, "website": None}))
        self.assertEqual(parse_meta_json(b"\x89PNG....")[0], "bad_json")
        self.assertEqual(parse_meta_json(b"[1,2]")[0], "bad_json")

    def test_known_metas(self):
        from replay_stage2 import known_metas
        das = {"A": {"status": "ok", "uri": "u"}, "B": {"status": "ok", "uri": None}, "C": {"status": "missing"},
               "D": {"status": "ok", "uri": "u"}, "E": {"status": "ok", "uri": "u"}}
        ipfs = {"A": {"status": "ok", "twitter": "t", "telegram": None, "website": None},
                "D": {"status": "fail"}, "E": {"status": "bad_json"}}
        k = known_metas(das, ipfs)
        self.assertEqual(k, {"A": {"twitter": "t", "telegram": None, "website": None}, "B": None, "E": None})

    def test_meta_rails_point_in_time(self):
        s = Settings()
        H = 3_600_000
        tw = {"twitter": "https://twitter.com/samehandle", "telegram": None, "website": None}
        toks, metas = [], {}
        for i in range(4):                                    # 4 tokens share one X profile
            toks.append((f"T{i}", i * H, "c", f"name{i}", f"S{i}"))
            metas[f"T{i}"] = tw
        toks.append(("N0", 5 * H, "c", "Pepe", "PEPE")); metas["N0"] = {"twitter": None, "telegram": None,
                                                                        "website": "pepe.io"}
        toks.append(("N1", 6 * H, "c", "pepe ", "pepe")); metas["N1"] = {"twitter": None, "telegram": None,
                                                                         "website": "other.io"}
        toks.append(("U", 7 * H, "c", "x", "x"))                # unknown today: absent from metas
        toks.append(("Z", 8 * H, "c", "z", "z")); metas["Z"] = None
        toks.append(("L", 8 * 24 * H, "c", "late", "L")); metas["L"] = tw   # > 7 days later: reuse expired
        r = rp.meta_rails_in_order(s, toks, metas)
        self.assertEqual(r["T0"], []); self.assertEqual(r["T2"], [])
        self.assertIn("reused_twitter", r["T3"])                 # 3 earlier > max_link_reuse 2
        self.assertEqual(r["N0"], [])
        self.assertEqual(r["N1"], ["copycat_name"])
        self.assertNotIn("U", r)
        self.assertEqual(r["Z"], ["no_metadata", "no_socials"])
        self.assertEqual(r["L"], [])


class TestFetchPhasesOffline(unittest.TestCase):
    def test_das_and_ipfs_with_fake_client(self):
        import asyncio
        import json as _j
        import tempfile
        from pathlib import Path
        import fetch_token_meta as ftm

        class R:
            def __init__(self, code, body=b"", js=None):
                self.status_code, self.content, self._js = code, body, js

            def json(self):
                return self._js

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(self.status_code)

        class Fake:
            def __init__(self):
                self.calls = 0
                self.dead_calls = 0

            async def post(self, url, json=None, timeout=None):
                self.calls += 1
                ids = json["params"]["ids"]
                if self.calls == 1:
                    return R(429)
                res = [None if m == "M2" else {"id": m, "content": {"json_uri": (f"https://dead.host/m/{m}.json"
                                                                       if m.startswith("D") else
                                                                       f"https://ipfs.io/ipfs/Q{m}"),
                                                                       "metadata": {"name": m, "symbol": m}}}
                       for m in ids]
                return R(200, js={"result": res})

            async def get(self, url, timeout=None):
                if "QM0" in url:
                    return R(200, b'{"twitter":"https://x.com/a"}')
                if "QM1" in url and "filebase" in url:
                    return R(429)
                if "QM1" in url and "4everland" in url:
                    return R(200, b'{"website":"w.io"}')
                if "dead.host" in url:
                    self.dead_calls += 1
                    return R(404)
                return R(504)

        async def run(tmp):
            orig = ftm.asyncio.sleep

            async def nosleep(_):
                return None
            ftm.asyncio.sleep = nosleep
            try:
                c = Fake()
                mints = ["M0", "M1", "M2", "M3"] + [f"D{i:02d}" for i in range(40)]
                await ftm.das_phase(c, "k", mints, Path(tmp) / "das.jsonl", lambda m: None, rps=1e9)
                das = ftm.done_mints(Path(tmp) / "das.jsonl")
                st, _ = await ftm.ipfs_phase(c, das, mints, Path(tmp) / "ipfs.jsonl", lambda m: None, conc=1)
                return das, ftm.done_mints(Path(tmp) / "ipfs.jsonl"), st, c.dead_calls
            finally:
                ftm.asyncio.sleep = orig

        with tempfile.TemporaryDirectory() as tmp:
            das, ipfs, st, dead_calls = asyncio.run(run(tmp))
        self.assertEqual(das["M2"]["status"], "missing")
        self.assertEqual(das["M0"]["uri"], "https://ipfs.io/ipfs/QM0")
        self.assertEqual(ipfs["M0"]["status"], "ok"); self.assertEqual(ipfs["M0"]["twitter"], "https://x.com/a")
        self.assertEqual((ipfs["M1"]["status"], ipfs["M1"]["website"], ipfs["M1"]["gw"]), ("ok", "w.io", "4everland.io"))
        self.assertTrue(set(ipfs["M1"]["errs"]) <= {"429", "504"})     # whatever gateways came before 4everland
        self.assertIn(ipfs["M0"]["gw"], {ftm.host_of(g) for g in __import__("tape.pumplive.meta", fromlist=["x"]).IPFS_GATEWAYS})
        self.assertEqual(ipfs["M2"]["status"], "no_uri")
        self.assertEqual(ipfs["M3"]["status"], "fail")
        self.assertEqual((st["ok"], st["fail"], st["no_uri"]), (2, 41, 1))
        self.assertEqual(dead_calls, ftm.HOST_DEAD_AFTER)           # then the dead host is skipped
        self.assertEqual(ipfs["D39"]["errs"], ["host_dead"])
        from tape.pumplive.meta import IPFS_GATEWAYS
        self.assertEqual(ipfs["M3"]["errs"], ["504"] * len(IPFS_GATEWAYS))  # every gateway; ipfs.io itself never


class TestGatewayUrls(unittest.TestCase):
    def test_order_and_schemes(self):
        from tape.pumplive.meta import IPFS_GATEWAYS, cid_of, gateway_urls
        self.assertEqual(list(gateway_urls("https://ipfs.io/ipfs/QmA")), [g + "QmA" for g in IPFS_GATEWAYS])
        self.assertEqual(list(gateway_urls("https://x-1.mypinata.cloud/ipfs/QmC")),     # a dedicated gateway is kept
                         [g + "QmC" for g in IPFS_GATEWAYS] + ["https://x-1.mypinata.cloud/ipfs/QmC"])
        self.assertEqual(list(gateway_urls("ipfs://QmB")), [g + "QmB" for g in IPFS_GATEWAYS])
        self.assertEqual(list(gateway_urls("https://meta.uxento.io/data/x.json")),
                         ["https://meta.uxento.io/data/x.json"])
        self.assertEqual(list(gateway_urls("sdcsd")), [])
        self.assertIsNone(cid_of("https://metadata.j7tracker.com/metadata/a.json"))

    def test_limiter_backoff(self):
        import fetch_token_meta as ftm
        L = ftm.HostLimiter(interval=0.05, floor=0.01, ceil=5.0)
        for _ in range(20):
            L.on_429()
        self.assertAlmostEqual(L.interval, 0.125)               # a burst of 429s = one back-off
        for _ in range(20):
            L.last_backoff = -1e9
            L.on_429()
        self.assertEqual(L.interval, 5.0)
        for _ in range(500):
            L.on_ok()
        self.assertAlmostEqual(L.interval, 0.01)


class TestSourceBreaker(unittest.TestCase):
    def test_never_ok_source_is_skipped_but_gateways_never(self):
        import asyncio
        import tempfile
        from pathlib import Path
        import fetch_token_meta as ftm

        class R:
            def __init__(self, code, body=b""):
                self.status_code, self.content = code, body

        class Fake:
            def __init__(self):
                self.n = {"filebase": 0, "own": 0}

            async def get(self, url, timeout=None):
                if "filebase" in url:
                    self.n["filebase"] += 1
                    return R(504)
                if "own.example" in url:
                    self.n["own"] += 1
                    return R(429)
                return R(504)

        async def run(tmp):
            orig = ftm.asyncio.sleep

            async def nosleep(_):
                return None
            ftm.asyncio.sleep = nosleep
            try:
                c = Fake()
                das = {f"M{i}": {"status": "ok", "uri": f"https://own.example/ipfs/Q{i}"} for i in range(50)}
                st, _ = await ftm.ipfs_phase(c, das, list(das), Path(tmp) / "i.jsonl", lambda m: None, conc=1)
                return st, c.n, ftm.done_mints(Path(tmp) / "i.jsonl")
            finally:
                ftm.asyncio.sleep = orig

        with tempfile.TemporaryDirectory() as tmp:
            st, n, rows = asyncio.run(run(tmp))
        self.assertEqual(st["fail"], 50)
        self.assertEqual(n["filebase"], 50)                             # a working gateway is never skipped
        self.assertEqual(n["own"], ftm.SOURCE_DEAD_AFTER)               # the never-ok source: 30 tries, then skipped
        from tape.pumplive.meta import IPFS_GATEWAYS
        self.assertEqual(rows["M49"]["errs"], ["504"] * len(IPFS_GATEWAYS) + ["skip_source"])

class TestOrderedSources(unittest.TestCase):
    def test_least_busy_gateway_first(self):
        import fetch_token_meta as ftm
        gws = ["https://a.gw/ipfs/", "https://b.gw/ipfs/"]
        lim = {"a.gw": ftm.HostLimiter(), "b.gw": ftm.HostLimiter()}
        lim["a.gw"].next_t, lim["b.gw"].next_t = 10.0, 5.0
        self.assertEqual(ftm.ordered_sources("https://ipfs.io/ipfs/QmX", gws, lim),
                         ["https://b.gw/ipfs/QmX", "https://a.gw/ipfs/QmX"])
        self.assertEqual(ftm.ordered_sources("https://own.example/ipfs/QmX", gws, {}),
                         ["https://a.gw/ipfs/QmX", "https://b.gw/ipfs/QmX", "https://own.example/ipfs/QmX"])
        self.assertEqual(ftm.ordered_sources("https://meta.uxento.io/d/1.json", gws, lim),
                         ["https://meta.uxento.io/d/1.json"])


if __name__ == "__main__":
    unittest.main()
