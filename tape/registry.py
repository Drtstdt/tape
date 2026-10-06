"""D123 (docs/DECISIONS.md) -- the trial registry: every hypothesis / feature /
model / rule ever evaluated is recorded, including failures, so multiple-
testing corrections cover the WHOLE searched space (EXPLORATION_PROMPT rules
2 and 9).

Storage (append-only JSON lines, one record per event):
    <dir>/trials.jsonl       trial records and later status updates
    <dir>/candidates.json    pre-registered sealed candidates (written once)
    <dir>/sealed_access.json written the one time the sealed zone is opened

Hard rules enforced here:
  * families are fixed (A-G) with fixed trial budgets; a trial beyond its
    family budget, or in an unknown family, is refused;
  * trial ids are sequential and never reused; records are never edited --
    a status change is a new line referencing the trial id;
  * no trial may be registered on the sealed zone; the sealed zone is opened
    exactly once, by `open_sealed`, for candidates pre-registered (and hashed)
    with `preregister_candidates` -- a second opening is refused.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import zones as _zones

FAMILY_BUDGET = {
    "T": 10,   # diagnostics of the pipeline itself (not hypotheses about the market)
    "A": 20,   # baseline & tempo
    "B": 30,   # early-candle trajectories
    "C": 30,   # wallet microstructure
    "D": 40,   # interactions & rules
    "E": 10,   # gradient-boosting challenger
    "F": 24,   # exit variants
    "G": 10,   # meta-labelling
    "H": 16,   # long holds (hours), D132 (12) + D134 partial exits (4); all fixed before any H run
    "W": 8,    # wallet skill / copy-trading, D135/D136 -- the LAST pump.fun family
    "X": 4,    # D142 brute-force search (user override of the D135 stop rule): D141 candidate + top 3
    "R": 4,    # D145 replay of the live paper bot (rails + learner): R000-R002 stage 1 + 1 for stage 2 (metadata)
}
ALLOWED_ZONES = ("discovery", "validation", "discovery+validation")
STATUSES = ("registered", "screened_out", "passed_discovery", "failed_validation",
            "passed_validation", "sealed_pass", "sealed_fail", "diagnostic")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]


class RegistryError(RuntimeError):
    pass


class Registry:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "trials.jsonl"

    # -- reading ------------------------------------------------------------
    def events(self) -> List[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def trials(self) -> Dict[str, dict]:
        """trial_id -> merged record (registration + later updates, in order)."""
        res: Dict[str, dict] = {}
        for e in self.events():
            tid = e["trial_id"]
            if e["event"] == "register":
                res[tid] = dict(e)
            elif tid in res:
                res[tid].update({k: v for k, v in e.items() if k not in ("event", "trial_id")})
                res[tid].setdefault("history", []).append(e)
        return res

    def _append(self, rec: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    # -- writing ------------------------------------------------------------
    def register(self, family: str, name: str, hypothesis: str, spec: dict, zone: str,
                 status: str = "registered", metrics: Optional[dict] = None,
                 p_value: Optional[float] = None, notes: str = "") -> str:
        if family not in FAMILY_BUDGET:
            raise RegistryError(f"unknown family {family!r}; families are fixed: {sorted(FAMILY_BUDGET)}")
        if zone not in ALLOWED_ZONES:
            raise RegistryError(f"trials may only use {ALLOWED_ZONES}; the sealed zone is opened "
                                f"once via open_sealed()")
        if status not in STATUSES:
            raise RegistryError(f"unknown status {status!r}")
        tr = self.trials()
        used = sum(1 for t in tr.values() if t["family"] == family)
        if used >= FAMILY_BUDGET[family]:
            raise RegistryError(f"family {family} budget exhausted ({used}/{FAMILY_BUDGET[family]}): "
                                f"report the result, do not add trials")
        if (self.dir / "sealed_access.json").exists():
            raise RegistryError("the sealed zone has been opened; the search is closed")
        n = sum(1 for t in tr if t.startswith(family))
        tid = f"{family}{n:03d}"
        self._append({"event": "register", "trial_id": tid, "family": family, "name": name,
                      "hypothesis": hypothesis, "spec": spec, "spec_hash": spec_hash(spec),
                      "zone": zone, "status": status, "metrics": metrics or {},
                      "p_value": p_value, "notes": notes, "utc": _now()})
        return tid

    def update(self, trial_id: str, **fields) -> None:
        if trial_id not in self.trials():
            raise RegistryError(f"no trial {trial_id}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise RegistryError(f"unknown status {fields['status']!r}")
        self._append({"event": "update", "trial_id": trial_id, "utc": _now(), **fields})

    # -- multiple testing -------------------------------------------------
    def bh_qvalues(self, families: Optional[Sequence[str]] = None) -> Dict[str, float]:
        """Benjamini-Hochberg q-values over EVERY trial with a p-value (diagnostics
        'T' excluded unless asked) -- the whole searched space, not the winners."""
        fams = set(families) if families else set(FAMILY_BUDGET) - {"T"}
        items = [(t, r["p_value"]) for t, r in self.trials().items()
                 if r["family"] in fams and r.get("p_value") is not None]
        return bh(dict(items))

    def n_trials(self, exclude_diagnostics: bool = True) -> int:
        return sum(1 for r in self.trials().values() if not (exclude_diagnostics and r["family"] == "T"))

    # -- sealed --------------------------------------------------------------
    def preregister_candidates(self, candidates: List[dict]) -> str:
        """Each candidate: {'trial_id': ..., 'decision_rule': ..., 'threshold': ...,
        'metric': ..., 'success_criterion': ...}. Written once; returns its hash."""
        p = self.dir / "candidates.json"
        if p.exists():
            raise RegistryError("candidates already pre-registered (written once)")
        if not candidates or len(candidates) > 5:
            raise RegistryError("pre-register 1..5 named candidates")
        tr = self.trials()
        for c in candidates:
            if c.get("trial_id") not in tr:
                raise RegistryError(f"candidate {c.get('trial_id')} is not a registered trial")
            if tr[c["trial_id"]].get("status") != "passed_validation":
                raise RegistryError(f"candidate {c['trial_id']} has not passed validation")
        blob = json.dumps({"candidates": candidates, "utc": _now(),
                           "n_trials_total": self.n_trials()}, indent=1, sort_keys=True, default=str)
        p.write_text(blob, encoding="utf-8")
        return hashlib.sha256(blob.encode()).hexdigest()

    def open_sealed(self, zones_dir) -> Dict[str, List[str]]:
        """The one and only access to the sealed zone. Returns {'sealed_dense': [...],
        'sealed_sparse': [...]} (whichever exist)."""
        cp = self.dir / "candidates.json"
        ap = self.dir / "sealed_access.json"
        if not cp.exists():
            raise RegistryError("pre-register candidates first")
        if ap.exists():
            raise RegistryError(f"the sealed zone was already opened ({ap.read_text(encoding='utf-8')[:200]})")
        ap.write_text(json.dumps({"utc": _now(), "candidates_sha256":
                                  hashlib.sha256(cp.read_bytes()).hexdigest()}), encoding="utf-8")
        meta = json.loads((Path(zones_dir) / "zones.json").read_text(encoding="utf-8"))
        return {z: _zones.load_zone(zones_dir, z, _zones._SEALED_TOKEN)
                for z in meta["zones"] if z.startswith("sealed")}


def bh(pvals: Dict[str, float]) -> Dict[str, float]:
    """Benjamini-Hochberg adjusted p-values (q-values)."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    q, out = 1.0, {}
    for rank in range(m, 0, -1):
        k, p = items[rank - 1]
        q = min(q, p * m / rank)
        out[k] = min(q, 1.0)
    return out
