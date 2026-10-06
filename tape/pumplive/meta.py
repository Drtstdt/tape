"""Token metadata (the create's `uri` JSON: name, symbol, description, image,
twitter, telegram, website) and social-link normalisation.

No X/Twitter API is used (paid): a 'background check' here means presence,
link type and REUSE -- the same handle/site re-used across many fresh tokens
is the copycat / serial-launch pattern (copycats graduate ~10x less often,
arXiv 2609.10246)."""

from __future__ import annotations

import re
from typing import Dict, Optional

# D148 evidence (diag_meta_sources.py, 2026-10-06, 25 pump.fun CIDs each): ipfs.filebase.io 24/25 ok
# (~220 ms), 4everland.io 18/25; ipfs.io, dweb.link, w3s.link, nftstorage.link all 403 and
# gateway.pinata.cloud 429 for every request; cloudflare-ipfs.com was shut down in 2024.
# D153 (diag_gateways.py, 80 CIDs): pump.mypinata.cloud 8.1 ok/s under load (403 for some CIDs),
# filebase 4.9 ok/s, 4everland 3.8 ok/s; 9 other public gateways served nothing (402/5xx/no connection)
IPFS_GATEWAYS = ("https://pump.mypinata.cloud/ipfs/", "https://ipfs.filebase.io/ipfs/", "https://4everland.io/ipfs/")
# measured 0/25 with 403 or 429 on every request (D148); D151: queueing on ipfs.io stalled the fetch
REFUSING_GATEWAYS = frozenset({"ipfs.io", "dweb.link", "w3s.link", "nftstorage.link", "gateway.pinata.cloud",
                               "cloudflare-ipfs.com"})


def normalise_link(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^(www\.|mobile\.)", "", u)
    u = u.replace("twitter.com/", "x.com/").rstrip("/")
    u = u.split("?")[0].split("#")[0]
    return u or None


def twitter_kind(url: Optional[str]) -> str:
    """'none' | 'profile' | 'tweet' | 'community' | 'search' | 'other'."""
    n = normalise_link(url)
    if not n:
        return "none"
    if not n.startswith("x.com/"):
        return "other"
    path = n[len("x.com/"):]
    if path.startswith("i/communities"):
        return "community"
    if "/status/" in path:
        return "tweet"
    if path.startswith(("search", "hashtag", "i/")):
        return "search"
    return "profile" if re.fullmatch(r"[a-z0-9_]{1,15}", path) else "other"


def twitter_handle(url: Optional[str]) -> Optional[str]:
    n = normalise_link(url)
    if not n or not n.startswith("x.com/"):
        return None
    h = n[len("x.com/"):].split("/")[0]
    return h if re.fullmatch(r"[a-z0-9_]{1,15}", h) and h not in ("i", "search", "hashtag") else None


def socials(meta: Optional[Dict]) -> Dict[str, Optional[str]]:
    m = meta or {}
    return {"twitter": normalise_link(m.get("twitter")), "telegram": normalise_link(m.get("telegram")),
            "website": normalise_link(m.get("website"))}


_CID = re.compile(r"(?:/ipfs/|^ipfs://)([A-Za-z0-9]+)")


def cid_of(uri: Optional[str]) -> Optional[str]:
    """IPFS content id of an https gateway uri (.../ipfs/<cid>) or an ipfs://<cid> uri."""
    m = _CID.search(uri or "")
    return m.group(1) if m else None


def gateway_urls(uri: str):
    """IPFS uri: the CID on the working gateways, then the uri itself only if its host
    is not a gateway known to refuse (e.g. a project's own dedicated gateway).
    Any other uri (launcher-tool hosts): the uri itself only."""
    cid = cid_of(uri)
    if cid:
        for g in IPFS_GATEWAYS:
            yield g + cid
    if uri and uri.lower().startswith(("http://", "https://")):
        host = re.sub(r"^https?://", "", uri.lower()).split("/")[0]
        if not (cid and host in REFUSING_GATEWAYS):       # never ask a gateway that refuses (D148/D151)
            yield uri


async def fetch_meta(client, uri: str, timeout: float = 4.0) -> Optional[Dict]:
    for u in gateway_urls(uri):
        try:
            r = await client.get(u, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            continue
    return None
