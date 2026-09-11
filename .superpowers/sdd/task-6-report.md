# Task 6 Report: Remove `aa export` and `aa archive`

## Status

DONE_WITH_CONCERNS

## TDD Evidence

### RED (Step 2)

`test_cli_has_no_export_or_archive_commands` was added first. Production still registered `export` and `archive`.

Command:

```bash
uv run pytest tests/product -k "export or archive or test_cli_has_no_export" -v
```

Output:

```
FAILED test_cli_has_no_export_or_archive_commands
  AssertionError: assert 'export' not in {'archive', 'bindings', 'compile', 'export', 'lock', 'resume', ...}

40 failed, 25 passed, 4 skipped, 853 deselected
```

Exit code: 1

Failure reason: `export` still registered, not a typo. Other `-k` failures were leftover publish/archive module tests from Task 5 path flatten.

### GREEN (Step 4)

Command:

```bash
uv run pytest tests/product/test_cli_fail_closed.py tests/product/test_cli_langgraph_lifecycle.py -v
```

Output:

```
PASSED test_cli_has_no_export_or_archive_commands
PASSED test_all_fourteen_public_entrypoints_are_current
PASSED test_leftover_invocation_without_identity_fails_closed

7 passed, 4 skipped, 3 errors
```

Exit code: 1

The new command-absence test passed. The 3 errors are `opencode_composition` setup: intake plugin descriptor digest disagrees with the static declaration. That is a stale wheel/declaration mismatch from earlier tasks, not from removing the CLI. Improvement/retro graphs did not need a `qa/archive` → `qa/results` allowlist change to compile.

## What changed

- Click app no longer has `export` or `archive`. Delivery stops at achieved.
- `export.py` deleted. `ArchiveError` and `qa/archive` relocate helpers removed from `status.py`.
- `AssuranceProductApplication.export` / `.archive` removed so the product still imports after `export.py` is gone.
- CLI tests that invoked those commands were deleted or inverted. Lifecycle publication stops at achieved.

## Files Changed

| File | Action |
|------|--------|
| `assurance_product/cli.py` | delete `export` / `archive` commands |
| `assurance_product/export.py` | deleted |
| `assurance_product/status.py` | delete `ArchiveError` / archive relocate |
| `assurance_product/application.py` | delete export/archive methods (compile) |
| `tests/product/test_cli_fail_closed.py` | failing test first |
| `tests/product/test_cli_export.py` | deleted |
| `tests/product/test_cli_lifecycle.py` | drop export CLI tests |
| `tests/product/test_cli_compile.py` | invert help command tree |
| `tests/product/test_cli_langgraph_lifecycle.py` | stop at achieved |
| `README.md`, product `README.md` | command tables |

`docs/usage.md` is not in the repo.

## Commit

`2f99203a` Remove aa export and aa archive; delivery stops at achieved.

Only Task 6 product/CLI/docs files were staged. No benchmark/results or eval-fixtures.

## Concerns

- Module tests still import `assurance_product.export` / `archive_published` (`test_result_export.py`, `test_archive_after_publish.py`, `test_export_security.py`, `test_publish_recovery.py`, `test_application_export_archive.py`, `test_replay_properties.py`, `test_terminal_full_closure.py`, `tests/phase6/test_export_delivery_acceptance.py`). Left for Task 7.
- `AGENTS.md` still lists `aa export` / `aa archive`.
- Graph entrypoints `archive` and `improvement-export` remain.
- Step 4 composition fixture errors are pre-existing intake descriptor drift.

## Review findings (Important)

Deleted leftover Click/module tests of `aa export` / `aa archive` / `assurance_product.export` / `archive_published` / `publish_achieved`. Graph-entrypoint tests named `archive` / `improvement-export` were left alone.

- Deleted: `test_application_export_archive.py`, `test_archive_after_publish.py`, `test_result_export.py`, `test_export_security.py`, `test_publish_recovery.py`, `tests/phase6/test_export_delivery_acceptance.py`
- Stripped export/publish bits from `test_replay_properties.py` and `test_terminal_full_closure.py`
- `DELIVERY_FLOW` and `AGENTS.md` now say delivery is `` `aa run` to achieved ``
- Fault catalogs that pointed at deleted publish tests now supersede the six `export-*` rows onto `test_cli_has_no_export_or_archive_commands`

### Verification

```bash
uv run pytest tests/product/test_cli_fail_closed.py -k "export or archive or test_cli_has_no_export" tests/phase6/test_current_documentation.py -v
```

`test_cli_has_no_export_or_archive_commands` PASSED. The `-k` also deselected the six documentation tests, so they were run without the filter: 6 passed.

Also passed: `test_replay_properties.py`, `test_phase5_final_fault_gate.py` (4), docs (6).

`test_run_terminalizes_achieved_full_from_its_terminal_snapshot` still fails on first `application.run` (missing `qa/results/codegen/api-generated-files.json`). That is Task 5 path-flatten fixture drift, not export removal.
