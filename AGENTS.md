# AGENTS.md

## Cursor Cloud specific instructions

This repo is a uv workspace that ships the graph runtime, six capability
wheels, adapter wheels, and `assurance-product`. `aa` is owned by
`assurance-product`. There is no long-running service, database, web server,
or frontend to start. Standard dev/lint/test/build commands live in
`README.md` and `.github/workflows/ci.yml`. `uv sync` at the repo root
installs workspace members into one env.

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
  `pytest`, focused Raw Agent checks, and the three smoke scripts
  (`scripts/graph_engine_smoke_test.sh`,
  `scripts/assurance_capability_wheel_smoke_test.sh`,
  `scripts/assurance_product_wheel_smoke_test.sh`).
- Installed commands are `aa compile`, `aa start`, `aa run`, `aa status`,
  `aa resume`, `aa export`, `aa archive`, `aa bindings build`, and
  `aa lock show`. Delivery is `aa run` to achieved, then `aa export`, then
  optional `aa archive`.
- Full workflow driving (`aa run`) needs an external OpenCode agent server
  (default `http://127.0.0.1:4096`); it is not required for building, testing,
  or the deterministic scheduler.

## Python wheels own topology and Agent contracts

Python wheels own `StateGraph` topology and semantic Agent contracts. OpenCode
writes authorized raw workspace files and returns one locally validated JSON
result. The Kernel seals and commits the actual bytes. Product explicitly
composes six Feature bundles; `.aa/` contains closed organization data only;
changing nodes, edges, contracts, bindings, schemas, or runtime policy requires
code review, tests, wheel rebuild, Checkpoint R, and authenticated deployment.

The engine loads only installed wheel products and does not scan the SUT for
graphs, handlers, schemas, validators, or runtime bindings. Organization
configuration stays in the project's `.aa/`.

Do not load operations, pre-commit validators, gate builtins, or new artifact
shapes from the SUT. One sentence: Python wheels own the graph; OpenCode
returns one raw JSON result; `.aa/` holds organization configuration.
