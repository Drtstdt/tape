"""Tiny, dependency-free .env loader.

Not python-dotenv -- this project takes on a dependency only when the
alternative is writing a decoder or an ORM by hand (see store.py, costs.py).
Parsing `KEY=value` lines is stdlib work; one fewer package is one fewer
thing that can break between you and your own API key.

Existing environment variables always win: `os.environ.setdefault` never
overwrites a value the shell already set, matching standard dotenv behaviour
and letting a CI/deploy environment override a stray `.env` file.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path) -> int:
    """Parse a `.env` file into `os.environ`. Returns how many keys were set
    (0 if the file doesn't exist or every key was already set) -- so a
    caller that expects a key can tell "no file" apart from "file present,
    key already came from elsewhere" without a second stat() call.
    """
    p = Path(path)
    if not p.is_file():
        return 0
    set_count = 0
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value
            set_count += 1
    return set_count


def load_project_dotenv() -> int:
    """Load `.env` from the current working directory first (the common
    case: scripts and tests are run from the project root), then from the
    project root inferred from THIS file's location (`tape/env.py` -> project
    root is its parent), so it still works when invoked from elsewhere --
    e.g. an editor's "run" button with some other cwd.
    """
    total = load_dotenv(Path.cwd() / ".env")
    project_root = Path(__file__).resolve().parent.parent
    total += load_dotenv(project_root / ".env")
    return total
