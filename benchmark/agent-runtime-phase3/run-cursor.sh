#!/usr/bin/env bash
# fail-closed: missing or drifted Cursor is a hard error, never a silent pass.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BENCH="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="$BENCH/manifest.json"
OUTPUT="$BENCH/results/cursor-$(date +%Y%m%d-%H%M%S)"
test -f "$MANIFEST"
mkdir -p "$BENCH/results"
cd "$ROOT"
exec uv run python benchmark/agent-runtime-phase3/run_item.py \
  --adapter cursor \
  --manifest "$MANIFEST" \
  --output "$OUTPUT"
