# AGENTS.md

## Cursor Cloud specific instructions

This repo is a single Python 3.11 CLI project (`aa`, the "assurance-agent") managed by
[`uv`](https://docs.astral.sh/uv/). There is no long-running service, database, web server, or
frontend to start — the "application" is the `aa` CLI. Standard dev/lint/test/build commands live in
`README.md` ("开发与测试") and `.github/workflows/ci.yml`; use those as the source of truth.

Non-obvious notes:

- `uv` is not part of the base image. The startup update script installs it (to `~/.local/bin`) and
  runs `uv sync --dev`. Interactive shells pick it up via the `. "$HOME/.local/bin/env"` line the
  installer added to `~/.bashrc`. If `uv` is ever missing in a shell, run
  `export PATH="$HOME/.local/bin:$PATH"`.
- `uv sync` provisions its own pinned CPython 3.11 (from `.python-version`); do not rely on the
  system `python3` (which is 3.12).
- Run everything through `uv run ...` (e.g. `uv run aa --version`, `uv run pytest -v`,
  `uv run ruff check .`, `uv run pyright`, `uv run lint-imports`).
- The full CI gate is: `ruff check .`, `ruff format --check .`, `pyright`, `lint-imports` (import
  layering contracts in `.importlinter`), `pytest`, and `bash scripts/packaging_smoke_test.sh`.
- To exercise the CLI end-to-end without an OpenCode server, use the deterministic scheduler on the
  bundled example: `cp -R examples/minimal-sut /tmp/aa-demo && cd /tmp/aa-demo && uv run --project
  <repo> aa init --yes && uv run --project <repo> aa status --change CH-DEMO-001 --next --json`.
  `aa doctor` reporting `warning` for missing `frontend`/`backend`/`playwright` is expected in a bare
  demo SUT and exits 0.
- Full workflow driving (`aa workflow run ... --adapter opencode`) needs an external OpenCode agent
  server (default `http://127.0.0.1:4096`); it is not required for building, testing, or the
  deterministic scheduler.
