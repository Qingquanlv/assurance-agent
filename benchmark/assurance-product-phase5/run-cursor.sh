#!/usr/bin/env bash
# fail-closed: missing or drifted Cursor is a hard error, never a silent pass.
set -euo pipefail
exec uv run python benchmark/assurance-product-phase5/run_item.py \
  --item cursor-ret-dept-management \
  --adapter cursor
