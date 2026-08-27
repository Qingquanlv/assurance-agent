# AGENTS.md

## Cursor Cloud specific instructions

This repo is a uv workspace that ships the graph runtime, six capability
wheels, adapter wheels, and `assurance-product` (the `aa` CLI). There is no
long-running service, database, web server, or frontend to start. Standard
dev/lint/test/build commands live in `README.md` and `.github/workflows/ci.yml`.
`uv sync` at the repo root installs workspace members into one env.

Non-obvious notes:

- `uv` is not part of the base image. The startup update script installs it (to `~/.local/bin`) and
  runs `uv sync --dev`. Interactive shells pick it up via the `. "$HOME/.local/bin/env"` line the
  installer added to `~/.bashrc`. If `uv` is ever missing in a shell, run
  `export PATH="$HOME/.local/bin:$PATH"`.
- `uv sync` provisions its own pinned CPython 3.11 (from `.python-version`); do not rely on the
  system `python3` (which is 3.12).
- Run everything through `uv run ...` (e.g. `uv run aa --help`, `uv run pytest -v`,
  `uv run ruff check .`, `uv run pyright`, `uv run lint-imports`).
- The full CI gate is: `ruff check .`, `ruff format --check .`, `pyright`, `lint-imports`,
  `pytest`, `uv run python scripts/check_no_legacy.py --scope repository`, and
  `bash scripts/assurance_product_wheel_smoke_test.sh`.
- Full workflow driving (`aa run`) needs an external OpenCode agent server
  (default `http://127.0.0.1:4096`); it is not required for building, testing,
  or the deterministic scheduler.

## Custom orchestration is not a plugin platform

YAML only changes how the graph walks. Do not turn skills, operations,
pre-commit validators, or gate functions into project-loadable plugins. Add
capability by shipping an installed product (`graph_engine.products` entry
points). Organization configuration stays in the project's `.aa/`. The engine
loads only installed wheel products and does not scan the SUT.

Project-replaceable internal orchestration files:

- Workflow graph: packaged `schemas/workflow-schema.yaml`, replaceable by the
  project's `.aa/workflow-schema.yaml`.
- Execution contracts: packaged `schemas/execution-contracts.yaml`, likewise
  replaceable as a whole file.

Do not load operations, pre-commit validators, gate builtins, or new artifact
shapes from the SUT. One sentence: YAML replaces the graph; Python wheels add
capability; `.aa/` holds organization configuration.
