#!/usr/bin/env bash
# sync-core.sh — vendor the jev-routing core daemon into the Pi package.
#
# Copies <src>/core/ -> adapters/pi/core/ (flat, stdlib-only Python).
# Excludes __pycache__ and test_*.py: the package ships the daemon, not the
# core's test suite (those run from the source repo).
#
# The destination is a build artifact (gitignored); the source of truth is
# /workspace/jev-routing/core. Run from anywhere:
#   scripts/sync-core.sh            # default source /workspace/jev-routing/core
#   JEV_CORE_SRC=/path/to/repo scripts/sync-core.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Default source: sibling repo jev-routing/core, resolved relative to this
# repo — works under any mount point (/workspace in omp, /opt/data on host).
SRC="${JEV_CORE_SRC:-$ROOT/../jev-routing/core}"
DEST="$ROOT/adapters/pi/core"

if [ ! -f "$SRC/jev_core.py" ]; then
  echo "sync-core: $SRC does not look like the jev-routing core (no jev_core.py)" >&2
  exit 1
fi

rm -rf "$DEST"
mkdir -p "$DEST"

# Top-level files only (the core is flat): every .py plus any non-test extras,
# never __pycache__/, never test_*.py (or the tests' _testbase.py helper).
find "$SRC" -maxdepth 1 -type f ! -name 'test_*.py' ! -name '_testbase.py' -exec cp {} "$DEST/" \;

# Defensive: if a future core grows subpackages, mirror them minus caches/tests.
find "$SRC" -mindepth 1 -type d ! -name '__pycache__' -exec sh -c '
  src="$1"; dest="$2"; rel="${1#"$src"/}"
  mkdir -p "$dest/$rel"
  find "$src/$rel" -maxdepth 1 -type f ! -name "test_*.py" -exec cp {} "$dest/$rel/" \;
' _ "$SRC" "$DEST" \; 2>/dev/null || true

count=$(find "$DEST" -type f | wc -l)
echo "sync-core: $count files -> $DEST"
