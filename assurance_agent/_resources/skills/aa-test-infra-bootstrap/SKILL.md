---
name: aa-test-infra-bootstrap
description: "AA Phase 0 (one-shot): scaffold the shared pytest/Locust test infrastructure under tests/ before any planning or codegen runs. Creates or backfills tests/config.py (unified settings), tests/conftest.py with the session-autouse _verify_sut_ready fixture, and tests/schema_validation.py (assert_matches_schema + _LOCAL_SCHEMAS). Idempotent: skip files that already exist with the required contract. Never edits tests/api/, tests/e2e/, tests/fuzz/, tests/perf/. Trigger on: bootstrap qa test infra, test infra missing, no tests/config.py, scaffold tests/ skeleton, aa-test-infra-bootstrap."
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Derive `<project-root>` (directory containing `.aa/config.yaml`; default = cwd).
2. Read `qa/changes/<change-id>/workflow-state.yaml` if `<change-id>` was supplied. If `phases.test_infra_bootstrap.status == done` AND every scaffold file passes the per-file contract check below → STOP, report "already bootstrapped".
3. Read each of the following files (if present) to determine what is missing or non-conformant:
   - `<project-root>/tests/config.py`
   - `<project-root>/tests/conftest.py`
   - `<project-root>/tests/schema_validation.py`
4. Use the **on-disk file contents** (not prior chat memory) as the sole source of truth.

**After completing work:**

1. Each scaffolded file is either created (if missing) or **left untouched** (if it already passes the contract). Never overwrite an existing file unless the user passed `--force` and explicitly named the file.
2. Update `workflow-state.yaml`:
   - Set `phases.test_infra_bootstrap.status = done`
   - Set `phases.test_infra_bootstrap.files = [tests/config.py, tests/conftest.py, tests/schema_validation.py]`
   - Set `phases.test_infra_bootstrap.created = [<subset that was newly created>]`
   - Set `phases.test_infra_bootstrap.kept = [<subset that already existed and passed>]`
3. If `<change-id>` was not supplied, persist the same block to `<project-root>/.aa/state/test-infra.yaml` instead.

---

# AA Test Infra Bootstrap (Phase 0)

## Purpose

Stand up the **shared pytest/Locust test infrastructure** that every other AA phase silently assumes is already on disk:

- `tests/config.py` — unified runtime settings (`base_url`, `frontend_url`, admin credentials, QA test-data prefixes). Single source of truth for API / E2E / Fuzz / Performance.
- `tests/conftest.py` — root pytest conftest, including the session-autouse `_verify_sut_ready` fixture that fails the run fast when the backend SUT is unreachable or admin login is broken. Covers API / E2E / Fuzz (Performance has its own readiness contract in `aa-performance-codegen`).
- `tests/schema_validation.py` — `assert_matches_schema(body, "METHOD /path")` and `_LOCAL_SCHEMAS` registry, used by `aa-api-codegen` to wire response-body schema checks into happy-path API tests.

This skill is **one-shot and idempotent**:

- Runs before `aa-explore` (Phase 1.2) and before `aa-case-design` (Phase 2.1).
- If a file already exists and matches the contract, **leave it alone**.
- Never writes test code under `tests/api/`, `tests/e2e/`, `tests/fuzz/`, `tests/perf/` — those belong to the codegen skills.
- Never invents URLs, ports, or credentials silently. Defaults below are sourced from the repo's `run.py`, `web/.env`, and `app/core/init_app.py`; if the agent cannot verify them from those files, it MUST ask the user before writing.

## When to run

- **Always run** at the start of an AA workflow for a project that does not yet have `tests/config.py`, `tests/conftest.py`, and `tests/schema_validation.py` simultaneously present and contract-conformant.
- **Skip** when all three files already exist and pass the per-file contract check.
- May also be invoked ad-hoc by the user (`aa-test-infra-bootstrap`) to repair partial state.

## Per-file Contract

### tests/config.py

Must define:

- Module-level docstring stating it is the unified settings module for API / E2E / Fuzz / Performance.
- `QaSettings` dataclass (frozen) with at minimum: `base_url`, `base_url_source`, `frontend_url`, `frontend_url_source`, `admin_username`, `admin_password`, and prefix fields `user_prefix`, `role_prefix`, `dept_prefix`.
- `load_settings()` reading env vars (`BASE_URL` / `API_BASE_URL`, `E2E_FRONTEND_URL` / `FRONTEND_URL`, `QA_ADMIN_USERNAME`, `QA_ADMIN_PASSWORD`) with provenance recorded in `*_source` fields.
- Module-level `settings = load_settings()`.

If the file exists but is missing any of these symbols → STOP, ask user before editing.

### tests/conftest.py

Must:

- `import pytest` and `from tests.config import settings`.
- Insert project root into `sys.path` if missing.
- Define `@pytest.fixture(scope="session", autouse=True) def _verify_sut_ready()` that:
  - Honors `QA_SKIP_SUT_READINESS=1` to bypass (CI debug).
  - Performs `GET /openapi.json` against `settings.base_url`; exits via `pytest.exit(..., returncode=2)` on connection error or non-200.
  - Performs `POST /api/v1/base/access_token` with `settings.admin_username` / `settings.admin_password`; exits via `pytest.exit(..., returncode=2)` on failure.
  - All error messages include `settings.base_url_source` so on-call can tell whether the URL came from env or default.

If the file exists but does **not** register `_verify_sut_ready` with `scope="session", autouse=True` → STOP, ask user before editing.

### tests/schema_validation.py

Must define:

- `_LOCAL_SCHEMAS: dict[str, dict]` — keys are `"METHOD /api/path"` strings.
- `assert_matches_schema(body, key: str) -> None` — looks up `key` in `_LOCAL_SCHEMAS`, raises `AssertionError` with the missing-key list if the body does not match the registered schema, raises `LookupError` / `KeyError` with a clear message if `key` is not registered.
- Optional but recommended: `_SUCCESS_EXTRA_WRAPPER` helper for the project's `{code, msg, data}` envelope.

If the file is missing, scaffold with an **empty** `_LOCAL_SCHEMAS` and a docstring telling future codegen runs to register endpoints here (per `aa-api-codegen` "Schema Registration Required" section).

## Steps

```
1. Detect <project-root> and <change-id> (if any).
2. For each of (tests/config.py, tests/conftest.py, tests/schema_validation.py):
   a. If file is absent → mark CREATE.
   b. If file is present → run the per-file contract check.
      - PASS → mark KEEP.
      - FAIL → STOP and ask the user; do NOT auto-overwrite.
3. For each CREATE:
   a. Verify default constants against the project (run.py for port; web/.env for VITE_PORT; app/core/init_app.py for admin credentials).
   b. If any default cannot be verified → ask the user before writing.
   c. Write the file to satisfy the per-file contract above (inline the required symbols and structure from the contract sections — there is no bundled `templates/` directory).
4. Update workflow-state.yaml (or .aa/state/test-infra.yaml) with the created/kept lists.
5. Report a one-screen summary listing each file's status (CREATED / KEPT / BLOCKED).
```

## Hard Rules

- **Idempotent.** Never rewrite a contract-conformant file. Never silently overwrite a non-conformant file.
- **No test code.** Never touch `tests/api/`, `tests/e2e/`, `tests/fuzz/`, `tests/perf/` — those are owned by `aa-*-codegen`.
- **No fixtures beyond `_verify_sut_ready`.** Per-module fixtures (e.g. `api_client`, `auth_headers`, `logged_in_page`) belong in `tests/api/conftest.py` / `tests/e2e/conftest.py` and are scaffolded by the respective codegen skills.
- **Defaults are sourced, not invented.** Read `run.py`, `web/.env`, `app/core/init_app.py` to derive defaults. If unverifiable, ASK.
- **No CLI side effects.** This skill writes files only. It must not start the backend, frontend, or run pytest.
- **One-shot per project.** After `phases.test_infra_bootstrap.status == done`, subsequent AA workflows skip this phase. Re-run only via explicit user request.

## Anti-patterns

- Generating `tests/api/conftest.py` here ❌ — that is `aa-api-codegen`'s output.
- Putting `BASE_URL = "http://localhost:9999"` directly in `tests/conftest.py` ❌ — always import from `tests.config.settings`.
- Pre-registering arbitrary endpoints in `_LOCAL_SCHEMAS` ❌ — registration is per-endpoint and owned by `aa-api-codegen`'s "Schema Registration Required" flow.
- Skipping the user confirmation when defaults can't be verified ❌ — `tests/config.py` is the system's source of truth; wrong defaults silently break every layer.

## Next Workflow

After `aa-test-infra-bootstrap` succeeds:

- Phase 1.1 `aa-workflow` registry check → Phase 1.2 `aa-explore` → Phase 2.1 `aa-case-design` proceeds normally.
- Subsequent codegen skills (`aa-api-codegen`, `aa-e2e-codegen`, `aa-fuzz-codegen`, `aa-performance-codegen`) can safely assume the scaffolded files exist and follow the contracts above.
