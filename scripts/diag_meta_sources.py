#!/usr/bin/env python3
"""D148 diagnostic: WHY the metadata probe got only 33% (fetch_token_meta.py --limit 300).

Takes the probe's tokens from E:\\tape_research\\token_meta (das.jsonl, ipfs.jsonl) and
tries every source separately on the same tokens, recording the HTTP status /
exception and the latency -- evidence for which source to use, not a guess:
  uri       the token's own uri (as the probe did first)
  ipfs.io / dweb.link / gateway.pinata.cloud / w3s.link / ipfs.filebase.io / nftstorage.link / 4everland
            the uri's CID on each public gateway (IPFS uris only)
  pump_api  pump.fun's own coin record (frontend-api-v3.pump.fun/coins/<mint>), which keeps the
            socials pump.fun read at create -- also for tokens whose uri host is gone
For tokens whose JSON the probe DID get, it also checks that pump_api's socials
equal the JSON's (is pump_api a faithful, point-in-time copy?).

    python scripts\\diag_meta_sources.py            (~2-4 min)
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_token_meta import done_mints, parse_meta_json  # noqa: E402
from tape.pumplive.meta import normalise_link  # noqa: E402
from tape.pumplive.net import ssl_context  # noqa: E402

GATEWAYS = {"ipfs.io": "https://ipfs.io/ipfs/", "dweb.link": "https://dweb.link/ipfs/",
            "pinata": "https://gateway.pinata.cloud/ipfs/", "w3s.link": "https://w3s.link/ipfs/",
            "filebase": "https://ipfs.filebase.io/ipfs/", "nftstorage": "https://nftstorage.link/ipfs/",
            "4everland": "https://4everland.io/ipfs/"}
PUMP_API = "https://frontend-api-v3.pump.fun/coins/{mint}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0 Safari/537.36", "Accept": "application/json"}


def cid_of(uri):
    m = re.search(r"/ipfs/([A-Za-z0-9]+)", uri or "")
    return m.group(1) if m else None


async def try_url(client, url, timeout):
    t = time.time()
    try:
        r = await client.get(url, timeout=timeout, headers=UA)
        code = r.status_code
        st, fields = parse_meta_json(r.content) if code == 200 else ("http", {})
        return {"code": code, "ok": st == "ok", "fields": fields, "ms": int((time.time() - t) * 1000),
                "ctype": r.headers.get("content-type", "")[:40]}
    except Exception as e:  # noqa: BLE001
        return {"code": type(e).__name__, "ok": False, "fields": {}, "ms": int((time.time() - t) * 1000)}


async def amain(a):
    import httpx
    meta = Path(a.meta)
    das = done_mints(meta / "das.jsonl")
    ipfs = done_mints(meta / "ipfs.jsonl")
    rng = random.Random(3)
    failed = [m for m, r in ipfs.items() if r["status"] == "fail"]
    okd = [m for m, r in ipfs.items() if r["status"] == "ok"]
    f_ipfs = [m for m in failed if cid_of(das[m]["uri"])]
    f_other = [m for m in failed if not cid_of(das[m]["uri"])]
    sample_ipfs = rng.sample(f_ipfs, min(a.n, len(f_ipfs)))
    sample_other = rng.sample(f_other, min(a.n, len(f_other)))
    sample_ok = rng.sample(okd, min(a.n, len(okd)))
    print(f"probe tokens: {len(ipfs)}; failed {len(failed)} (IPFS uri {len(f_ipfs)}, other hosts {len(f_other)}); "
          f"ok {len(okd)}. Testing {len(sample_ipfs)} + {len(sample_other)} + {len(sample_ok)} tokens.", flush=True)
    host = collections.Counter(re.sub(r"^https?://", "", das[m]["uri"]).split("/")[0] for m in failed)
    print("failed tokens by uri host:", dict(host.most_common(12)), flush=True)

    sem = asyncio.Semaphore(a.conc)
    res = collections.defaultdict(list)

    async def run(src, group, m, url):
        async with sem:
            r = await try_url(client, url, a.timeout)
        res[(src, group)].append((m, r))

    t0 = time.time()
    async with httpx.AsyncClient(follow_redirects=True, verify=ssl_context()) as client:
        jobs = []
        for m in sample_ipfs:
            jobs.append(run("uri", "failed_ipfs", m, das[m]["uri"]))
            for g, base in GATEWAYS.items():
                jobs.append(run(g, "failed_ipfs", m, base + cid_of(das[m]["uri"])))
            jobs.append(run("pump_api", "failed_ipfs", m, PUMP_API.format(mint=m)))
        for m in sample_other:
            jobs.append(run("uri", "failed_other", m, das[m]["uri"]))
            jobs.append(run("pump_api", "failed_other", m, PUMP_API.format(mint=m)))
        for m in sample_ok:
            jobs.append(run("pump_api", "ok_compare", m, PUMP_API.format(mint=m)))
        await asyncio.gather(*jobs)
    print(f"\n{len(jobs)} requests in {time.time() - t0:.0f}s\n", flush=True)
    print(f"{'source':12s} {'group':14s} {'ok':>7s}  median ms  status codes / errors")
    for (src, grp), rows in sorted(res.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        ok = sum(r["ok"] for _, r in rows)
        ms = sorted(r["ms"] for _, r in rows)[len(rows) // 2]
        codes = collections.Counter(str(r["code"]) for _, r in rows)
        print(f"{src:12s} {grp:14s} {ok:3d}/{len(rows):<3d}  {ms:8d}   {dict(codes.most_common(5))}")
    # pump_api vs the JSON the probe fetched
    same = diff = miss = 0
    ex = []
    for m, r in res[("pump_api", "ok_compare")]:
        if not r["ok"]:
            miss += 1
            continue
        a_ = {k: normalise_link(ipfs[m].get(k)) for k in ("twitter", "telegram", "website")}
        b_ = {k: normalise_link(r["fields"].get(k)) for k in ("twitter", "telegram", "website")}
        if a_ == b_:
            same += 1
        else:
            diff += 1
            ex.append((m, a_, b_))
    print(f"\npump_api vs probe JSON on tokens the probe fetched: same socials {same}, different {diff}, "
          f"pump_api failed {miss}")
    for m, x, y in ex[:5]:
        print(f"   {m}: json {x} | pump_api {y}")
    out = Path(a.meta) / "diag_sources.json"
    out.write_text(json.dumps({f"{s}|{g}": [(m, {k: v for k, v in r.items() if k != 'fields'}) for m, r in rows]
                               for (s, g), rows in res.items()}, indent=0), encoding="utf-8")
    print(f"details -> {out}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--meta", default=r"E:\tape_research\token_meta")
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--conc", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=15.0)
    a = ap.parse_args(argv)
    asyncio.run(amain(a))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
