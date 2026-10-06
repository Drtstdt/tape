#!/usr/bin/env python3
"""D152 diagnostic: which IPFS gateways serve pump.fun metadata, and HOW FAST under load.

D151 left filebase as the bottleneck: ~1.5 ok/s with as many 429s (gap 100-900 ms),
4everland barely used. This tries candidate gateways on the same CIDs (taken from
das.jsonl), each gateway in two passes:
  1. 20 CIDs one at a time        -> does it serve them at all, latency
  2. 60 CIDs, 8 at a time         -> throughput (ok/s) and 429s under load
Stop fetch_token_meta.py while this runs (it would compete for the same limits).

    python scripts\\diag_gateways.py           (~3-5 min)
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_token_meta import parse_meta_json  # noqa: E402
from tape.pumplive.meta import cid_of  # noqa: E402
from tape.pumplive.net import ssl_context  # noqa: E402

CANDIDATES = [
    "https://ipfs.filebase.io/ipfs/",
    "https://4everland.io/ipfs/",
    "https://pump.mypinata.cloud/ipfs/",
    "https://gateway.lighthouse.storage/ipfs/",
    "https://ipfs.eth.aragon.network/ipfs/",
    "https://hardbin.com/ipfs/",
    "https://cf-ipfs.com/ipfs/",
    "https://ipfs.cyou/ipfs/",
    "https://flk-ipfs.xyz/ipfs/",
    "https://ipfs.decentralized-content.com/ipfs/",
    "https://storry.tv/ipfs/",
    "https://ipfs.le-space.de/ipfs/",
]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0 Safari/537.36"}


def load_cids(path: Path, n: int, seed: int = 5):
    cids = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                c = cid_of(json.loads(line).get("uri"))
            except ValueError:
                continue
            if c:
                cids.append(c)
    random.Random(seed).shuffle(cids)
    return cids[:n]


async def one(client, url, timeout):
    t = time.time()
    try:
        r = await client.get(url, timeout=timeout, headers=UA)
        ok = r.status_code == 200 and parse_meta_json(r.content)[0] == "ok"
        return str(r.status_code), ok, time.time() - t
    except Exception as e:  # noqa: BLE001
        return type(e).__name__, False, time.time() - t


async def amain(a):
    import httpx
    cids = load_cids(Path(a.meta) / "das.jsonl", 80)
    print(f"{len(cids)} CIDs from das.jsonl\n", flush=True)
    print(f"{'gateway':44s} {'seq ok':>7s} {'med s':>6s}   {'load ok':>8s} {'ok/s':>6s}  codes under load")
    rows = []
    async with httpx.AsyncClient(follow_redirects=True, verify=ssl_context()) as client:
        for g in CANDIDATES:
            seq = [await one(client, g + c, a.timeout) for c in cids[:20]]
            s_ok = sum(o for _, o, _ in seq)
            med = sorted(d for _, _, d in seq)[len(seq) // 2]
            if s_ok == 0:
                codes = collections.Counter(c for c, _, _ in seq)
                print(f"{g:44s} {s_ok:3d}/20 {med:6.2f}   {'-':>8s} {'-':>6s}  (sequential) {dict(codes.most_common(4))}",
                      flush=True)
                continue
            sem = asyncio.Semaphore(8)

            async def lim(c):
                async with sem:
                    return await one(client, g + c, a.timeout)
            t0 = time.time()
            load = await asyncio.gather(*(lim(c) for c in cids[20:80]))
            el = time.time() - t0
            l_ok = sum(o for _, o, _ in load)
            codes = collections.Counter(c for c, _, _ in load)
            rows.append((g, l_ok / el))
            print(f"{g:44s} {s_ok:3d}/20 {med:6.2f}   {l_ok:4d}/60 {l_ok / el:6.1f}  {dict(codes.most_common(4))}",
                  flush=True)
    good = [g for g, r in sorted(rows, key=lambda x: -x[1]) if r > 0.5]
    print("\nworking gateways, fastest first:", good)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--meta", default=r"E:\tape_research\token_meta")
    ap.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args(argv)
    asyncio.run(amain(a))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
