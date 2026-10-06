"""Every event to hourly jsonl.gz files (multi-member gzip, append-safe):
new live data for later research -- every hour, all day, not 12 of 24 (D97)."""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path


class Recorder:
    def __init__(self, root, enabled: bool = True):
        self.root, self.enabled = Path(root), enabled
        self.buf, self.last_flush = [], time.time()

    def write(self, kind: str, **rec):
        if self.enabled:
            rec["k"] = kind
            rec.setdefault("t", int(time.time() * 1000))
            self.buf.append(rec)
            if len(self.buf) >= 2000 or time.time() - self.last_flush > 10:
                self.flush()

    def flush(self):
        if not self.buf:
            return
        by_file = {}
        for r in self.buf:
            tm = time.gmtime(r["t"] / 1000)
            by_file.setdefault(self.root / time.strftime("%Y-%m-%d", tm) / f"{time.strftime('%H', tm)}.jsonl.gz",
                               []).append(r)
        for p, rows in by_file.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, "at", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
        self.buf, self.last_flush = [], time.time()
