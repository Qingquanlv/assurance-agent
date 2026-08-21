#!/usr/bin/env bash
# fail-closed: missing or drifted Cursor is a hard error, never a silent pass.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MANIFEST="$(cd "$(dirname "$0")" && pwd)/manifest.json"
test -f "$MANIFEST"
cd "$ROOT"
uv run python benchmark/agent-runtime-phase3/run_item.py --adapter cursor --manifest "$MANIFEST"
