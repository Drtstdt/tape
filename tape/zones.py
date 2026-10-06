"""D123 (docs/DECISIONS.md) -- frozen chronological zones, v1.

Universe U0 (D121): mints whose pump.fun `create` event is in the store v2
(born in a collected hour) and that traded at least once. Zone membership is
a function of the CREATE TIME only -- never of anything that happens later.

Rule (fixed before any zone was computed, D122/D123):
  * candidate cut points are UTC day boundaries;
  * cut1 = the boundary whose cumulative U0 share is closest to 0.50,
    cut2 = the boundary after cut1 closest to 0.70 (shares on U0 counts per
    create day -- counts only, no outcomes);
  * embargo: tokens created in the 2 hours before a cut are dropped (a label
    horizon of 30 min plus the decision delay never crosses into the next zone);
  * tokens created on/after `end` (2026-10-01; Oct has 3 days of fragments)
    are out;
  * sealed is reported in two pre-registered parts: dense (created before
    2026-08-01, ~12-15 collected h/day) and sparse (2026-08-01 .. end,
    2-4 collected h/day, D122).

Freezing writes, per zone, a sorted mint list and its sha256 into zones.json.
`load_zone` re-hashes the list on every load and refuses a mismatch, and the
sealed zone can only be loaded through `tape.registry.open_sealed`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
ZONES = ("discovery", "validation", "sealed")
EMBARGO_MS = 2 * HOUR_MS
END_UTC = "2026-10-01"
DENSE_UNTIL_UTC = "2026-08-01"
TARGETS = (0.50, 0.70)


def utc_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def choose_cuts(day_counts: Dict[str, int], targets: Tuple[float, float] = TARGETS) -> Tuple[str, str]:
    """day_counts: 'YYYY-MM-DD' -> number of U0 tokens created that day.
    Returns (cut1_day, cut2_day): zone k+1 starts at 00:00 UTC of cut_k."""
    days = sorted(day_counts)
    if len(days) < 3:
        raise ValueError("need at least 3 days")
    total = float(sum(day_counts.values()))
    # boundary i = start of days[i]; share of tokens created before it
    cum, shares = 0.0, []
    for d in days:
        shares.append(cum / total)
        cum += day_counts[d]
    idx = list(range(1, len(days)))
    i1 = min(idx, key=lambda i: (abs(shares[i] - targets[0]), i))
    i2 = min((i for i in idx if i > i1), key=lambda i: (abs(shares[i] - targets[1]), i))
    return days[i1], days[i2]


def assign_zone(create_ms: int, cut1_ms: int, cut2_ms: int, end_ms: int) -> str:
    """'discovery' | 'validation' | 'sealed' | 'embargo' | 'out'."""
    if create_ms >= end_ms:
        return "out"
    for cut in (cut1_ms, cut2_ms):
        if cut - EMBARGO_MS <= create_ms < cut:
            return "embargo"
    if create_ms < cut1_ms:
        return "discovery"
    if create_ms < cut2_ms:
        return "validation"
    return "sealed"


def list_hash(mints: Sequence[str]) -> str:
    h = hashlib.sha256()
    for m in sorted(mints):
        h.update(m.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def write_list(path: Path, mints: Sequence[str]) -> str:
    path.write_text("".join(m + "\n" for m in sorted(mints)), encoding="utf-8")
    return list_hash(mints)


def read_list(path: Path) -> List[str]:
    return [l for l in Path(path).read_text(encoding="utf-8").splitlines() if l]


class ZoneIntegrityError(RuntimeError):
    pass


def load_zone(zones_dir, zone: str, _sealed_token: object = None) -> List[str]:
    """Load a frozen zone's mint list, verifying its sha256 against zones.json.
    The sealed zone needs the token issued by registry.open_sealed()."""
    zones_dir = Path(zones_dir)
    meta = json.loads((zones_dir / "zones.json").read_text(encoding="utf-8"))
    names = [z for z in meta["zones"]]
    if zone not in names:
        raise KeyError(f"unknown zone {zone!r}; frozen zones: {names}")
    if zone.startswith("sealed") and _sealed_token is not _SEALED_TOKEN:
        raise PermissionError("the sealed zone is opened exactly once, via tape.registry.open_sealed()")
    mints = read_list(zones_dir / meta["zones"][zone]["file"])
    got = list_hash(mints)
    if got != meta["zones"][zone]["sha256"]:
        raise ZoneIntegrityError(f"{zone}: sha256 {got[:16]} != frozen {meta['zones'][zone]['sha256'][:16]}")
    return mints


_SEALED_TOKEN = object()
