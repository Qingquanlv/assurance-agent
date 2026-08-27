# Task 7 Report — Transfer `aa` Ownership to Assurance Product

## Status: DONE_WITH_CONCERNS

`assurance-product` is now the sole owner of `aa`. The root workspace is a
non-publishable uv virtual project. `aa-next` and root product entry points
are absent. Legacy `assurance_agent/` and `packages/assurance-kernel/` were
not deleted. Task 3 live OpenCode admission was not fabricated.

## Baseline

- Branch: `codex/pure-graph-engine-phase3-spec`
- Committed HEAD at start: `3bd375d08f7920040acf310bb39371604a508d54`
- Pre-cutover scripts: root `aa = assurance_agent.cli:main`;
  product `aa-next = assurance_product.cli:main`

## What I implemented

- Product console script is `aa = assurance_product.cli:main`.
- Click identity is `prog_name="aa"` with help text
  `aa — authenticated Assurance graph product.`
- Root `pyproject.toml` is a private `assurance-workspace` with
  `[tool.uv] package = false`. Removed root `[project.scripts]`,
  `[project.entry-points."assurance_agent.products"]`, `[build-system]`,
  and Hatch wheel tables.
- `uv.lock` now has `assurance-workspace` as `source = { virtual = "." }`.
  The `assurance-agent` lock package is gone. `assurance-kernel` remains a
  workspace member and a dev dependency until Tasks 8–9.
- `pythonpath = ["."]` keeps the still-present source tree importable after
  the root package is no longer installed.
- `tests/conftest.py` suppresses `ProductError` when the removed root
  `assurance` entry point is absent, so focused collection can proceed.

## TDD Evidence (RED then GREEN)

**RED** — product still owned `aa-next`; root still publishable:

```bash
uv run pytest tests/phase6/test_cli_cutover.py tests/phase6/test_workspace_manifest.py tests/phase5/test_product_packaging.py -q
```

```text
FAILED tests/phase6/test_cli_cutover.py::test_product_wheel_owns_only_final_aa
FAILED tests/phase6/test_cli_cutover.py::test_main_sets_aa_program_name
FAILED tests/phase6/test_cli_cutover.py::test_help_identity_is_aa_not_aa_next
FAILED tests/phase6/test_workspace_manifest.py::test_root_is_not_a_publishable_distribution
FAILED tests/phase6/test_workspace_manifest.py::test_lock_has_no_root_assurance_agent_package
FAILED tests/phase5/test_product_packaging.py::test_product_metadata_exposes_only_two_product_entry_points
6 failed, 2 passed, 1 warning in 2.31s
```

Expected RED causes: product `console_scripts` was `aa-next`;
`prog_name="aa-next"`; root lacked `tool.uv.package`; lock still listed
`assurance-agent`. The two passing tests are the existing engine-wheel omit
check and the “keep legacy members” guard.

**GREEN** — after script/root transfer, lock rebuild, and collection guard:

```bash
uv sync --dev
uv run aa --help
uv run pytest tests/phase6/test_cli_cutover.py tests/phase6/test_workspace_manifest.py tests/phase5/test_product_packaging.py -q
```

```text
Usage: aa [OPTIONS] COMMAND [ARGS]...

  aa — authenticated Assurance graph product.
```

```text
error: Failed to spawn: `aa-next`
  Caused by: No such file or directory (os error 2)
```

```text
8 passed, 1 warning in 2.29s
```

The warning is the pre-existing `CompiledWorkflow.schema` field-name shadow
in `assurance_kernel`.

Focused lint after GREEN: `ruff check` clean, `ruff format --check` clean,
`pyright` `0 errors, 0 warnings, 0 informations`.

## Files changed

Modified:

- `packages/assurance-product/pyproject.toml`
- `packages/assurance-product/assurance_product/cli.py`
- `pyproject.toml`
- `uv.lock`
- `tests/phase5/test_product_packaging.py`
- `tests/conftest.py` (collection guard after root entry points were removed)

Created:

- `tests/phase6/test_cli_cutover.py`
- `tests/phase6/test_workspace_manifest.py`
- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/task-7-report.md`

Not deleted: `assurance_agent/`, `packages/assurance-kernel/`.

## Self-review

- Sole installed console script is `aa = assurance_product.cli:main`.
- Root has no build backend, no `aa`/`aa-next` script, and no
  `assurance_agent.products` entry points.
- Help begins with `Usage: aa`; `uv run aa-next` cannot resolve.
- Lock members no longer include `assurance-agent`.
- No live OpenCode/Cursor was run. No Task 3 admission artifact was written.

## Concerns

- `tests/conftest.py` is outside the task file list. After
  `[tool.uv] package = false`, ancestor collection still called
  `select_product("assurance")` and failed closed. Fix round 1 now
  ignores only `unknown product: assurance` and re-raises other
  `ProductError` kinds.
- Tests that still require the installed `assurance` product entry point
  will no longer auto-select it. That is expected until Task 8 deletes
  `assurance_agent`.
- Extra libraries (`click`, `httpx`, `packaging`, `pydantic`, `pyyaml`,
  `ruamel.yaml`) moved into the root `dev` group so the still-present
  legacy source tree stays importable until Tasks 8–9.

## Fix round 1 — narrow collection ProductError

Independent review of `3bd375d..bcfbdcd` approved the cutover but flagged
`suppress(ProductError)` as too broad. The guard now ignores only
`unknown product: assurance` (exact or prefix) and re-raises every other
`ProductError`. The `aa` transfer and missing root entry points were left
alone.

**RED** — swallow-all helper still in place:

```bash
uv run pytest tests/phase6/test_conftest_product_guard.py -q
```

```text
FAILED tests/phase6/test_conftest_product_guard.py::test_other_product_errors_are_reraised[duplicate product id 'assurance' declared by: a, b]
FAILED tests/phase6/test_conftest_product_guard.py::test_other_product_errors_are_reraised[product id mismatch: entry 'assurance' != 'other']
FAILED tests/phase6/test_conftest_product_guard.py::test_other_product_errors_are_reraised[invalid product id: assurance]
FAILED tests/phase6/test_conftest_product_guard.py::test_other_product_errors_are_reraised[unknown product: other]
4 failed, 2 passed, 1 warning in 0.12s
```

Expected RED: unknown-assurance cases still pass; duplicate / mismatch /
invalid / other-product messages were swallowed.

**GREEN** — after narrowing `_select_default_product`:

```bash
uv run pytest tests/phase6/test_conftest_product_guard.py -q
uv run pytest tests/phase6/test_cli_cutover.py tests/phase6/test_workspace_manifest.py tests/phase5/test_product_packaging.py -q
```

```text
......                                                                   [100%]
6 passed, 1 warning in 0.13s
```

```text
........                                                                 [100%]
8 passed, 1 warning in 2.17s
```

Combined focused + covering run: `14 passed, 1 warning in 2.31s`. The
warning is still `CompiledWorkflow.schema` field-name shadow.

The focused regression lives at
`tests/phase6/test_conftest_product_guard.py`. It was run for RED/GREEN
but left unstaged because this round commits only `tests/conftest.py`
and this report.
