#!/usr/bin/env python3
"""D123: freeze the three chronological zones (v1) and seed the trial registry.

Runs once. Refuses to run again if zones_v1/zones.json exists (a frozen zone
is never recomputed -- that would be selecting the split after the fact).
Uses only create times and token counts (no outcomes). ~1 min.

    python scripts\\freeze_zones_v1.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tape import pf_store as ps  # noqa: E402
from tape import zones as zn  # noqa: E402
from tape.registry import Registry  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", default=r"E:\tape_store_v2")
    ap.add_argument("--out", default=r"E:\tape_research\zones_v1")
    ap.add_argument("--registry", default=r"E:\tape_research\registry")
    ap.add_argument("--facts", default=r"E:\tape_research\facts_v2\facts.json")
    a = ap.parse_args()

    import pandas as pd

    out = Path(a.out)
    if (out / "zones.json").exists():
        print(f"REFUSED: {out / 'zones.json'} exists -- zones are frozen and never recomputed.")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print(f"freeze_zones_v1 {datetime.now(timezone.utc).isoformat()}  store={a.store}", flush=True)

    ev = ps.load_events(a.store)
    cr = (ev[(ev["event_type"] == "create") & ev["ts_ms"].notna()]
          .groupby("mint", as_index=False)["ts_ms"].min().rename(columns={"ts_ms": "create_ts"}))
    mi = ps.load_mint_index(a.store)
    u0 = cr[cr["mint"].isin(set(mi["mint"]))].copy()
    u0["create_ts"] = u0["create_ts"].astype("int64")
    end_ms = zn.utc_ms(zn.END_UTC)
    print(f"  events {len(ev):,}; creates {len(cr):,}; U0 (create + >=1 swap) {len(u0):,} "
          f"({time.time() - t0:.0f}s)", flush=True)

    inside = u0[u0["create_ts"] < end_ms]
    inside_day = pd.to_datetime(inside["create_ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    day_counts = inside_day.value_counts().sort_index().to_dict()
    cut1, cut2 = zn.choose_cuts(day_counts)
    c1, c2 = zn.utc_ms(cut1), zn.utc_ms(cut2)
    u0["zone"] = [zn.assign_zone(t, c1, c2, end_ms) for t in u0["create_ts"]]
    dense_ms = zn.utc_ms(zn.DENSE_UNTIL_UTC)
    is_sealed = u0["zone"] == "sealed"
    u0.loc[is_sealed & (u0["create_ts"] < dense_ms), "zone"] = "sealed_dense"
    u0.loc[is_sealed & (u0["create_ts"] >= dense_ms), "zone"] = "sealed_sparse"
    tot = sum(day_counts.values())

    meta = {
        "version": "zones_v1",
        "frozen_utc": datetime.now(timezone.utc).isoformat(),
        "rule": ("U0 = mints with a create event in store v2 and >= 1 swap; zone by create time; "
                 "cuts at UTC day boundaries closest to cumulative U0 share 0.50 / 0.70; "
                 "embargo: created in the 2 h before a cut; created >= END excluded; "
                 "sealed split at DENSE_UNTIL into dense / sparse (both pre-registered)."),
        "params": {"targets": list(zn.TARGETS), "embargo_ms": zn.EMBARGO_MS, "end_utc": zn.END_UTC,
                   "dense_until_utc": zn.DENSE_UNTIL_UTC},
        "cuts": {"validation_starts": cut1, "sealed_starts": cut2},
        "shares_at_cuts": {
            cut1: sum(v for d, v in day_counts.items() if d < cut1) / tot,
            cut2: sum(v for d, v in day_counts.items() if d < cut2) / tot},
        "code_sha256": {"tape/zones.py": hashlib.sha256(Path(zn.__file__).read_bytes()).hexdigest()},
        "store_compacted_sha256": hashlib.sha256((Path(a.store) / "compacted.json").read_bytes()).hexdigest(),
        "counts": {z: int(n) for z, n in u0["zone"].value_counts().items()},
        "zones": {},
    }
    for z in ("discovery", "validation", "sealed_dense", "sealed_sparse"):
        sub = u0[u0["zone"] == z]
        f = out / f"mints_{z}.txt"
        h = zn.write_list(f, sub["mint"].tolist())
        os.chmod(f, stat.S_IREAD)
        meta["zones"][z] = {"file": f.name, "sha256": h, "n": int(len(sub)),
                            "first_create_utc": zn.utc_day(int(sub["create_ts"].min())) if len(sub) else None,
                            "last_create_utc": zn.utc_day(int(sub["create_ts"].max())) if len(sub) else None}
    zj = out / "zones.json"
    zj.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.chmod(zj, stat.S_IREAD)
    zsha = hashlib.sha256(zj.read_bytes()).hexdigest()

    print(f"\n  cuts: validation starts {cut1} (share before {meta['shares_at_cuts'][cut1]:.3f}), "
          f"sealed starts {cut2} (share before {meta['shares_at_cuts'][cut2]:.3f})")
    for z, d in meta["zones"].items():
        print(f"  {z:14s} n={d['n']:>9,}  {d['first_create_utc']} .. {d['last_create_utc']}  sha256={d['sha256'][:16]}")
    print(f"  embargo={meta['counts'].get('embargo', 0):,}  out(>= {zn.END_UTC})={meta['counts'].get('out', 0):,}")
    print(f"  zones.json sha256={zsha}  (files set read-only)")

    reg = Registry(a.registry)
    if not reg.trials() and Path(a.facts).exists():
        f = json.loads(Path(a.facts).read_text(encoding="utf-8"))
        reg.register("T", "T000 coverage artefact vs age_ms",
                     "age_ms 'younger is better' is produced by labels crossing uncollected hours",
                     {"source": "explore_facts_v2", "label_until": f["args"]["label_until"],
                      "facts_generated_utc": f["generated_utc"]},
                     zone="discovery", status="diagnostic", metrics=f.get("diag_T000", {}),
                     notes="AUC 0.384 -> 0.400 after removing CENS: artefact explains a small part only; "
                           "rows still from the look-ahead-filtered universe (D122)")
        reg.register("T", "T001 look-ahead universe filter",
                     "the whole-tape >=50 swaps / >=20 bars filter inflates the hit rate",
                     {"source": "explore_facts_v2", "decision": "K-th bar"},
                     zone="discovery", status="diagnostic", metrics=f.get("decision_K", {}),
                     notes="P(UP) K=5 0.239 vs 0.542 with filter (D122); filter dropped from research")
        print(f"  registry seeded with T000, T001 -> {reg.path}")
    reg.register("T", "T002 zones_v1 frozen", "record of the frozen split",
                 {"zones_json_sha256": zsha, "cuts": meta["cuts"]}, zone="discovery+validation",
                 status="diagnostic", metrics=meta["counts"])
    print(f"  done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
