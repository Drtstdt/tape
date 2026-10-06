"""D133: replication of already-registered trials on a corrected research set.

The research set v2 merged distinct same-amount swaps of one transaction
(dedup key without the reserve, D133). The set is rebuilt as v3 and families
A/C/E are re-run on it with their SPECS UNCHANGED. A replication:
  * registers NO new hypothesis trials (the searched space did not grow);
  * maps every result onto the ORIGINAL trial id (same spec, set aside);
  * recomputes BH q over the whole registry with the replicated p-values in
    place of the originals, and stores them on the original trials, so every
    later BH correction (families F, H) uses the corrected p-values;
  * records one diagnostic ('T') with the per-trial outcome, and updates each
    original trial with `replication_v3` and -- per the rule fixed in D133
    before any v3 number is seen -- `status` = the v3 verdict (the corrected
    data is authoritative; both verdicts stay in the history).
Running the same replication twice is refused.
"""

from __future__ import annotations

from typing import Dict

from .registry import Registry, RegistryError, bh, spec_hash

V3 = "sets_v3"


def set_name_of(path) -> str:
    """'E:\\tape_research\\sets_v3\\discovery' -> 'sets_v3' (any OS)."""
    for part in str(path).replace("\\", "/").split("/"):
        if part.startswith("sets_"):
            return part
    return "?"


def original_ids(reg: Registry, family: str, key: str, set_name: str = "sets_v2") -> Dict[str, str]:
    """{spec[key]: trial_id} for the family's registered (non-replication) trials."""
    out: Dict[str, str] = {}
    for tid, t in sorted(reg.trials().items()):
        sp = t.get("spec") or {}
        if t["family"] == family and sp.get("set") == set_name and key in sp:
            out.setdefault(str(sp[key]), tid)
    return out


def replication_spec(family: str, set_name: str = V3) -> dict:
    return {"replication_of_family": family, "set": set_name, "decision": "D133"}


def already_replicated(reg: Registry, family: str, set_name: str = V3) -> bool:
    h = spec_hash(replication_spec(family, set_name))
    return any(t.get("spec_hash") == h for t in reg.trials().values())


def replicated_q(reg: Registry, pvals_by_tid: Dict[str, float]) -> Dict[str, float]:
    """BH over every non-diagnostic trial with a p-value, the replicated
    p-values replacing the originals (same m as the original correction)."""
    p = {tid: t["p_value"] for tid, t in reg.trials().items()
         if t["family"] != "T" and t.get("p_value") is not None}
    missing = set(pvals_by_tid) - set(p)
    if missing:
        raise RegistryError(f"replication of unregistered trials: {sorted(missing)}")
    p.update(pvals_by_tid)
    return bh(p)


def record(reg: Registry, family: str, per_trial: Dict[str, dict], summary: dict,
           set_name: str = V3) -> str:
    """per_trial: {original_tid: {'status': 'passed_discovery'|'screened_out',
    'p_value': float|None, 'q_bh': float|None, ...metrics}}."""
    trials = reg.trials()
    changed = {tid: (trials[tid].get("status"), r["status"]) for tid, r in per_trial.items()
               if trials[tid].get("status") != r["status"]}
    tid_t = reg.register("T", f"{family} replication on {set_name} (D133)",
                         "same specs, corrected dedup: conclusions unchanged?",
                         replication_spec(family, set_name), zone="discovery", status="diagnostic",
                         metrics={"summary": summary, "status_changes": changed,
                                  "per_trial": {k: {kk: v for kk, v in r.items()
                                                    if kk in ("status", "p_value", "q_bh")}
                                                for k, r in per_trial.items()}})
    for tid, r in per_trial.items():
        extra = {"p_value": r["p_value"]} if r.get("p_value") is not None else {}
        reg.update(tid, status=r["status"], replication_v3={**r, "diagnostic": tid_t}, **extra,
                   notes=(f"status from {set_name} replication ({tid_t}); was {trials[tid].get('status')}"
                          if tid in changed else f"replicated on {set_name} ({tid_t}), verdict unchanged"))
    return tid_t
