#!/usr/bin/env bash
# fail-closed: missing or drifted OpenCode is a hard error, never a silent pass.
set -euo pipefail
exec uv run python benchmark/assurance-product/run_item.py \
  --item opencode-ret-dept-management \
  --adapter opencode
