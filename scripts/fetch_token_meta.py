#!/usr/bin/env python3
"""D147: token metadata for the family-R replay, stage 2 (socials / Twitter / copycat rails).

The store has no name/symbol/uri (D145), so for every token of the replay
universe (E:\\tape_research\\replay_R\\discovery\\part-b*.parquet):
  1. Helius DAS getAssetBatch (1000 mints per call, 10 credits per call ->
     ~3,400 credits for ~336k tokens): on-chain name, symbol, json_uri;
  2. the uri's JSON (pump.fun: twitter, telegram, website) from the uri itself,
     then the same CID on public IPFS gateways (tape/pumplive/meta.py).
The JSON is content-addressed (IPFS CID fixed at create), so its content is
point-in-time; only its AVAILABILITY today can differ from then, so fetch
failures are recorded as 'fail' and kept apart from a real 'no_uri'.

Caches (append-only jsonl, resumable -- a rerun skips mints already done):
  <out>\\das.jsonl    {"mint","status":ok|missing|error,"uri","name","symbol"}
  <out>\\ipfs.jsonl   {"mint","status":ok|fail|bad_json|no_uri,"twitter","telegram","website","gw","ms"}

    python scripts\\fetch_token_meta.py --limit 300      (probe: random sample, prints rates and full-run ETA)
    python scripts\\fetch_token_meta.py                  (all; resumable)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape.env import load_project_dotenv  # noqa: E402
from tape.pumplive.meta import IPFS_GATEWAYS, cid_of, gateway_urls  # noqa: E402
from tape.pumplive.net import ssl_context  # noqa: E402

DAS_BATCH = 1000
MAX_BYTES = 200_000
KEEP = ("twitter", "telegram", "website")


def done_mints(path: Path) -> dict:
    out = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    out[r["mint"]] = r
                except (ValueError, KeyError):
                    continue                     # a torn last line after a crash
    return out


def parse_das(asset) -> dict:
    """One getAssetBatch item -> cache row (pure, tested)."""
    if not asset:
        return {"status": "missing", "uri": None, "name": None, "symbol": None}
    c = asset.get("content") or {}
    md = c.get("metadata") or {}
    uri = (c.get("json_uri") or "").strip() or None
    return {"status": "ok", "uri": uri, "name": md.get("name"), "symbol": md.get("symbol")}


def parse_meta_json(body: bytes):
    """JSON body -> (status, fields) (pure, tested)."""
    if len(body) > MAX_BYTES:
        return "bad_json", {}
    try:
        j = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return "bad_json", {}
    if not isinstance(j, dict):
        return "bad_json", {}
    return "ok", {k: (str(j[k])[:300] if j.get(k) not in (None, "") else None) for k in KEEP}


async def das_phase(client, key, mints, path, log, rps=2.0):
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    done = done_mints(path)          # read ONCE (D150: re-reading it per mint made a resumed run hang)
    todo = [m for m in mints if m not in done]
    batches = [todo[i:i + DAS_BATCH] for i in range(0, len(todo), DAS_BATCH)]
    log(f"  DAS: {len(mints) - len(todo):,} cached, {len(todo):,} to fetch in {len(batches)} calls "
        f"(~{10 * len(batches):,} credits)")
    t0 = time.time()
    with open(path, "a", encoding="utf-8") as f:
        for i, b in enumerate(batches, start=1):
            for attempt in range(6):
                try:
                    r = await client.post(url, json={"jsonrpc": "2.0", "id": "r", "method": "getAssetBatch",
                                                     "params": {"ids": b}}, timeout=60)
                    if r.status_code == 429 or r.status_code >= 500:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    r.raise_for_status()
                    res = r.json().get("result")
                    if not isinstance(res, list) or len(res) != len(b):
                        raise RuntimeError(f"unexpected result: {str(r.json())[:200]}")
                    break
                except Exception as e:  # noqa: BLE001
                    wait = 2 ** attempt
                    log(f"    DAS call {i}: {type(e).__name__}: {str(e)[:120]}; retry in {wait}s")
                    await asyncio.sleep(wait)
            else:
                for m in b:
                    f.write(json.dumps({"mint": m, "status": "error", "uri": None, "name": None, "symbol": None}) + "\n")
                continue
            by_id = {a.get("id"): a for a in res if a}
            for m in b:
                f.write(json.dumps({"mint": m, **parse_das(by_id.get(m))}) + "\n")
            f.flush()
            el = time.time() - t0
            if i % 10 == 0 or i == len(batches):
                log(f"    DAS {i}/{len(batches)} elapsed={el:.0f}s ETA~{el / i * (len(batches) - i):.0f}s")
            await asyncio.sleep(1.0 / rps)


HOST_DEAD_AFTER = 25          # a non-IPFS host with this many failures and no success is skipped
SOURCE_DEAD_AFTER = 30        # a source host with this many errors and no success is skipped (D151)
RETRIES_429 = 4               # a 429 is retried on the SAME source after the limiter backs off (D149)


def host_of(uri: str) -> str:
    return re.sub(r"^https?://", "", uri or "").split("/")[0].lower()


class HostLimiter:
    """Adaptive spacing of requests to ONE host (AIMD): every 429 widens the gap
    between requests (x1.5 + 50 ms, max 2 s), every 200 narrows it (x0.9, min
    `floor`). D149: at a fixed concurrency of 32, filebase answered 429 to ~56% of
    IPFS tokens; the gateways behind it time out or refuse as well."""

    def __init__(self, interval=0.05, floor=0.01, ceil=2.0):
        self.interval, self.floor, self.ceil = interval, floor, ceil
        self.next_t = 0.0
        self.n429 = 0
        self.nok = 0
        self.last_backoff = -1e9
        self.nbad = 0

    async def wait(self):
        now = time.monotonic()
        delay = self.next_t - now
        self.next_t = max(now, self.next_t) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)

    def on_429(self):
        """Back off at most once per second: a burst of concurrent requests that all get
        429 is ONE signal, not 16 (otherwise the gap jumps to the ceiling and the rate
        collapses -- seen in an offline simulation, D150)."""
        self.n429 += 1
        now = time.monotonic()
        if now - self.last_backoff >= 1.0:
            self.last_backoff = now
            self.interval = min(self.ceil, self.interval * 1.5 + 0.05)

    def on_ok(self):
        self.nok += 1
        self.interval = max(self.floor, self.interval * 0.9)


def ordered_sources(uri, gateways, lim):
    """D152: the CID on EVERY working gateway, the least busy first (earliest free slot of
    its limiter), so the load splits by each gateway's measured capacity instead of
    piling on the first one; then the uri itself unless it is a refusing gateway."""
    cid = cid_of(uri)
    out = []
    if cid:
        def key(g):
            L = lim.get(host_of(g))
            return (L.next_t, L.interval) if L else (0.0, 0.0)
        out = [g + cid for g in sorted(gateways, key=key)]
    out += [u for u in gateway_urls(uri) if not any(u.startswith(g) for g in IPFS_GATEWAYS)]
    return out


async def ipfs_phase(client, das, mints, path, log, conc=16, timeout=8.0, retry_failed=True, gateways=None):
    gateways = list(gateways or IPFS_GATEWAYS)
    gw_hosts = {host_of(g) for g in gateways}
    done = done_mints(path)
    todo = [m for m in mints if m not in done or (retry_failed and done[m].get("status") == "fail")]
    hosts = {}                   # host -> [ok, fail] for the dead-host breaker (non-IPFS uris only)
    lim = {}                     # host -> HostLimiter
    log(f"  IPFS: {len(mints) - len(todo):,} cached, {len(todo):,} to fetch (concurrency {conc})")
    sem = asyncio.Semaphore(conc)
    st = {"n": 0, "ok": 0, "fail": 0, "bad_json": 0, "no_uri": 0, "gw": {}, "ms": 0}
    t0 = time.time()
    last = [t0]
    f = open(path, "a", encoding="utf-8")

    async def get(u, errs):
        """One source with retries on 429; returns the response or None."""
        h = host_of(u)
        L = lim.setdefault(h, HostLimiter())
        breaker = h not in gw_hosts                     # the working gateways are only ever slowed, never skipped
        if breaker and L.nok == 0 and L.nbad >= SOURCE_DEAD_AFTER:
            errs.append("skip_source")               # D151: never succeeded -> do not queue on it
            return None
        for _ in range(RETRIES_429):
            await L.wait()
            try:
                r = await client.get(u, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                errs.append(type(e).__name__)
                L.nbad += 1
                return None
            if r.status_code == 429:
                errs.append("429")
                L.on_429()
                L.nbad += 1
                if breaker and L.nok == 0 and L.nbad >= SOURCE_DEAD_AFTER:
                    return None
                continue
            if r.status_code != 200:
                errs.append(str(r.status_code))
                L.nbad += 1
                return None
            L.on_ok()
            return r
        return None

    async def one(m):
        uri = (das.get(m) or {}).get("uri")
        row = {"mint": m, "status": "no_uri", "gw": None, "ms": 0, **{k: None for k in KEEP}}
        is_ipfs = cid_of(uri) is not None
        h = host_of(uri)
        hs = hosts.setdefault(h, [0, 0])
        if uri and not is_ipfs and hs[0] == 0 and hs[1] >= HOST_DEAD_AFTER:
            row.update(status="fail", errs=["host_dead"])
        elif uri:
            row["status"] = "fail"
            async with sem:
                t1 = time.time()
                errs = []
                for u in ordered_sources(uri, gateways, lim):
                    r = await get(u, errs)
                    if r is None:
                        continue
                    status, fields = parse_meta_json(r.content)
                    row.update(fields, status=status, gw=host_of(u))       # which source served it
                    if status == "ok":
                        break
                row["ms"] = int((time.time() - t1) * 1000)
                row["errs"] = errs                         # per attempt: exception name / HTTP code
            if not is_ipfs:
                hs[0 if row["status"] == "ok" else 1] += 1
        f.write(json.dumps(row) + "\n")
        st["n"] += 1
        st[row["status"]] += 1
        st["ms"] += row["ms"]
        if row["gw"] is not None:
            st["gw"][row["gw"]] = st["gw"].get(row["gw"], 0) + 1
        now = time.time()
        if now - last[0] > 20 or st["n"] == len(todo):
            last[0] = now
            f.flush()
            el = now - t0
            busy = sorted(lim.items(), key=lambda kv: -(kv[1].nok + kv[1].n429))[:4]
            log(f"    IPFS {st['n']:,}/{len(todo):,} ok={st['ok']:,} fail={st['fail']:,} bad={st['bad_json']:,} "
                f"no_uri={st['no_uri']:,} by gateway {dict(sorted(st['gw'].items(), key=lambda kv: -kv[1])[:4])} "
                f"rate={st['n'] / el:.1f}/s elapsed={el:.0f}s ETA~{el / st['n'] * (len(todo) - st['n']):.0f}s | "
                + " ".join(f"{h}: ok {L.nok:,} 429 {L.n429:,} gap {L.interval * 1000:.0f}ms" for h, L in busy))

    try:
        CH = 5000
        for i in range(0, len(todo), CH):
            await asyncio.gather(*(one(m) for m in todo[i:i + CH]))
    finally:
        f.close()
    return st, time.time() - t0


async def amain(a) -> int:
    import httpx
    import pandas as pd

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    lf = open(out / "fetch_log.txt", "a", encoding="utf-8")

    def log(m):
        line = time.strftime("%H:%M:%S ") + m
        print(line, flush=True)
        lf.write(line + "\n")
        lf.flush()

    load_project_dotenv()
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        log("STOP: HELIUS_API_KEY missing in .env")
        return 2
    parts = sorted(Path(a.replay).glob("part-b*.parquet"))
    if not parts:
        log(f"STOP: no replay parts in {a.replay} -- run replay_pumplive.py first")
        return 4
    log(f"  reading {len(parts)} replay parts ...")
    rcols = None
    frames = []
    for p in parts:
        if rcols is None:
            import pyarrow.parquet as pq
            rcols = [c for c in pq.read_schema(p).names if c.startswith("r_")]
        frames.append(pd.read_parquet(p, columns=["mint", "create_ts"] + rcols))
    d = pd.concat(frames, ignore_index=True)
    d["kept"] = ~d[rcols].any(axis=1)
    toks = d.groupby("mint", as_index=False).agg(create_ts=("create_ts", "min"), kept=("kept", "any"))
    # D150: tokens with a decision kept by the stage-1 rails FIRST (the only ones R003 can trade),
    # then the rest (they only feed the link/name reuse index) -- stopping early still leaves R003 runnable
    toks = toks.sort_values(["kept", "create_ts"], ascending=[False, True])
    n_kept = int(toks["kept"].sum())
    mints = toks["mint"].tolist()
    if a.only_kept:
        mints = mints[:n_kept]
    log(f"  tokens {len(toks):,}; with a stage-1-kept decision {n_kept:,} (fetched first)"
        + (" -- --only-kept: the rest is skipped" if a.only_kept else ""))
    if a.limit:
        mints = random.Random(a.seed).sample(mints, min(a.limit, len(mints)))
    log(f"=== fetch_token_meta: {len(mints):,} tokens{' (probe sample)' if a.limit else ''} ===")
    async with httpx.AsyncClient(follow_redirects=True, verify=ssl_context(),
                                 limits=httpx.Limits(max_connections=a.conc + 4)) as client:
        log("  loading the DAS cache ...")
        await das_phase(client, key, mints, out / "das.jsonl", log)
        das = done_mints(out / "das.jsonl")
        cov = [das.get(m, {}) for m in mints]
        log(f"  DAS coverage: ok {sum(1 for r in cov if r.get('status') == 'ok'):,}, with uri "
            f"{sum(1 for r in cov if r.get('uri')):,}, missing {sum(1 for r in cov if r.get('status') == 'missing'):,}, "
            f"error {sum(1 for r in cov if r.get('status') == 'error'):,}")
        gws = [g.strip() for g in a.gateways.split(",") if g.strip()] if a.gateways else None
        if gws:
            log(f"  gateways: {gws}")
        st, el = await ipfs_phase(client, das, mints, out / "ipfs.jsonl", log, conc=a.conc, timeout=a.timeout,
                                  gateways=gws)
    if a.limit and st["n"]:
        full = len(toks)
        allr = done_mints(out / "ipfs.jsonl")
        n_ok = sum(1 for m in mints if (allr.get(m) or {}).get("status") == "ok")
        log(f"  PROBE: sample ok {n_ok}/{len(mints)} = {n_ok / len(mints):.1%} (this run, retried tokens only: "
            f"ok {st['ok'] / st['n']:.1%}, fail {st['fail'] / st['n']:.1%}); "
            f"mean {st['ms'] / max(st['n'], 1):.0f} ms per token; full run of {full:,} tokens at "
            f"{st['n'] / el:.1f}/s ~ {full / (st['n'] / el) / 3600:.1f} h (DAS ~{full / DAS_BATCH / 2 / 60:.0f} min)")
    log("  done")
    lf.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--replay", default=r"E:\tape_research\replay_R\discovery")
    ap.add_argument("--out", default=r"E:\tape_research\token_meta")
    ap.add_argument("--limit", type=int, default=0, help="probe: random sample of N tokens")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--gateways", default="", help="comma-separated IPFS gateway prefixes (default: meta.IPFS_GATEWAYS)")
    ap.add_argument("--only-kept", action="store_true", help="only tokens with a stage-1-kept decision")
    ap.add_argument("--conc", type=int, default=16)
    ap.add_argument("--timeout", type=float, default=8.0)
    a = ap.parse_args(argv)
    return asyncio.run(amain(a))


if __name__ == "__main__":
    raise SystemExit(main())
