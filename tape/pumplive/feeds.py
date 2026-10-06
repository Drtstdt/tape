"""Network side: PumpPortal (free new-token + migration stream) and Helius
logsSubscribe per bonding curve (paid in credits: ~2 credits per 0.1 MB).
Needs `websockets` and `httpx` (both in requirements.txt)."""

from __future__ import annotations

import asyncio
import json
import time

from .net import ssl_context

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
HELIUS_WS = "wss://mainnet.helius-rpc.com/?api-key={key}"


async def pumpportal(engine, on_new, log, stop: asyncio.Event, raw_sink=None):
    import websockets
    backoff = 1
    while not stop.is_set():
        try:
            async with websockets.connect(PUMPPORTAL_WS, ping_interval=20, max_size=2 ** 22, ssl=ssl_context()) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                log("[pumpportal] connected: subscribeNewToken + subscribeMigration")
                backoff = 1
                async for raw in ws:
                    if stop.is_set():
                        break
                    msg = json.loads(raw)
                    if raw_sink is not None:
                        raw_sink("pumpportal", msg)
                    tx = str(msg.get("txType", "")).lower()
                    if tx == "create" and msg.get("mint"):
                        tok = engine.on_create(msg)
                        if tok is not None:
                            on_new(tok)
                    elif "migrat" in tx and msg.get("mint"):
                        engine.on_migration(msg["mint"])
        except Exception as e:  # noqa: BLE001
            log(f"[pumpportal] {type(e).__name__}: {e}; reconnect in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


class HeliusLogs:
    """One websocket, many logsSubscribe(mentions=[bonding curve]) subscriptions."""

    def __init__(self, key, engine, log, raw_sink=None):
        self.url = HELIUS_WS.format(key=key)
        self.engine, self.log, self.raw_sink = engine, log, raw_sink
        self.wanted = set()            # curves we want
        self.sub_of = {}               # curve -> subscription id
        self.curve_of = {}             # subscription id -> curve
        self.pending = {}              # request id -> (curve, kind)
        self.queue = asyncio.Queue()
        self.ws = None
        self._id = 0

    def subscribe(self, curve):
        self.wanted.add(curve)
        self.queue.put_nowait(("sub", curve))

    def unsubscribe(self, curve):
        self.wanted.discard(curve)
        self.queue.put_nowait(("unsub", curve))

    async def _send(self, method, params, curve):
        self._id += 1
        self.pending[self._id] = (curve, method)
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}))

    async def _writer(self):
        while True:
            kind, curve = await self.queue.get()
            if self.ws is None:
                continue
            if kind == "sub" and curve in self.wanted and curve not in self.sub_of:
                await self._send("logsSubscribe", [{"mentions": [curve]}, {"commitment": "confirmed"}], curve)
            elif kind == "unsub" and curve in self.sub_of:
                sid = self.sub_of.pop(curve)
                self.curve_of.pop(sid, None)
                await self._send("logsUnsubscribe", [sid], curve)

    async def run(self, stop: asyncio.Event):
        import websockets
        backoff = 1
        writer = asyncio.create_task(self._writer())
        try:
            while not stop.is_set():
                try:
                    async with websockets.connect(self.url, ping_interval=30, max_size=2 ** 24,
                                                  ssl=ssl_context()) as ws:
                        self.ws, self.sub_of, self.curve_of, self.pending = ws, {}, {}, {}
                        self.log(f"[helius] connected; (re)subscribing {len(self.wanted)} curves")
                        for c in list(self.wanted):
                            self.queue.put_nowait(("sub", c))
                        backoff = 1
                        async for raw in ws:
                            if stop.is_set():
                                break
                            self._handle(json.loads(raw))
                except Exception as e:  # noqa: BLE001
                    self.ws = None
                    self.log(f"[helius] {type(e).__name__}: {e}; reconnect in {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
        finally:
            writer.cancel()

    def _handle(self, msg):
        if "id" in msg and msg.get("id") in self.pending:
            curve, method = self.pending.pop(msg["id"])
            if method == "logsSubscribe" and "result" in msg:
                if curve in self.wanted:
                    self.sub_of[curve], self.curve_of[msg["result"]] = msg["result"], curve
                else:                                    # dropped while the request was in flight
                    self.queue.put_nowait(("unsub", curve))
                    self.sub_of[curve], self.curve_of[msg["result"]] = msg["result"], curve
            elif "error" in msg:
                self.log(f"[helius] {method} error for {curve}: {msg['error']}")
            return
        if msg.get("method") != "logsNotification":
            return
        p = msg.get("params", {})
        curve = self.curve_of.get(p.get("subscription"))
        res = p.get("result", {})
        val = res.get("value", {})
        if self.raw_sink is not None:
            self.raw_sink("helius", {"curve": curve, "slot": res.get("context", {}).get("slot"), **val})
        if curve:
            self.engine.on_logs(curve, int(res.get("context", {}).get("slot", 0)), val.get("signature", ""),
                                val.get("logs") or [], val.get("err"))


async def meta_worker(engine, queue: asyncio.Queue, log):
    import httpx
    from .meta import fetch_meta
    async with httpx.AsyncClient(follow_redirects=True, verify=ssl_context()) as client:
        while True:
            tok = await queue.get()
            try:
                meta = await fetch_meta(client, tok.uri) if tok.uri else None
            except Exception:  # noqa: BLE001
                meta = None
            engine.on_meta(tok.mint, meta)


async def clock(engine, recorder, log, stop: asyncio.Event, report_every_s=60, out_path=None):
    last = 0
    while not stop.is_set():
        await asyncio.sleep(1)
        engine.on_clock()
        if time.time() - last >= report_every_s:
            last = time.time()
            rep = engine.report()
            c = rep["counts"]
            top = rep["top"][0] if rep["top"] else None
            log(f"[report] creates {c['creates']:,} rail-rejected {c['rail_rejects']:,} watched {rep['watched_now']} "
                f"trades {c['trades']:,} decisions {c['decisions']:,} (rejected {c['decision_rejects']:,}) "
                f"closed {c['closed']:,} feed lag {c['avg_feed_lag_s']} s | champion {rep['champion']}"
                + (f" | best {top['cid']} n={top['n']} mean {top['mean']:+.4f} lcb {top['lcb']:+.4f}" if top else ""))
            if out_path:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(rep, f, indent=1, default=str)
            recorder.flush()
