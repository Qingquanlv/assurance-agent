# Assurance workspace

Private uv workspace for the installed Assurance graph product. The publishable
wheels are `graph-engine`, six capability packages, `assurance-product`, and the
OpenCode/Cursor adapter wheels. The product console script is `aa`.

There is no long-running service. Develop and test through `uv run`.

## Commands

```bash
uv sync --dev
uv run aa --help
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run python scripts/check_no_legacy.py --scope repository
bash scripts/assurance_product_wheel_smoke_test.sh
```

`aa compile`, `aa start`, `aa run`, `aa status`, `aa export`, and
`aa bindings build` operate on an installed product plus an explicit binding
wheel and project configuration tree.

## Installed product, not a SUT plugin loader

YAML replaces graph and contract text. Python wheels add installed capability.
Project `.aa/` holds organization configuration only. The engine does not load
executable plugins from the system under test.
