#!/usr/bin/env bash
# scripts/packaging_smoke_test.sh
# Build a wheel, install it into a throwaway venv, and run aa outside the
# source tree to prove packaged resources resolve without the repo.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

cd "$REPO_ROOT"
uv build --wheel --out-dir "$WORK_DIR/dist"

uv venv --python 3.11 "$WORK_DIR/venv"
uv pip install --quiet --python "$WORK_DIR/venv/bin/python" "$WORK_DIR"/dist/*.whl

mkdir "$WORK_DIR/project"
cd "$WORK_DIR/project"

"$WORK_DIR/venv/bin/aa" --version
"$WORK_DIR/venv/bin/aa" init --yes
test -f .aa/config.yaml
test -f .aa/execution-policy.json
"$WORK_DIR/venv/bin/aa" doctor --json > doctor.json
python3 - <<'PY'
import json
doc = json.load(open("doctor.json"))
assert doc["status"] in ("ok", "warning"), doc["status"]
PY
"$WORK_DIR/venv/bin/aa" config print > /dev/null

# Packaged resources must be readable from the wheel install.
"$WORK_DIR/venv/bin/python" - <<'PY'
from assurance_agent import resources
assert "aa-full" in resources.read_text("schemas", "workflow-schema.yaml")
PY

echo "packaging smoke test: OK"
