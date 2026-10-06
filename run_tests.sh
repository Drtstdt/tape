#!/usr/bin/env bash
# No pytest needed for the core suite -- it is deliberately stdlib-only so a
# fresh clone can prove itself before anything is installed.
set -e
for f in tests/test_*.py; do
  echo "== $f"
  python3 "$f"
done
