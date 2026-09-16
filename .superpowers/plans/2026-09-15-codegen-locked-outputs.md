# Codegen Locked Outputs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Host-lock exact codegen write paths the way case-design locks `case.yaml`, so the model cannot write `qa/tests/api` as a file.

**Architecture:** Derive one test file and one testdata file per case module per family inside `build_codegen_scope`. Prepare’s `allowed_outputs` is that exact `locked_outputs` list. Finalize rebuilds the same scope from `qa/cases/**/case.yaml` and rejects any extra, missing, or mismatched path as retryable `OutputError`. Graph topology stays `codegen → codegen-review → done`.

**Tech Stack:** Python 3.11, pydantic, pytest, `uv run`.

## Global Constraints

- All four families share one family graph; lock all of them (`api`, `e2e`, `fuzz`, `performance`).
- Family dirs: api→`api`, e2e→`e2e`, fuzz→`fuzz`, performance→`perf`.
- Case path shape: `qa/cases/<module>/case.yaml` (same as intake `case_delta_paths`).
- Test file: `qa/tests/<family_dir>/<module>/test_<leaf>.py`.
- Testdata file: `qa/tests/testdata/<family_dir>/<module>.py` (full module, not only leaf).
- `FAMILY_TARGET_ROOTS` testdata becomes per-family (`qa/tests/testdata/api/` etc.). Prefix match is not the write allowlist.
- Finalize validates; it does not rewrite paths.
- Do not change graph topology, `_ATTEMPT_PATHS`, or codegen-review.
- Run tests with `uv run pytest`. Do not kill OpenCode on port 4096.

---

## File map

- `packages/capabilities/assurance-generation/assurance_generation/contracts/codegen.py` — `FAMILY_DIRS`, path helpers, `LockedModuleV1`, `CodegenScopeV1.locked_*`, `FAMILY_TARGET_ROOTS`.
- `packages/capabilities/assurance-generation/assurance_generation/operations/codegen_scope.py` — build lock from `case_ids_by_path`.
- `packages/capabilities/assurance-generation/assurance_generation/operations/planning.py` — `load_family_case_modules` (path → case_ids).
- `packages/capabilities/assurance-generation/assurance_generation/operations/codegen.py` — `codegen_outputs(scope)`, prepare allowlist, finalize exact lock.
- `packages/capabilities/assurance-generation/tests/test_codegen_scope.py` — scope derivation tests.
- `packages/capabilities/assurance-generation/tests/test_codegen.py` — prepare/finalize lock tests.
- `packages/capabilities/assurance-generation/tests/codegen_fixtures.py` — default locked paths from module `items`.
- `packages/capabilities/assurance-generation/tests/planning_fixtures.py` — `family_constraints` write_roots.
- Four `aa-*-codegen/SKILL.md` files — write only `locked_outputs`.

---

### Task 1: Path helpers and family write roots

**Files:**
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/codegen.py`
- Modify: `packages/capabilities/assurance-generation/tests/planning_fixtures.py`
- Test: `packages/capabilities/assurance-generation/tests/test_codegen_scope.py`

**Interfaces:**
- Consumes: existing `LayerName`, `under_write_root`, `CodegenScopeV1.write_roots == FAMILY_TARGET_ROOTS[family]`
- Produces:
  - `FAMILY_DIRS: dict[LayerName, str]`
  - `FAMILY_TARGET_ROOTS[family] == (f"qa/tests/{dir}/", f"qa/tests/testdata/{dir}/")`
  - `case_module_from_path(path: str) -> str`
  - `locked_test_file(family: LayerName, module: str) -> str`
  - `locked_testdata_file(family: LayerName, module: str) -> str`

- [ ] **Step 1: Write the failing tests**

Add to `test_codegen_scope.py`:

```python
from assurance_generation.contracts.codegen import (
    FAMILY_DIRS,
    FAMILY_TARGET_ROOTS,
    case_module_from_path,
    locked_testdata_file,
    locked_test_file,
)


def test_family_dirs_and_target_roots_are_per_family() -> None:
    assert FAMILY_DIRS == {
        "api": "api",
        "e2e": "e2e",
        "fuzz": "fuzz",
        "performance": "perf",
    }
    assert FAMILY_TARGET_ROOTS["api"] == ("qa/tests/api/", "qa/tests/testdata/api/")
    assert FAMILY_TARGET_ROOTS["e2e"] == ("qa/tests/e2e/", "qa/tests/testdata/e2e/")
    assert FAMILY_TARGET_ROOTS["fuzz"] == ("qa/tests/fuzz/", "qa/tests/testdata/fuzz/")
    assert FAMILY_TARGET_ROOTS["performance"] == ("qa/tests/perf/", "qa/tests/testdata/perf/")


def test_locked_paths_use_module_and_family_dir() -> None:
    assert case_module_from_path("qa/cases/dept/case.yaml") == "dept"
    assert locked_test_file("api", "dept") == "qa/tests/api/dept/test_dept.py"
    assert locked_testdata_file("api", "dept") == "qa/tests/testdata/api/dept.py"
    assert case_module_from_path("qa/cases/foo/bar/case.yaml") == "foo/bar"
    assert locked_test_file("e2e", "foo/bar") == "qa/tests/e2e/foo/bar/test_bar.py"
    assert locked_testdata_file("e2e", "foo/bar") == "qa/tests/testdata/e2e/foo/bar.py"


def test_case_module_from_path_rejects_non_case_yaml() -> None:
    with pytest.raises(ValueError, match="qa/cases/<module>/case.yaml"):
        case_module_from_path("qa/cases/dept.yaml")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_codegen_scope.py::test_family_dirs_and_target_roots_are_per_family packages/capabilities/assurance-generation/tests/test_codegen_scope.py::test_locked_paths_use_module_and_family_dir packages/capabilities/assurance-generation/tests/test_codegen_scope.py::test_case_module_from_path_rejects_non_case_yaml -v`

Expected: FAIL with `ImportError` or `AttributeError` for `FAMILY_DIRS` / `case_module_from_path`.

- [ ] **Step 3: Write minimal implementation**

In `contracts/codegen.py`, next to `FAMILY_TARGET_ROOTS`:

```python
from pathlib import PurePosixPath

FAMILY_DIRS: dict[LayerName, str] = {
    "api": "api",
    "e2e": "e2e",
    "fuzz": "fuzz",
    "performance": "perf",
}

FAMILY_TARGET_ROOTS: dict[LayerName, tuple[str, ...]] = {
    family: (f"qa/tests/{directory}/", f"qa/tests/testdata/{directory}/")
    for family, directory in FAMILY_DIRS.items()
}


def case_module_from_path(path: str) -> str:
    parts = PurePosixPath(path).parts
    if len(parts) < 4 or parts[:2] != ("qa", "cases") or parts[-1] != "case.yaml":
        raise ValueError("case path must be qa/cases/<module>/case.yaml")
    return "/".join(parts[2:-1])


def locked_test_file(family: LayerName, module: str) -> str:
    leaf = PurePosixPath(module).name
    return f"qa/tests/{FAMILY_DIRS[family]}/{module}/test_{leaf}.py"


def locked_testdata_file(family: LayerName, module: str) -> str:
    return f"qa/tests/testdata/{FAMILY_DIRS[family]}/{module}.py"
```

Update `planning_fixtures.family_constraints` write_roots to the same per-family testdata prefixes (copy `FAMILY_TARGET_ROOTS` values). Export the new names from `contracts/__init__.py` only if other packages already import `FAMILY_TARGET_ROOTS` from there; otherwise keep them on `contracts.codegen`.

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_codegen_scope.py::test_family_dirs_and_target_roots_are_per_family packages/capabilities/assurance-generation/tests/test_codegen_scope.py::test_locked_paths_use_module_and_family_dir packages/capabilities/assurance-generation/tests/test_codegen_scope.py::test_case_module_from_path_rejects_non_case_yaml -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/capabilities/assurance-generation/assurance_generation/contracts/codegen.py packages/capabilities/assurance-generation/tests/test_codegen_scope.py packages/capabilities/assurance-generation/tests/planning_fixtures.py
git commit -m "feat(generation): derive per-family locked codegen paths"
```

---

### Task 2: Host scope emits locked_modules and locked_outputs

**Files:**
- Modify: `packages/capabilities/assurance-generation/assurance_generation/contracts/codegen.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/codegen_scope.py`
- Test: `packages/capabilities/assurance-generation/tests/test_codegen_scope.py`

**Interfaces:**
- Consumes: `case_module_from_path`, `locked_test_file`, `locked_testdata_file`, `FAMILY_TARGET_ROOTS`
- Produces:
  - `class LockedModuleV1` with `module`, `case_ids`, `test_file`, `testdata_file`
  - `CodegenScopeV1.locked_modules: tuple[LockedModuleV1, ...]`
  - `CodegenScopeV1.locked_outputs: tuple[NonEmptyStr, ...]`
  - `build_codegen_scope(*, family, change_id, cases, capability_leafs, case_ids_by_path: Mapping[str, tuple[str, ...]]) -> CodegenScopeV1`

- [ ] **Step 1: Write the failing tests**

Replace the existing `build_codegen_scope(...)` call sites in `test_codegen_scope.py` so they pass `case_ids_by_path={"qa/cases/items/case.yaml": cases.case ids}`. Then add:

```python
def _case_ids(cases: CaseYamlAuthoring) -> tuple[str, ...]:
    return tuple(sorted(entry.case_id for entry in (*cases.added, *cases.modified)))


@pytest.mark.parametrize("family", ("api", "e2e", "fuzz", "performance"))
def test_host_scope_locks_module_test_and_testdata(family: str) -> None:
    cases = _cases(family)
    ids = _case_ids(cases)
    scope = build_codegen_scope(
        family=family,
        change_id="CH-DEMO-001",
        cases=cases,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids_by_path={"qa/cases/dept/case.yaml": ids},
    )
    directory = {"api": "api", "e2e": "e2e", "fuzz": "fuzz", "performance": "perf"}[family]
    assert scope.locked_modules[0].module == "dept"
    assert scope.locked_modules[0].case_ids == ids
    assert scope.locked_modules[0].test_file == f"qa/tests/{directory}/dept/test_dept.py"
    assert scope.locked_modules[0].testdata_file == f"qa/tests/testdata/{directory}/dept.py"
    assert scope.locked_outputs == tuple(
        sorted(
            (
                f"qa/results/codegen/{family}-codegen-summary.md",
                f"qa/results/codegen/{family}-generated-files.json",
                scope.locked_modules[0].test_file,
                scope.locked_modules[0].testdata_file,
            )
        )
    )


def test_host_scope_locks_one_pair_per_module() -> None:
    cases = _cases("api")
    first = cases.added[0].case_id
    scope = build_codegen_scope(
        family="api",
        change_id="CH-DEMO-001",
        cases=cases,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids_by_path={
            "qa/cases/dept/case.yaml": (first,),
            "qa/cases/menus/case.yaml": (),
        },
    )
    modules = {row.module: row for row in scope.locked_modules}
    assert set(modules) == {"dept"}
    assert modules["dept"].test_file == "qa/tests/api/dept/test_dept.py"


def test_host_scope_nested_module_keeps_full_testdata_path() -> None:
    cases = _cases("api")
    scope = build_codegen_scope(
        family="api",
        change_id="CH-DEMO-001",
        cases=cases,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids_by_path={"qa/cases/foo/bar/case.yaml": _case_ids(cases)},
    )
    row = scope.locked_modules[0]
    assert row.module == "foo/bar"
    assert row.test_file == "qa/tests/api/foo/bar/test_bar.py"
    assert row.testdata_file == "qa/tests/testdata/api/foo/bar.py"


def test_host_scope_rejects_invalid_case_path() -> None:
    cases = _cases("api")
    with pytest.raises((ValueError, ValidationError), match="qa/cases/<module>/case.yaml"):
        build_codegen_scope(
            family="api",
            change_id="CH-DEMO-001",
            cases=cases,
            capability_leafs=frozenset(VALID_LEAFS),
            case_ids_by_path={"qa/cases/dept.yaml": _case_ids(cases)},
        )
```

`test_host_scope_locks_one_pair_per_module` only includes modules that still have family case ids. Empty `menus` row is omitted.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_codegen_scope.py -v`

Expected: FAIL because `build_codegen_scope` has no `case_ids_by_path` and `CodegenScopeV1` has no `locked_modules`.

- [ ] **Step 3: Write minimal implementation**

Add to `contracts/codegen.py`:

```python
class LockedModuleV1(BaseModel):
    model_config = _FROZEN

    module: NonEmptyStr
    case_ids: tuple[NonEmptyStr, ...]
    test_file: NonEmptyStr
    testdata_file: NonEmptyStr
```

On `CodegenScopeV1` add `locked_modules` and `locked_outputs`. In `_validate_scope`, after the existing checks:

```python
covered_lock = tuple(sorted(case_id for row in self.locked_modules for case_id in row.case_ids))
if covered_lock != self.case_ids:
    raise ValueError("locked_modules case_ids must match scope.case_ids exactly once")
generated = []
for row in self.locked_modules:
    if row.test_file != locked_test_file(self.family, row.module):
        raise ValueError(f"locked test_file does not match host rule: {row.test_file}")
    if row.testdata_file != locked_testdata_file(self.family, row.module):
        raise ValueError(f"locked testdata_file does not match host rule: {row.testdata_file}")
    generated.extend((row.test_file, row.testdata_file))
expected_outputs = tuple(
    sorted(
        (
            *generated,
            f"qa/results/codegen/{self.family}-codegen-summary.md",
            f"qa/results/codegen/{self.family}-generated-files.json",
        )
    )
)
if self.locked_outputs != expected_outputs:
    raise ValueError("locked_outputs must be the sorted generated files plus codegen sidecars")
```

In `codegen_scope.py`:

```python
def build_codegen_scope(
    *,
    family: LayerName,
    change_id: str,
    cases: CaseYamlAuthoring,
    capability_leafs: frozenset[str],
    case_ids_by_path: Mapping[str, tuple[str, ...]],
) -> CodegenScopeV1:
    # existing coverage / fuzz / performance construction
    locked_modules = []
    for path, ids in sorted(case_ids_by_path.items()):
        if not ids:
            continue
        module = case_module_from_path(path)
        locked_modules.append(
            {
                "module": module,
                "case_ids": list(ids),
                "test_file": locked_test_file(family, module),
                "testdata_file": locked_testdata_file(family, module),
            }
        )
    generated = [path for row in locked_modules for path in (row["test_file"], row["testdata_file"])]
    locked_outputs = sorted(
        (
            *generated,
            f"qa/results/codegen/{family}-codegen-summary.md",
            f"qa/results/codegen/{family}-generated-files.json",
        )
    )
    return CodegenScopeV1.model_validate(
        {
            # existing fields
            "locked_modules": locked_modules,
            "locked_outputs": locked_outputs,
        },
        context={"capability_leafs": capability_leafs},
    )
```

Every current `build_codegen_scope` caller must pass `case_ids_by_path`. In this task, only `codegen_scope.py` and its unit tests. Other callers are Task 3.

- [ ] **Step 4: Run scope tests**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_codegen_scope.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/capabilities/assurance-generation/assurance_generation/contracts/codegen.py packages/capabilities/assurance-generation/assurance_generation/operations/codegen_scope.py packages/capabilities/assurance-generation/tests/test_codegen_scope.py
git commit -m "feat(generation): emit locked codegen modules and outputs"
```

---

### Task 3: Prepare writes only locked_outputs

**Files:**
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/planning.py`
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/codegen.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_codegen.py`
- Modify: `packages/capabilities/assurance-generation/tests/codegen_fixtures.py`
- Modify: four `aa-*-codegen/SKILL.md` files

**Interfaces:**
- Consumes: `build_codegen_scope(..., case_ids_by_path=...)`, `CodegenScopeV1.locked_outputs`
- Produces:
  - `load_family_case_modules(...) -> tuple[CaseYamlAuthoring, dict[str, tuple[str, ...]]]`
  - `codegen_outputs(scope: CodegenScopeV1) -> tuple[str, ...]` equal to `scope.locked_outputs`
  - Prepare `allowed_outputs` contains no directory root (`qa/tests/api`, `qa/tests/testdata`)

- [ ] **Step 1: Write the failing prepare test**

In `test_codegen.py`, change `test_codegen_prepare_authorizes_family_roots_and_codegen_manifests` into:

```python
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_authorizes_locked_outputs_only(family: str, tmp_path: Path) -> None:
    _write_reviewed_cases(tmp_path, family)
    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    directory = "perf" if family == "performance" else family
    locked_test = f"qa/tests/{directory}/items/test_items.py"
    locked_data = f"qa/tests/testdata/{directory}/items.py"
    allowed = request.workspace.allowed_outputs
    assert allowed == tuple(
        sorted(
            (
                f"qa/results/codegen/{family}-codegen-summary.md",
                f"qa/results/codegen/{family}-generated-files.json",
                locked_test,
                locked_data,
            )
        )
    )
    assert "qa/tests/api" not in allowed
    assert "qa/tests/testdata" not in allowed
    assert f"qa/tests/{directory}" not in allowed
```

`_write_reviewed_cases` already writes `qa/cases/items/case.yaml`. Every prepare test that uses `codegen_input` must call it (or prepare will `InputError` once Task 3 requires on-disk case files).

- [ ] **Step 2: Run the prepare test to verify it fails**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py::test_codegen_prepare_authorizes_locked_outputs_only -v`

Expected: FAIL because `allowed_outputs` still contains `qa/tests/api` / `qa/tests/testdata`.

- [ ] **Step 3: Thread case paths and return locked outputs**

In `planning.py`, add beside `load_family_cases`:

```python
def load_family_case_modules(
    workspace: Path,
    *,
    change_id: str,
    family: Family,
    capability_leafs: tuple[str, ...],
    case_paths: tuple[str, ...] | None = None,
) -> tuple[CaseYamlAuthoring, dict[str, tuple[str, ...]]]:
    cases = load_family_cases(
        workspace,
        change_id=change_id,
        family=family,
        capability_leafs=capability_leafs,
        case_paths=case_paths,
    )
    paths = (
        case_paths
        if case_paths is not None
        else tuple(
            path.relative_to(workspace).as_posix()
            for path in sorted((workspace / "qa" / "cases").glob("**/case.yaml"))
        )
    )
    grouped: dict[str, list[str]] = {}
    for relative in paths:
        document = _yaml_document(_regular_input_file(workspace, workspace.joinpath(*PurePosixPath(relative).parts)))
        ids = [
            str(entry["case_id"])
            for section in ("added", "modified")
            for entry in document.get(section, [])
            if isinstance(entry, Mapping)
            and entry.get("type") == _CASE_TYPES[family]
            and isinstance(entry.get("case_id"), str)
        ]
        grouped[relative] = ids
    return cases, {path: tuple(ids) for path, ids in grouped.items()}
```

If `load_family_cases` already reads those files, prefer extracting the grouping inside that loop instead of reading twice. Either way the public return is `(cases, case_ids_by_path)`.

In `validate_codegen_input`:

- Resolve `case_paths` from `reviewed.case_refs` when present, else `None` (discover).
- Always load through `load_family_case_modules`. If `business.reviewed_cases` is also supplied, keep validating it, but `case_ids_by_path` still comes from the on-disk case files. Missing `qa/cases/**/case.yaml` is `InputError`.
- Call `build_codegen_scope(..., case_ids_by_path=case_ids_by_path)`.

Replace `codegen_outputs` with:

```python
def codegen_outputs(scope: CodegenScopeV1) -> tuple[str, ...]:
    return scope.locked_outputs
```

Prepare:

```python
allowed_outputs=codegen_outputs(scope),
```

Skills (all four `aa-*-codegen/SKILL.md`): in Outputs / Boundaries, replace `qa/tests/<family>/**` and `qa/tests/testdata/**` with: write only host `locked_outputs`; `target_file` must equal the locked test file for that case; testdata must be the locked testdata file. Keep the existing sentence that `allowed_outputs` is the exact write whitelist.

- [ ] **Step 4: Run prepare tests**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py -k prepare -v`

Expected: the new lock test PASS. Other prepare tests that do not write `qa/cases/items/case.yaml` FAIL until you add `_write_reviewed_cases` to each of them. Add that helper call; do not weaken prepare to accept inline cases without files.

- [ ] **Step 5: Commit**

```bash
git add packages/capabilities/assurance-generation/assurance_generation/operations/planning.py packages/capabilities/assurance-generation/assurance_generation/operations/codegen.py packages/capabilities/assurance-generation/tests/test_codegen.py packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-api-codegen/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-e2e-codegen/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-fuzz-codegen/SKILL.md packages/capabilities/assurance-generation/assurance_generation/resources/skills/aa-performance-codegen/SKILL.md
git commit -m "feat(generation): lock codegen prepare outputs to host paths"
```

---

### Task 4: Finalize exact lock, no path rewrite

**Files:**
- Modify: `packages/capabilities/assurance-generation/assurance_generation/operations/codegen.py`
- Modify: `packages/capabilities/assurance-generation/tests/test_codegen.py`
- Modify: `packages/capabilities/assurance-generation/tests/codegen_fixtures.py`

**Interfaces:**
- Consumes: `load_family_case_modules`, `build_codegen_scope`, `LockedModuleV1`
- Produces: finalize `OutputError` when receipt/`files`/`target_file` drift from the rebuilt lock; `InputError` when case paths are not `qa/cases/<module>/case.yaml`

Default fixture module is `items` (same as `_write_reviewed_cases`).

```python
def locked_oracle_paths(family: str, module: str = "items") -> tuple[str, str]:
    directory = "perf" if family == "performance" else family
    return (
        f"qa/tests/{directory}/{module}/test_{module.rsplit('/', 1)[-1]}.py",
        f"qa/tests/testdata/{directory}/{module}.py",
    )
```

`durable_oracle_path(family=...)` should return the locked test file for `items`, not `qa/tests/api/test_users.py`. `codegen_result` must list both the test file and the testdata file (testdata role `support`, empty `case_ids`).

- [ ] **Step 1: Write the failing finalize tests**

```python
def _write_locked_generated(write_root: Path, family: str, module: str = "items") -> tuple[str, str]:
    test_path, data_path = locked_oracle_paths(family, module)
    _write_generated(write_root, family, test_path)
    _write_generated(write_root, family, data_path, b"helper\n")
    return test_path, data_path


@pytest.mark.asyncio
async def test_codegen_finalize_accepts_host_locked_paths(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, data_path = _write_locked_generated(write_root, "api")
    authored = codegen_result(files=[test_path, data_path], family="api")
    _write_manifest(write_root, authored)
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(authored),
        project,
        write_root=write_root,
    )
    assert outcome.status == "succeeded", outcome.failure


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_write_root_as_file(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    bad = "qa/tests/api"
    _write_generated(write_root, "api", bad)
    authored = codegen_result(files=[bad], family="api")
    authored["mapping"]["entries"][0]["target_file"] = bad
    _write_manifest(write_root, authored)
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(authored),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_missing_testdata(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, _ = locked_oracle_paths("api")
    _write_generated(write_root, "api", test_path)
    authored = codegen_result(files=[test_path], family="api")
    _write_manifest(write_root, authored)
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(authored),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_target_file_mismatch(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, data_path = _write_locked_generated(write_root, "api")
    authored = codegen_result(files=[test_path, data_path], family="api")
    authored["mapping"]["entries"][0]["target_file"] = "qa/tests/api/other/test_other.py"
    _write_manifest(write_root, authored)
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(authored),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_invalid_case_path(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    bad = project / "qa/cases/dept.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n", encoding="utf-8")
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(codegen_result(files=["qa/tests/api/dept/test_dept.py"])),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is False
```

`codegen_result` must put testdata entries in `files` as `support` when the list includes the testdata path. Update the helper; do not keep a second ad-hoc manifest shape.

Delete or rewrite `test_codegen_finalize_keeps_support_as_extra_hashed_entry`: extra `qa/tests/api/conftest.py` is no longer legal. The locked testdata file is the only support file.

- [ ] **Step 2: Run the new finalize tests to verify they fail**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py::test_codegen_finalize_accepts_host_locked_paths packages/capabilities/assurance-generation/tests/test_codegen.py::test_codegen_finalize_rejects_write_root_as_file packages/capabilities/assurance-generation/tests/test_codegen.py::test_codegen_finalize_rejects_missing_testdata packages/capabilities/assurance-generation/tests/test_codegen.py::test_codegen_finalize_rejects_target_file_mismatch packages/capabilities/assurance-generation/tests/test_codegen.py::test_codegen_finalize_rejects_invalid_case_path -v`

Expected: FAIL. Accept test fails because testdata is not required yet; reject tests fail because write-root paths still pass `family_allows_target`.

- [ ] **Step 3: Finalize rebuilds the host lock**

In `CodegenFinalizeHandler.execute`, after validating the payload and before `_complete_files`:

```python
_, scope, _ = validate_codegen_input(
    {
        "change_id": payload.change_id or document.change_id,
        "plan_digest": payload.plan_digest,
        "plan_ref": payload.plan_ref.model_dump(mode="json"),
        "capability_leafs": list(payload.capability_leafs),
        "reviewed_case": None
        if payload.reviewed_case is None
        else payload.reviewed_case.model_dump(mode="json"),
    },
    family,
    context.project_root,
)
```

`validate_codegen_input` already raises `InputError` for a bad case path.

Then enforce, all as `OutputError` (no coerce):

```python
locked_generated = {
    path
    for path in scope.locked_outputs
    if path.startswith("qa/tests/")
}
test_by_case = {
    case_id: row.test_file
    for row in scope.locked_modules
    for case_id in row.case_ids
}
receipt_tests = {entry.repo_path for entry in document.files}
if receipt_tests != locked_generated:
    raise OutputError(
        "codegen receipt test paths do not match locked outputs; "
        f"missing={sorted(locked_generated - receipt_tests)}, "
        f"unexpected={sorted(receipt_tests - locked_generated)}"
    )
for item in document.mapping.entries:
    expected = test_by_case.get(item.case_id)
    if expected is None or item.target_file != expected:
        raise OutputError(
            f"mapping target_file must equal the locked test file for {item.case_id}"
        )
```

In `_complete_files`, replace `family_allows_target` / `under_write_root` allowlisting with exact membership in `locked_generated`. Pass `locked_generated` in as `allowed_paths` and treat it as an exact set:

```python
if target not in allowed_paths:
    raise OutputError(f"undeclared generated/modified test file: {target}")
```

Do not rewrite `target_file` or `repo_path`.

Existing finalize tests must write `qa/cases/items/case.yaml` and include the testdata file. Update `codegen_result` / `durable_oracle_path` so the rest of `test_codegen.py` follows the lock.

- [ ] **Step 4: Run finalize and codegen tests**

Run: `uv run pytest packages/capabilities/assurance-generation/tests/test_codegen.py packages/capabilities/assurance-generation/tests/test_codegen_scope.py -v`

Expected: PASS.

Then: `uv run pytest packages/capabilities/assurance-generation/tests -q`

Expected: PASS, or only failures in tests that still assume `qa/tests/api/test_users.py` / directory-root `allowed_outputs`. Fix those fixtures to the `items` lock; do not loosen finalize.

- [ ] **Step 5: Commit**

```bash
git add packages/capabilities/assurance-generation/assurance_generation/operations/codegen.py packages/capabilities/assurance-generation/tests/test_codegen.py packages/capabilities/assurance-generation/tests/codegen_fixtures.py
git commit -m "feat(generation): reject codegen writes outside the host lock"
```

---

## Spec coverage

| Spec section | Task |
|---|---|
| Path rules / family dirs | 1 |
| `FAMILY_TARGET_ROOTS` per-family testdata | 1 |
| `locked_modules` / `locked_outputs` | 2 |
| Invalid case path → `InputError` | 2 (scope), 4 (finalize) |
| Prepare exact `allowed_outputs` | 3 |
| Skills: write only lock | 3 |
| Mapping `target_file` must match lock | 4 |
| Finalize exact receipt / testdata required / no rewrite | 4 |
| OutputError retryable for bad writes | 4 |
| Graph / codegen-review unchanged | no task |

## Self-review

- No TBD / “similar to Task N” without code.
- `build_codegen_scope` gains `case_ids_by_path` in Task 2; Task 3 is the first production caller.
- Fixture module is `items` everywhere `_write_reviewed_cases` writes `qa/cases/items/case.yaml`.
- Extra support files such as `qa/tests/api/conftest.py` are out of the lock; testdata is the only support file.
