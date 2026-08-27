# Task 6 Report — Audit and Remove Legacy Runtime State Once Before Cutover

## Status: DONE

Implemented the release-time one-shot audit/cleanup pair and ran it against
the frozen empty Phase 5 `project_roots`. Ready-for-cutover with no selected
legacy files is the truthful outcome. Task 3 live OpenCode admission was not
fabricated. Change-local result files were never selected.

## Baseline

- Branch: `codex/pure-graph-engine-phase3-spec`
- Committed HEAD at start: `8db0fbc37dc4fa120a57fa22f2c957cbfdf7126d`
- Handoff consumed (read-only):
  `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.json`
- Frozen `project_roots`: `[]`
- Handoff digest (canonical):
  `d1a91e5096a34960c2e18fde520307ca4566c46d6abe27534e0eea51b5282d5e`

## What I implemented

- `scripts/phase6_legacy_activity_audit.py`: read-only exact-depth scan of
  closed names under handoff `project_roots` only. PID probe uses
  `os.kill(pid, 0)` and never sends a real signal. Live/unresolved/malformed
  /identity-mismatch activity blocks cutover (exit 40).
- `scripts/phase6_legacy_state_cleanup.py`: deletes only after the exact
  zero-live audit digest is presented. Descriptor-relative `unlink`/`rmdir`,
  no-follow opens, same-device checks, and `fsync` of mutated change
  directories. Idempotent on absent selected entries.
- Closed names only: `driver.json`, `driver.lock`, `running-tasks.json`,
  `workflow-state.json`, `workflow-state.yaml`, `.progression.lock`,
  `.graph-runtime/`.
- Never selected or deleted: `events.jsonl`, `status.json`, `.runtime/`,
  `.staging/`.
- Scripts are one-shots: not imported by `assurance_product` and not called
  by `aa start`/`run`.

## TDD Evidence (RED then GREEN)

**RED** — modules absent:

```bash
uv run pytest tests/phase6/test_legacy_activity_audit.py tests/phase6/test_legacy_state_cleanup.py -q
```

```text
ImportError: cannot import name 'phase6_legacy_activity_audit' from 'scripts'
ImportError: cannot import name 'phase6_legacy_state_cleanup' from 'scripts'
25 failed, 2 passed, 1 warning in 0.87s
```

The two passing tests are the product/CLI non-import regressions, which do
not require the new modules.

**GREEN** — after implementation, artifact generation, and format:

```bash
uv run pytest tests/phase6/test_legacy_activity_audit.py tests/phase6/test_legacy_state_cleanup.py -q
```

```text
27 passed, 1 warning in 1.99s
```

Covering gates including handoff/admission:

```bash
uv run pytest tests/phase6/test_legacy_activity_audit.py tests/phase6/test_legacy_state_cleanup.py tests/phase6/test_phase5_handoff.py tests/phase6/test_remaining_phase_admission.py -q
```

```text
57 passed, 1 warning in 4.59s
```

The warning is the pre-existing `CompiledWorkflow.schema` field-name shadow
in `assurance_kernel`.

Focused lint after GREEN: `ruff check` clean, `ruff format --check` clean,
`pyright` `0 errors, 0 warnings, 0 informations`.

## Audit / cleanup / re-audit

Handoff:

```text
.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.json
```

Output directory:

```text
.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout
```

### Audit 1

```bash
uv run python scripts/phase6_legacy_activity_audit.py \
  --handoff .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.json \
  --output .superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/activity-audit.json
```

```text
exit 0
zero-live audit digest:
3313d0ee661ae9013f4bd0674ed0e2d7c1871f78d9c5cbd4a952e2f67fab2bf4
project_roots: []
records: []
ready_for_cutover: true
handoff_digest:
d1a91e5096a34960c2e18fde520307ca4566c46d6abe27534e0eea51b5282d5e
```

### Cleanup

```bash
uv run python scripts/phase6_legacy_state_cleanup.py \
  --handoff .superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.json \
  --audit .superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/activity-audit.json \
  --expected-digest 3313d0ee661ae9013f4bd0674ed0e2d7c1871f78d9c5cbd4a952e2f67fab2bf4 \
  --output .superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/cleanup-report.json
```

```text
exit 0
cleanup digest:
7a86d864c4662723042180692b85949eed0bba0e180d6c7680375ae22c6bff0b
audit_digest:
3313d0ee661ae9013f4bd0674ed0e2d7c1871f78d9c5cbd4a952e2f67fab2bf4
removed: []
runtime_directories_existed: []
status: completed
```

### Audit 2

Same command as Audit 1. Exit 0. Digest unchanged:

```text
3313d0ee661ae9013f4bd0674ed0e2d7c1871f78d9c5cbd4a952e2f67fab2bf4
ready_for_cutover: true
project_roots: []
records: []
```

No Change-local result digest was rewritten. No SUT root was invented.

## Files changed

Created:

- `scripts/phase6_legacy_activity_audit.py`
- `scripts/phase6_legacy_state_cleanup.py`
- `tests/phase6/test_legacy_activity_audit.py`
- `tests/phase6/test_legacy_state_cleanup.py`
- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/activity-audit.json`
- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/cleanup-report.json`
- `.superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/task-6-report.md`

Not staged: pre-existing dirty files, including `progress.md`.

## Self-review

- Exact-depth, no-follow, same-device, descriptor-relative deletion, fsync,
  and idempotency are covered by failing-then-passing tests.
- Regression tests forbid selecting `events.jsonl`, `status.json`,
  `.runtime/`, and `.staging/`.
- Cleanup refuses a live or mismatched audit digest.
- Product/CLI source trees do not import or call these scripts.
- Empty frozen roots were not replaced with invented SUT paths.
