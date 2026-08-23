#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
exec uv run python benchmark/assurance-product-phase5/run_comparison.py
