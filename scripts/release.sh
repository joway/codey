#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYPROJECT="$ROOT_DIR/pyproject.toml"

if [[ $# -lt 1 ]]; then
  echo "usage: scripts/release.sh <version> [--upload]" >&2
  exit 1
fi

VERSION="$1"
UPLOAD="${2:-}"

if [[ ! -f "$PYPROJECT" ]]; then
  echo "pyproject.toml not found at $PYPROJECT" >&2
  exit 1
fi

VERSION="$VERSION" PYPROJECT="$PYPROJECT" python3 - <<'PY'
from pathlib import Path
import re
import os

path = Path(os.environ["PYPROJECT"])
text = path.read_text(encoding="utf-8")
version = os.environ["VERSION"]
new_text, count = re.subn(r'^version\s*=\s*"[^"]+"', f'version = "{version}"', text, flags=re.M)
if count != 1:
    raise SystemExit("version field not found or ambiguous in pyproject.toml")
path.write_text(new_text, encoding="utf-8")
print(f"updated version to {version}")
PY

python3 -m build

if [[ "$UPLOAD" == "--upload" ]]; then
  python3 -m pip install --upgrade twine >/dev/null
  twine upload "$ROOT_DIR"/dist/*
else
  echo "build complete. pass --upload to publish."
fi
