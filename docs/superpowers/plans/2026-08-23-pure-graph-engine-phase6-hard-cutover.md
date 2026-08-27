# Pure Graph Engine Phase 6 Hard Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `assurance-product` the sole owner of `aa`, directly remove closed legacy runtime state before seed capture, delete the legacy distributions and runtime, and release an export-only pure graph-engine Assurance product.

**Architecture:** Admit only a complete Phase 5 handoff, audit legacy activity before the cut, and add one product-owned closed state cleaner that runs only before a new invocation seed is captured. Atomically transfer the `aa` console script and workspace packaging to `assurance-product`, delete `assurance_agent` and `assurance_kernel` without a bridge, retire the executable legacy comparison baseline, and prove the final installed product through isolated OpenCode/Cursor benchmarks whose only delivery surface is `aa export`.

**Tech Stack:** Python 3.11, uv workspace, Click, Pydantic v2 frozen models, graph-engine immutable snapshots and ledger, Python entry points, Hatchling wheels, pytest, Ruff, Pyright, import-linter, POSIX descriptor-relative filesystem operations on macOS and Linux, Bash release smoke scripts.

**Spec:** `docs/superpowers/specs/2026-08-23-pure-graph-engine-phase6-hard-cutover-design.md`

## Global Constraints

- Implement only after the Phase 5 handoff is mechanically complete, including one successful provider-live OpenCode product benchmark and one successful provider-live Cursor product benchmark; quota, credential, executable, timeout, or provider failures are blockers.
- Work in a fresh isolated worktree created with `superpowers:using-git-worktrees`; do not execute this deletion plan in the dirty Phase 5 worktree.
- Preserve unrelated user changes and untracked benchmark results. Every commit stages only the files declared by its task.
- `assurance-product` becomes the only distribution publishing `aa`; `aa-next`, a launcher shell, a hidden legacy command, and duplicate `aa` entry points are forbidden.
- Delete the publishable `assurance-agent` and `assurance-kernel` distributions, the `assurance_agent` and `assurance_kernel` import packages, ProductHooks, old catalogs, old runtime, forwarding imports, old resources, and obsolete tests in this phase.
- Do not translate, read, resume, replay, export, or score a legacy invocation after cutover.
- Cleanup runs only before `aa start` or a first `aa run` creates an invocation, only below the explicit absolute `--project-dir`, and before stable seed capture.
- Cleanup directly removes only immediate `qa/changes/{change_id}/{events.jsonl,workflow-state.json,workflow-state.yaml,running-tasks.json,.progression.lock,driver.json,driver.lock,.graph-runtime/}` entries.
- Preserve `.aa/{config.yaml,policy.yaml,data-knowledge.yaml,memory/**}`, `qa/cases/**`, `qa/archive/**`, `qa/issues/**`, `qa/improvements/**`, `qa/retro/**`, business files below `qa/changes/**`, and `tests/**`.
- Cleanup follows no symlink, accepts no hardlink or special selected file, crosses no mount, deletes no matching basename at another depth, creates no backup, and is idempotent after a legal partial prefix.
- A live or unresolved legacy process blocks cleanup and seed capture. The final product never signals a PID based only on legacy state.
- After seed capture, tasks write only engine-owned attempt workspaces; successful validated candidates alone advance immutable snapshot HEAD.
- `aa export` is the only result delivery command. It exports only an authenticated succeeded HEAD into an absent or empty destination and never writes or merges into the original SUT.
- Do not implement `aa apply`, three-way merge, copy-back, drift resolution, rollback journal, `ApplyReceipt`, or an original-SUT publisher.
- `graph-engine` remains business-neutral, contains no default graph, and loads no executable SUT plugin.
- Runtime code comes only from explicit installed authenticated wheels. Project configuration remains strict, data-only, and incapable of granting handlers, operations, validators, effects, models, endpoints, executables, permissions, or secret authority.
- Both final products select exactly one adapter and exact route/model/permission/source identities; no fallback or ambient discovery is introduced.
- Provider conversations remain provider-owned. The graph ledger stores only bounded generic activity evidence and canonical product results.
- Every implementation task uses RED/GREEN TDD, runs its focused regression gate, writes `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/task-{N}-report.md`, requests Spec and Standards review, resolves every P1/P2 finding, and commits independently.
- Run Python commands through `uv run`; do not use the system Python for repository code.
- Before final acceptance run Ruff, format check, Pyright, import-linter, full pytest, the final committed-HEAD product wheel smoke, the no-legacy gate, and both provider-live benchmarks.

---

## File Responsibility Map

### Cutover evidence and release tools

- `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/admission.json`: immutable Phase 5 handoff and provider-live admission digests.
- `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/deletion-inventory.json`: exact source, test, resource, command, comparison, and configuration deletions with replacement evidence.
- `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/activity-audit.json`: final pre-cut legacy activity scan.
- `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/final-evidence.json`: final command, wheel, gate, benchmark, cleanup, and export identities.
- `scripts/check_phase6_admission.py`: fails unless the completed Phase 5 handoff and required evidence match the frozen admission file.
- `scripts/phase6_legacy_activity_audit.py`: read-only explicit-project scan for live or unresolved legacy activity.
- `scripts/check_no_legacy.py`: source, metadata, entry-point, wheel, import, and current-document no-legacy gate.
- `scripts/no_legacy_allowlist.txt`: exact historical spec/evidence files allowed to name the deleted system.

### Final Assurance product

- `packages/assurance-product/assurance_product/legacy_cleanup_models.py`: frozen cleanup entry/report contracts and canonical digest.
- `packages/assurance-product/assurance_product/legacy_cleanup.py`: exact-depth inspection, live-state rejection, descriptor-relative deletion, fsync, and idempotent retry.
- `packages/assurance-product/assurance_product/models.py`: `AssuranceRootInputV1` binding the cleanup report into root input.
- `packages/assurance-product/assurance_product/cli.py`: final `aa` command, pre-seed cleanup integration, and export-only delivery.
- `packages/assurance-product/assurance_product/export.py`: unchanged authority for fresh-destination succeeded-HEAD export; changed only if final regressions expose a spec mismatch.
- `packages/assurance-product/assurance_product/resources/schemas/invocation-root-input-v1.json`: strict runtime-generated root-input schema including cleanup evidence.
- `packages/assurance-product/pyproject.toml`: sole `aa` console script and final dependencies/entry points.
- `packages/assurance-product/README.md`: final command and isolated result workflow.

### Workspace and architecture

- `pyproject.toml`: non-publishable uv workspace aggregator, final test/type/lint roots, no legacy build or dependency.
- `uv.lock`: final workspace/dependency closure without the deleted distributions.
- `.importlinter`: final deep-module boundaries only.
- `.github/workflows/ci.yml`: final no-legacy, repository, and wheel gates.
- `assurance_agent/`: deleted in full.
- `packages/assurance-kernel/`: deleted in full.
- `scripts/packaging_smoke_test.sh`: deleted; final product smoke is authoritative.
- `scripts/assurance_product_wheel_smoke_test.sh`: committed-HEAD final wheel and command isolation matrix.

### Tests and benchmark

- `tests/phase6/`: admission, activity, cleanup, CLI cutover, workspace, deletion, no-legacy, export-only, and acceptance tests.
- `tests/product/`: final name for retained Phase 5 product/graph/runtime tests and non-legacy fixtures.
- `tests/phase5/fixtures/comparison/legacy/`: deleted.
- `benchmark/assurance-product/`: final provider-live harness, manifest, export validator, and result layout.
- `benchmark/assurance-product-phase5/`: removed after retained final harness files move.
- `README.md`, `AGENTS.md`, and current architecture/release docs: final package/command/workflow instructions.

## Dependency Order

```text
1 Phase 5 admission
  -> 2 activity freeze/drain audit
  -> 3 cleanup contracts/inspection
  -> 4 destructive cleanup safety
  -> 5 pre-seed integration
  -> 6 aa/package takeover
  -> 7 final wheel smoke
  -> 8 assurance_agent deletion
  -> 9 assurance_kernel deletion
  -> 10 no-legacy architecture gate
  -> 11 comparison/test retirement
  -> 12 final isolated benchmark
  -> 13 export-only acceptance
  -> 14 docs/examples/release surface
  -> 15 OpenCode live gate
  -> 16 Cursor live gate
  -> 17 full security/business/repository gates
  -> 18 final cutover evidence
```

---

### Task 1: Admit the Complete Phase 5 Handoff and Freeze Deletion Inputs

**Files:**
- Create: `tests/phase6/__init__.py`
- Create: `tests/phase6/conformance.py`
- Create: `tests/phase6/test_admission.py`
- Create: `scripts/check_phase6_admission.py`
- Create: `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/admission.json`
- Create: `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/deletion-inventory.json`

**Interfaces:**
- Consumes: completed `.superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly/phase6-handoff.md`, Phase 4 ownership/handoff inventories, Phase 5 benchmark evidence, and committed Phase 5 source.
- Produces: `AdmissionV1`, `DeletionInventoryV1`, `load_admission(path: Path) -> AdmissionV1`, `load_deletion_inventory(path: Path) -> DeletionInventoryV1`, and a command that exits nonzero unless every frozen digest and gate is current.

- [ ] **Step 1: Write the failing admission tests**

```python
def test_phase6_admission_requires_complete_live_handoff(evidence_root: Path) -> None:
    admission = load_admission(evidence_root / "admission.json")
    assert admission.schema_version == "1"
    assert admission.phase5_status == "complete"
    assert admission.provider_live == {"opencode": "succeeded", "cursor": "succeeded"}
    assert all(value.startswith("sha256:") for value in admission.digests.values())


def test_every_deletion_has_replacement_or_obsolete_proof(evidence_root: Path) -> None:
    inventory = load_deletion_inventory(evidence_root / "deletion-inventory.json")
    assert inventory.compatibility_bridge_allowed is False
    assert inventory.items
    assert all(item.disposition in {"replaced", "obsolete"} for item in inventory.items)
    assert all(item.proof for item in inventory.items)
```

- [ ] **Step 2: Run the admission test and confirm Phase 5 currently blocks it**

Run: `uv run pytest tests/phase6/test_admission.py -q`

Expected: FAIL while the handoff contains `stub_pending_task_27`, lacks either successful live benchmark, or the admission files do not exist. Do not continue Phase 6 implementation until Phase 5 closes this failure honestly.

- [ ] **Step 3: Implement strict evidence models and digest verification**

```python
class AdmissionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    phase5_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    phase5_status: Literal["complete"]
    provider_live: dict[Literal["opencode", "cursor"], Literal["succeeded"]]
    legacy_projects: tuple[str, ...]
    digests: dict[str, str]


class DeletionItemV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    kind: Literal["source", "test", "resource", "command", "comparison", "configuration"]
    disposition: Literal["replaced", "obsolete"]
    owner: str
    proof: str
    status: Literal["planned", "verified"]


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def verify_admission(repo: Path, admission: AdmissionV1) -> None:
    for relative, expected in sorted(admission.digests.items()):
        actual = sha256_file(repo / relative)
        if actual != expected:
            raise AdmissionError(f"admission digest drift: {relative}")
```

Define the inventory around `DeletionItemV1`:

```python
class DeletionInventoryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    compatibility_bridge_allowed: Literal[False]
    items: tuple[DeletionItemV1, ...]
```

Initial items use `status="planned"`. Reject duplicate paths, glob paths, paths outside the repository, unclassified items, missing proof, or an item that names a new production owner under a legacy root.

Validate `legacy_projects` as sorted unique absolute project roots taken from the completed Phase 5 old-invocation disposition list. An empty tuple is valid only when the handoff explicitly states that no old project root exists.

- [ ] **Step 4: Freeze the completed handoff and exact deletion inventory**

Run:

```bash
uv run python scripts/check_phase6_admission.py \
  --repo . \
  --freeze .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/admission.json \
  --inventory .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/deletion-inventory.json
```

Expected: exit 0; the generated admission records the exact completed Phase 5 commit and exact evidence digests, and the deletion inventory covers every Phase 4/5 legacy residual exactly once.

- [ ] **Step 5: Run focused verification and commit**

Run: `uv run pytest tests/phase6/test_admission.py -q && uv run ruff check scripts/check_phase6_admission.py tests/phase6`

Expected: PASS.

```bash
git add scripts/check_phase6_admission.py tests/phase6
git add -f .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/admission.json .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/deletion-inventory.json
git commit -m "chore(phase6): freeze cutover admission"
```

---

### Task 2: Audit Legacy Activity Before the Cut

**Files:**
- Create: `scripts/phase6_legacy_activity_audit.py`
- Create: `tests/phase6/test_legacy_activity_audit.py`
- Create: `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/activity-audit.json`

**Interfaces:**
- Consumes: the exact `legacy_projects` roots in Task 1 `admission.json` and exact direct-child `driver.json` / `running-tasks.json` files.
- Produces: `ActivityRecordV1`, `ActivityAuditV1`, `audit_projects(projects: tuple[Path, ...], probe: ProcessProbe) -> ActivityAuditV1`; exits 40 when any activity is live or unresolved and never sends a signal.

- [ ] **Step 1: Write failing read-only audit tests**

```python
def test_activity_audit_scans_only_immediate_change_roots(tmp_path: Path) -> None:
    project = seeded_project(tmp_path)
    write_driver(project / "qa/changes/CH-1/driver.json", pid=101)
    write_driver(project / "qa/archive/CH-OLD/driver.json", pid=202)
    audit = audit_projects((project,), probe=FakeProbe({101: "dead", 202: "live"}))
    assert [item.relative_path for item in audit.records] == ["qa/changes/CH-1/driver.json"]
    assert audit.ready_for_cutover is True


def test_live_or_unresolved_activity_blocks_cutover(tmp_path: Path) -> None:
    project = seeded_project(tmp_path)
    write_driver(project / "qa/changes/CH-1/driver.json", pid=101)
    for state in ("live", "unresolved"):
        audit = audit_projects((project,), probe=FakeProbe({101: state}))
        assert audit.ready_for_cutover is False
```

- [ ] **Step 2: Run tests to verify the audit module is missing**

Run: `uv run pytest tests/phase6/test_legacy_activity_audit.py -q`

Expected: FAIL because `phase6_legacy_activity_audit` is absent.

- [ ] **Step 3: Implement the strict scanner and process probe**

```python
class ProcessProbe(Protocol):
    def state(self, pid: int) -> Literal["dead", "live", "unresolved"]:
        raise NotImplementedError


def candidate_state_files(project: Path) -> tuple[Path, ...]:
    changes = project / "qa" / "changes"
    if not changes.is_dir() or changes.is_symlink():
        return ()
    return tuple(
        path
        for change in sorted(changes.iterdir(), key=lambda item: item.name)
        if change.is_dir() and not change.is_symlink()
        for path in (change / "driver.json", change / "running-tasks.json")
        if path.exists()
    )
```

Use `os.kill(pid, 0)` only to classify PID liveness. Treat permission denial, malformed JSON, invalid PID, identity disagreement, and an unreadable file as `unresolved`. Do not terminate a process or interpret a graph event.

- [ ] **Step 4: Record a zero-activity cutover audit**

Run:

```bash
uv run python scripts/phase6_legacy_activity_audit.py \
  --admission .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/admission.json \
  --output .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/activity-audit.json
```

Expected: exit 0 only after operators drain or explicitly terminate every approved old process; output has `ready_for_cutover: true` and no live/unresolved records.

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/phase6/test_legacy_activity_audit.py -q && uv run ruff check scripts/phase6_legacy_activity_audit.py tests/phase6/test_legacy_activity_audit.py`

```bash
git add scripts/phase6_legacy_activity_audit.py tests/phase6/test_legacy_activity_audit.py
git add -f .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/activity-audit.json
git commit -m "chore(phase6): audit legacy activity"
```

---

### Task 3: Define Closed Cleanup Contracts and Exact-Depth Inspection

**Files:**
- Create: `packages/assurance-product/assurance_product/legacy_cleanup_models.py`
- Create: `packages/assurance-product/assurance_product/legacy_cleanup.py`
- Create: `tests/phase6/test_legacy_cleanup_inspection.py`
- Create: `tests/phase6/cleanup_fixtures.py`
- Modify: `packages/assurance-product/assurance_product/__init__.py`

**Interfaces:**
- Consumes: one absolute `project_dir: Path`.
- Produces: `LegacyCleanupEntryV1`, `LegacyCleanupReportV1`, `LegacyCleanupError`, `inspect_legacy_runtime_state(project_dir: Path) -> tuple[LegacyCleanupEntryV1, ...]`, and `legacy_cleanup_digest(report: LegacyCleanupReportV1) -> str`.
- Produces for tests: `seed_cleanup_project(root: Path) -> Path`, `digest_preserved_files(project: Path) -> dict[str, str]`, and `project_with_unsafe_entry(root: Path, kind: str) -> Path` in `tests.phase6.cleanup_fixtures`.
- Consumers: Tasks 4 and 5.

- [ ] **Step 1: Write failing closed-path inspection tests**

```python
CLOSED_FILES = {
    "events.jsonl",
    "workflow-state.json",
    "workflow-state.yaml",
    "running-tasks.json",
    ".progression.lock",
    "driver.json",
    "driver.lock",
}


def test_inspection_selects_only_closed_immediate_paths(project_with_legacy_state: Path) -> None:
    entries = inspect_legacy_runtime_state(project_with_legacy_state)
    paths = {item.relative_path for item in entries}
    assert paths == {
        *(f"qa/changes/CH-1/{name}" for name in CLOSED_FILES),
        "qa/changes/CH-1/.graph-runtime",
    }
    assert "qa/archive/CH-1/events.jsonl" not in paths
    assert "qa/changes/CH-1/issues/events.jsonl" not in paths
```

- [ ] **Step 2: Run tests and confirm the models/functions are absent**

Run: `uv run pytest tests/phase6/test_legacy_cleanup_inspection.py -q`

Expected: FAIL on missing `assurance_product.legacy_cleanup`.

- [ ] **Step 3: Add frozen models and canonical digest**

```python
class LegacyCleanupEntryV1(FrozenModel):
    relative_path: str
    kind: Literal["file", "directory"]
    size: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class LegacyCleanupReportV1(FrozenModel):
    schema_version: Literal["1"]
    project_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    removed: tuple[LegacyCleanupEntryV1, ...]
    runtime_directories_existed: tuple[str, ...]
    status: Literal["completed"]


def legacy_cleanup_digest(report: LegacyCleanupReportV1) -> str:
    return canonical_digest(cast(JSONValue, report.model_dump(mode="json")))
```

Validators require sorted unique canonical POSIX paths, file entries with digest, directory entries without digest, and exact closed suffixes. Compute `project_identity_digest` as the canonical digest of `{"resolved_path": str(project), "st_dev": stat.st_dev, "st_ino": stat.st_ino}` captured from the validated project-root descriptor.

- [ ] **Step 4: Implement no-follow exact-depth inspection**

```python
_CLOSED_FILES = (
    ".progression.lock",
    "driver.json",
    "driver.lock",
    "events.jsonl",
    "running-tasks.json",
    "workflow-state.json",
    "workflow-state.yaml",
)
_RUNTIME_DIRECTORY = ".graph-runtime"


def inspect_legacy_runtime_state(project_dir: Path) -> tuple[LegacyCleanupEntryV1, ...]:
    project = require_safe_absolute_project(project_dir)
    change_roots = open_immediate_change_roots(project)
    entries = [entry for root in change_roots for entry in inspect_closed_entries(project, root)]
    return tuple(sorted(entries, key=lambda item: item.relative_path))
```

Use `Path.lstat()` and `os.stat(name, dir_fd=directory_fd, follow_symlinks=False)` and reject symlink, hard-linked regular file, FIFO, socket, device, unexpected directory, broad root, traversal, or depth mismatch. Inspection reads bytes only to calculate regular-file SHA-256 and never parses legacy events.

In `cleanup_fixtures.py`, expose a pytest `project_with_legacy_state` fixture that calls `seed_cleanup_project(tmp_path)`. The seeded tree includes every closed file, `.graph-runtime/checkpoints/state.json`, and preserved sentinels under `.aa`, `qa/cases`, `qa/archive`, `qa/issues`, `qa/improvements`, nested change artifact directories, and `tests`.

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/phase6/test_legacy_cleanup_inspection.py -q && uv run ruff check packages/assurance-product/assurance_product/legacy_cleanup*.py tests/phase6/test_legacy_cleanup_inspection.py`

```bash
git add packages/assurance-product/assurance_product/legacy_cleanup_models.py packages/assurance-product/assurance_product/legacy_cleanup.py packages/assurance-product/assurance_product/__init__.py tests/phase6/test_legacy_cleanup_inspection.py tests/phase6/cleanup_fixtures.py
git commit -m "feat(product): inspect closed legacy state"
```

---

### Task 4: Directly Delete Legacy State with Filesystem Safety and Retry

**Files:**
- Modify: `packages/assurance-product/assurance_product/legacy_cleanup.py`
- Create: `tests/phase6/test_legacy_cleanup.py`
- Create: `tests/phase6/test_legacy_cleanup_faults.py`

**Interfaces:**
- Consumes: Task 3 closed inspection contracts.
- Produces: `cleanup_legacy_runtime_state(project_dir: Path) -> LegacyCleanupReportV1`.
- Consumers: Task 5 pre-seed start path.

- [ ] **Step 1: Write failing deletion and preservation tests**

```python
def test_cleanup_deletes_closed_state_and_preserves_business_data(project_with_legacy_state: Path) -> None:
    before = digest_preserved_files(project_with_legacy_state)
    report = cleanup_legacy_runtime_state(project_with_legacy_state)
    assert report.status == "completed"
    assert inspect_legacy_runtime_state(project_with_legacy_state) == ()
    assert digest_preserved_files(project_with_legacy_state) == before


def test_cleanup_is_idempotent(project_with_legacy_state: Path) -> None:
    first = cleanup_legacy_runtime_state(project_with_legacy_state)
    second = cleanup_legacy_runtime_state(project_with_legacy_state)
    assert first.removed
    assert second.removed == ()
    assert second.runtime_directories_existed == ()


def test_proven_live_driver_blocks_cleanup(project_with_legacy_state: Path, monkeypatch) -> None:
    monkeypatch.setattr(legacy_cleanup, "_pid_state", lambda pid: "live")
    with pytest.raises(LegacyCleanupError, match="legacy activity is live"):
        cleanup_legacy_runtime_state(project_with_legacy_state)


def test_malformed_stale_driver_has_no_resume_authority(project_with_legacy_state: Path) -> None:
    (project_with_legacy_state / "qa/changes/CH-1/driver.json").write_text("not-json")
    report = cleanup_legacy_runtime_state(project_with_legacy_state)
    assert report.status == "completed"
```

- [ ] **Step 2: Write failing adversarial and fault-cut tests**

```python
@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo"])
def test_cleanup_rejects_unsafe_selected_entry(tmp_path: Path, unsafe_kind: str) -> None:
    project = project_with_unsafe_entry(tmp_path, unsafe_kind)
    with pytest.raises(LegacyCleanupError):
        cleanup_legacy_runtime_state(project)


@pytest.mark.parametrize("cut_after", range(8))
def test_cleanup_retries_legal_partial_prefix(project_with_legacy_state: Path, cut_after: int) -> None:
    with injected_unlink_failure(after=cut_after):
        with pytest.raises(LegacyCleanupError):
            cleanup_legacy_runtime_state(project_with_legacy_state)
    report = cleanup_legacy_runtime_state(project_with_legacy_state)
    assert report.status == "completed"
    assert inspect_legacy_runtime_state(project_with_legacy_state) == ()
```

- [ ] **Step 3: Run tests and verify no mutating function exists**

Run: `uv run pytest tests/phase6/test_legacy_cleanup.py tests/phase6/test_legacy_cleanup_faults.py -q`

Expected: FAIL because `cleanup_legacy_runtime_state` is missing.

- [ ] **Step 4: Implement validate-then-delete using directory descriptors**

```python
def cleanup_legacy_runtime_state(project_dir: Path) -> LegacyCleanupReportV1:
    inspected = inspect_legacy_runtime_state(project_dir)
    reject_live_legacy_activity(project_dir)
    grouped = group_entries_by_change(inspected)
    for change_relative, entries in grouped:
        with open_change_directory(project_dir, change_relative) as change_fd:
            revalidate_entries(change_fd, entries)
            for entry in sorted(entries, key=deletion_order):
                delete_entry_at(change_fd, entry)
            os.fsync(change_fd)
    return completed_cleanup_report(project_dir, inspected)
```

Delete files with descriptor-relative `os.unlink`. Delete `.graph-runtime` through a private no-follow recursive walker that validates each inode immediately before unlink/rmdir and never crosses `st_dev`. Treat `ENOENT` as an idempotent success; convert every other race or OS error to `LegacyCleanupError`.

`reject_live_legacy_activity()` parses only a valid direct `driver.json` PID and valid `running-tasks.json` lease PIDs. A process proven live blocks deletion. Dead PIDs, malformed/incomplete pointers, and invalid legacy authority are selected for deletion; the completed Task 2 release audit remains the precondition that rules out unresolved old activity before shipping this behavior.

- [ ] **Step 5: Verify deletion, fault, and existing export security regressions**

Run:

```bash
uv run pytest \
  tests/phase6/test_legacy_cleanup.py \
  tests/phase6/test_legacy_cleanup_faults.py \
  tests/phase5/test_export_security.py -q
uv run ruff check packages/assurance-product/assurance_product/legacy_cleanup.py tests/phase6
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/assurance-product/assurance_product/legacy_cleanup.py tests/phase6/test_legacy_cleanup.py tests/phase6/test_legacy_cleanup_faults.py
git commit -m "feat(product): purge legacy runtime state"
```

---

### Task 5: Bind Cleanup Evidence into New Invocation Start

**Files:**
- Modify: `packages/assurance-product/assurance_product/models.py`
- Modify: `packages/assurance-product/assurance_product/cli.py`
- Create: `packages/assurance-product/assurance_product/resources/schemas/invocation-root-input-v1.json`
- Modify: `tests/phase5/test_cli_lifecycle.py`
- Create: `tests/phase6/test_preseed_cleanup.py`
- Modify: `tests/phase5/cli_support.py`

**Interfaces:**
- Consumes: `cleanup_legacy_runtime_state()` and `LegacyCleanupReportV1`.
- Produces: `AssuranceRootInputV1`, `_capture_seed(project_dir: Path, product_input: ProductInputV1, cleanup: LegacyCleanupReportV1) -> InvocationSeed`, start JSON fields `legacy_cleanup` and `legacy_cleanup_digest`.
- Consumers: status/replay through existing root-input digest and Task 12 benchmark validation.

- [ ] **Step 1: Write failing order and first-run-only tests**

```python
def test_start_cleans_before_seed_capture(cli_runner, lifecycle_fixture, monkeypatch) -> None:
    observed: list[str] = []
    monkeypatch.setattr(cli, "cleanup_legacy_runtime_state", recording_cleanup(observed))
    monkeypatch.setattr(cli, "capture_workspace_seed", recording_capture(observed))
    result = cli_runner.invoke(app, ["start", *lifecycle_fixture.args])
    assert result.exit_code == 0, result.output
    assert observed == ["cleanup", "capture"]


def test_repeated_run_does_not_touch_project(cli_runner, completed_invocation, monkeypatch) -> None:
    monkeypatch.setattr(cli, "cleanup_legacy_runtime_state", forbidden_call)
    result = cli_runner.invoke(app, ["run", *completed_invocation.args])
    assert result.exit_code == 0, result.output
```

Build `lifecycle_fixture` and `completed_invocation` in this test from the retained `tests.phase5.cli_support.common_lifecycle_args`, `scripted_engine_factory`, and `installed_sources` fixtures. Define `recording_cleanup`, `recording_capture`, and `forbidden_call` as test-local callables; `recording_cleanup` returns a valid empty `LegacyCleanupReportV1` and `recording_capture` delegates to the real capture function after recording order.

- [ ] **Step 2: Write failing root-input binding test**

```python
def test_cleanup_report_is_bound_into_root_input(cli_runner, lifecycle_fixture) -> None:
    result = cli_runner.invoke(app, ["start", *lifecycle_fixture.args])
    document = parse_json_output(result.stdout)
    started = invocation_started_event(lifecycle_fixture.engine_root, lifecycle_fixture.invocation_id)
    root = AssuranceRootInputV1.model_validate(started.root_input)
    assert document["legacy_cleanup"] == root.legacy_cleanup.model_dump(mode="json")
    assert document["legacy_cleanup_digest"] == legacy_cleanup_digest(root.legacy_cleanup)
```

- [ ] **Step 3: Run tests and verify current start captures without cleanup**

Run: `uv run pytest tests/phase6/test_preseed_cleanup.py tests/phase5/test_cli_lifecycle.py -q`

Expected: FAIL because cleanup is not invoked and `AssuranceRootInputV1` is absent.

- [ ] **Step 4: Add the runtime-generated root-input model**

```python
class AssuranceRootInputV1(ProductInputV1):
    legacy_cleanup: LegacyCleanupReportV1


def make_root_input(product_input: ProductInputV1, cleanup: LegacyCleanupReportV1) -> AssuranceRootInputV1:
    return AssuranceRootInputV1.model_validate(
        {**product_input.model_dump(mode="json"), "legacy_cleanup": cleanup.model_dump(mode="json")}
    )
```

Generate and commit a strict JSON schema with `additionalProperties: false`. Keep all existing top-level product fields so current workflow JSON pointers remain unchanged.

- [ ] **Step 5: Integrate cleanup only into new invocation paths**

```python
cleanup = cleanup_legacy_runtime_state(project_dir)
seed = _capture_seed(project_dir, product_input, cleanup)
with create_engine(engine_root, authorization) as engine:
    with engine.start(
        composition,
        entrypoint=entrypoint,
        invocation_id=invocation_id,
        seed=seed,
        authorization=authorization,
    ):
        pass
document = {
    "invocation_id": invocation_id,
    "lock_digest": composition.lock_digest,
    "composition_digest": composition.digest,
    "seed_tree_id": seed.workspace.tree_id,
    "root_input_digest": seed.root_input_digest,
    "legacy_cleanup": cleanup.model_dump(mode="json"),
    "legacy_cleanup_digest": legacy_cleanup_digest(cleanup),
}
```

Call this sequence in `_start_invocation` and only the `exists is False` branch of `_run_invocation`. If cleanup fails, convert it to `CommandError`, do not call seed capture, and do not create an invocation root.

- [ ] **Step 6: Run focused graph/bootstrap regressions and commit**

Run:

```bash
uv run pytest \
  tests/phase6/test_preseed_cleanup.py \
  tests/phase5/test_cli_lifecycle.py \
  packages/graph-engine/tests/runtime/test_invocation_seed.py \
  packages/graph-engine/tests/runtime/test_engine_bootstrap_faults.py -q
uv run ruff check packages/assurance-product/assurance_product tests/phase6/test_preseed_cleanup.py
```

Expected: PASS.

```bash
git add packages/assurance-product/assurance_product/models.py packages/assurance-product/assurance_product/cli.py packages/assurance-product/assurance_product/resources/schemas/invocation-root-input-v1.json tests/phase5/test_cli_lifecycle.py tests/phase5/cli_support.py tests/phase6/test_preseed_cleanup.py
git commit -m "feat(product): clean legacy state before seed"
```

---

### Task 6: Transfer `aa` Ownership and Make the Root Workspace Non-Publishable

**Files:**
- Modify: `packages/assurance-product/pyproject.toml`
- Modify: `packages/assurance-product/assurance_product/cli.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/phase5/test_product_packaging.py`
- Create: `tests/phase6/test_cli_cutover.py`
- Create: `tests/phase6/test_workspace_manifest.py`

**Interfaces:**
- Consumes: final Phase 5 product CLI and Task 5 start behavior.
- Produces: exactly one `console_scripts` entry `aa = assurance_product.cli:main`, `main()` with `prog_name="aa"`, a root `[tool.uv] package = false` workspace, and no root build/entry-point metadata.
- Consumers: all final scripts, docs, wheel tests, and benchmarks.

- [ ] **Step 1: Write failing console-script and workspace tests**

```python
def test_product_wheel_owns_only_final_aa(built_product_wheel: Path) -> None:
    metadata = read_wheel_metadata(built_product_wheel)
    assert metadata.entry_points["console_scripts"] == {"aa": "assurance_product.cli:main"}
    assert "aa-next" not in metadata.entry_points["console_scripts"]


def test_root_is_not_a_publishable_distribution(repo_root: Path) -> None:
    config = tomllib.loads((repo_root / "pyproject.toml").read_text())
    assert config["tool"]["uv"]["package"] is False
    assert "build-system" not in config
    assert "scripts" not in config.get("project", {})
    assert "assurance_agent.products" not in config.get("project", {}).get("entry-points", {})
```

- [ ] **Step 2: Run tests and verify the duplicate/old ownership**

Run: `uv run pytest tests/phase6/test_cli_cutover.py tests/phase6/test_workspace_manifest.py tests/phase5/test_product_packaging.py -q`

Expected: FAIL because the root wheel owns `aa` and the product wheel owns `aa-next`.

- [ ] **Step 3: Switch the product script and Click identity**

```toml
[project.scripts]
aa = "assurance_product.cli:main"
```

```python
def main() -> None:
    app.main(prog_name="aa")


@click.group(context_settings={"help_option_names": ["--help"]})
def app() -> None:
    """aa — authenticated Assurance graph product."""
```

- [ ] **Step 4: Convert root metadata to a virtual workspace**

Keep the root `[project]` only as a private development environment named `assurance-workspace`, remove its dependencies/scripts/entry points/Hatch build tables, and add:

```toml
[tool.uv]
package = false
```

Keep `assurance-product`, both adapters, graph engine, six capability wheels, and test tools in the development dependency group. Keep the still-unremoved `assurance-kernel` workspace/source/type entries temporarily; Task 9 deletes them after replacement coverage is re-run. Run `uv lock` and inspect the lock to confirm no root `assurance-agent` package is generated.

- [ ] **Step 5: Verify command ownership and commit**

Run:

```bash
uv sync --dev
uv run aa --help
uv run pytest tests/phase6/test_cli_cutover.py tests/phase6/test_workspace_manifest.py tests/phase5/test_product_packaging.py -q
uv run ruff check packages/assurance-product/assurance_product/cli.py tests/phase6
```

Expected: help begins with `Usage: aa`; `uv run aa-next --help` cannot resolve; all focused tests pass.

```bash
git add packages/assurance-product/pyproject.toml packages/assurance-product/assurance_product/cli.py pyproject.toml uv.lock tests/phase5/test_product_packaging.py tests/phase6/test_cli_cutover.py tests/phase6/test_workspace_manifest.py
git commit -m "feat(cli): transfer aa to assurance product"
```

---

### Task 7: Convert the Committed-HEAD Wheel Smoke to the Final Product

**Files:**
- Modify: `scripts/assurance_product_wheel_smoke_test.sh`
- Modify: `tests/phase5/test_wheel_smoke_contract.py`
- Create: `tests/phase6/test_final_wheel_metadata.py`
- Delete: `scripts/packaging_smoke_test.sh`

**Interfaces:**
- Consumes: Task 6 final product and root workspace metadata.
- Produces: one committed-HEAD smoke that builds the 11 final source wheels, installs base/OpenCode/Cursor/both-adapter matrices, asserts only `aa`, and rejects both legacy distributions/modules/paths.

- [ ] **Step 1: Write failing smoke-contract assertions**

```python
def test_final_smoke_requires_aa_and_forbids_aa_next(repo_root: Path) -> None:
    source = (repo_root / "scripts/assurance_product_wheel_smoke_test.sh").read_text()
    assert '"$prefix/bin/aa" compile' in source
    assert "aa-next" not in source
    assert "assurance-agent" in source
    assert "assurance-kernel" in source
    assert not (repo_root / "scripts/packaging_smoke_test.sh").exists()
```

- [ ] **Step 2: Run tests and verify the Phase 5 script still expects `aa-next`**

Run: `uv run pytest tests/phase5/test_wheel_smoke_contract.py tests/phase6/test_final_wheel_metadata.py -q`

Expected: FAIL on stale script name and legacy packaging script presence.

- [ ] **Step 3: Rewrite archive and install assertions**

In the embedded wheel checker require:

```python
if dist_name == "assurance-product":
    assert console_scripts == {"aa": "assurance_product.cli:main"}
    assert "aa-next" not in entry_points_text
for forbidden in ("assurance-agent", "assurance-kernel"):
    assert forbidden not in installed_names
for module in ("assurance_agent", "assurance_kernel"):
    assert util.find_spec(module) is None
```

Delete `scripts/packaging_smoke_test.sh`; its legacy wheel assertions must not be forwarded.

- [ ] **Step 4: Run focused tests and the committed-HEAD smoke**

Commit the task change before the smoke because the script intentionally builds `git archive HEAD`, then run:

```bash
git add scripts/assurance_product_wheel_smoke_test.sh scripts/packaging_smoke_test.sh tests/phase5/test_wheel_smoke_contract.py tests/phase6/test_final_wheel_metadata.py
git commit -m "test(packaging): verify final aa wheel isolation"
bash scripts/assurance_product_wheel_smoke_test.sh
```

Expected: all wheel archives, base-no-adapter, OpenCode, Cursor, both-installed selection, and source-drift probes pass; no legacy wheel/module/path or `aa-next` is present.

- [ ] **Step 5: Record smoke evidence**

Write the exact commit SHA, wheel filenames/digests, install matrix, and terminal markers to `task-7-report.md`. If the committed-HEAD smoke fails, fix in a new task-local commit and rerun from the new HEAD.

---

### Task 8: Delete the Legacy `assurance_agent` Distribution and Source

**Files:**
- Delete: `assurance_agent/`
- Delete: legacy-only files enumerated under `tests/` by `deletion-inventory.json`
- Delete: legacy-only examples and scripts assigned to `assurance_agent` by `deletion-inventory.json`
- Modify: `pyproject.toml`
- Modify: `.importlinter`
- Create: `tests/phase6/test_assurance_agent_deleted.py`

**Interfaces:**
- Consumes: Task 1 exact deletion/replacement inventory and passing six-wheel/product tests.
- Produces: a repository with no production `assurance_agent` path, import, build target, entry point, command, resource, or unmapped legacy-only test.
- Consumers: Task 10 no-legacy gate and final wheel smoke.

- [ ] **Step 1: Write the failing deletion proof**

```python
def test_assurance_agent_source_and_metadata_are_absent(repo_root: Path) -> None:
    assert not (repo_root / "assurance_agent").exists()
    assert not any("assurance_agent" in path.read_text(errors="ignore") for path in production_runtime_files(repo_root))
    config = tomllib.loads((repo_root / "pyproject.toml").read_text())
    assert "assurance_agent" not in json.dumps(config)


def test_deleted_agent_tests_have_replacement_evidence(deletion_inventory) -> None:
    deleted_tests = [
        item
        for item in deletion_inventory.items
        if item.kind == "test" and item.owner == "assurance-agent"
    ]
    assert deleted_tests
    assert all(item.proof.startswith("tests/") or item.disposition == "obsolete" for item in deleted_tests)
```

Define `production_runtime_files(repo_root)` in `tests.phase6.conformance` as the exact final production package and root/package metadata files; documentation, examples, benchmarks, historical specs, and frozen evidence are outside this Task 8 assertion.

- [ ] **Step 2: Run tests and capture the expected source-presence failure**

Run: `uv run pytest tests/phase6/test_assurance_agent_deleted.py -q`

Expected: FAIL because `assurance_agent/` still exists.

- [ ] **Step 3: Re-run replacement tests before deletion**

Run the exact replacement test paths recorded in `deletion-inventory.json`, plus:

```bash
uv run pytest \
  tests/phase4 \
  tests/phase5/test_full_graph_audit.py \
  tests/phase5/test_generation_branches.py \
  tests/phase5/test_execution_quality_flow.py \
  tests/phase5/test_issue_healing_flow.py \
  tests/phase5/test_archive_retro_improvement.py -q
```

Expected: PASS; every retained behavior is owned by a final wheel/product test before old source deletion.

- [ ] **Step 4: Delete only inventory-approved agent paths**

Remove the complete `assurance_agent/` root and each agent-owned legacy test/example/script in the frozen inventory. Remove root pytest/type/import-linter entries that point at deleted paths. Do not move old implementation into `assurance_product`.

- [ ] **Step 5: Verify product behavior without the source root**

Run:

```bash
uv run pytest \
  tests/phase6/test_assurance_agent_deleted.py \
  tests/phase4 \
  tests/phase5 -q
uv run pyright packages/assurance-product packages/assurance-intake packages/assurance-generation packages/assurance-execution packages/assurance-healing packages/assurance-quality packages/assurance-improvement tests/phase4 tests/phase5 tests/phase6
```

Expected: PASS; no retained test imports `assurance_agent`.

- [ ] **Step 6: Commit the deletion**

```bash
git add -A assurance_agent tests pyproject.toml .importlinter examples scripts
git commit -m "refactor(phase6): delete legacy assurance agent"
```

Inspect `git diff --cached --name-only` before committing and unstage any path not present in the frozen inventory or this task's declared configuration files.

---

### Task 9: Delete `assurance-kernel` and All Old Runtime Ownership

**Files:**
- Delete: `packages/assurance-kernel/`
- Delete: kernel-only tests/examples/resources enumerated by `deletion-inventory.json`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.importlinter`
- Create: `tests/phase6/test_assurance_kernel_deleted.py`

**Interfaces:**
- Consumes: Task 8 repository and Task 1 kernel deletion/replacement inventory.
- Produces: no `assurance-kernel` distribution, `assurance_kernel` module, ProductHooks, old graph/driver/checkpoint/operation/precommit/DSL catalog, forwarding import, or dependency reference.
- Consumers: Tasks 10, 15, 16, and 17.

- [ ] **Step 1: Write the failing kernel deletion proof**

```python
def test_kernel_source_workspace_and_lock_are_absent(repo_root: Path) -> None:
    assert not (repo_root / "packages/assurance-kernel").exists()
    config = tomllib.loads((repo_root / "pyproject.toml").read_text())
    serialized = json.dumps(config, sort_keys=True)
    assert "assurance-kernel" not in serialized
    assert "assurance_kernel" not in serialized
    assert 'name = "assurance-kernel"' not in (repo_root / "uv.lock").read_text()
```

- [ ] **Step 2: Run the test and capture the expected package-presence failure**

Run: `uv run pytest tests/phase6/test_assurance_kernel_deleted.py -q`

Expected: FAIL because `packages/assurance-kernel/` exists.

- [ ] **Step 3: Prove final engine/capability replacements before deletion**

Run:

```bash
uv run pytest \
  packages/graph-engine/tests \
  packages/agent-runtime-contracts/tests \
  packages/agent-runtime-opencode/tests \
  packages/agent-runtime-cursor/tests \
  tests/phase4 \
  tests/phase5 -q
```

Expected: PASS without importing the kernel from retained production/test paths.

- [ ] **Step 4: Delete kernel and rebuild workspace metadata**

Remove `packages/assurance-kernel/` and every kernel-only path in the inventory. Remove source mappings, test paths, Pyright paths, import contracts, docs commands, and build references. Run:

```bash
uv lock
uv sync --dev
```

Expected: dependency resolution succeeds with graph-engine and the final product closure only.

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run pytest tests/phase6/test_assurance_kernel_deleted.py packages/graph-engine/tests tests/phase4 tests/phase5 -q
uv run pyright
uv run lint-imports
```

Expected: PASS.

```bash
git add -A packages/assurance-kernel tests pyproject.toml uv.lock .importlinter examples scripts
git commit -m "refactor(phase6): delete legacy assurance kernel"
```

Inspect the staged path list against the frozen inventory before committing.

---

### Task 10: Enforce the No-Legacy Architecture Gate

**Files:**
- Create: `scripts/check_no_legacy.py`
- Create: `scripts/no_legacy_allowlist.txt`
- Create: `tests/phase6/test_no_legacy_gate.py`
- Modify: `scripts/assurance_product_wheel_smoke_test.sh`

**Interfaces:**
- Consumes: post-Task-9 repository and wheel metadata.
- Produces: `scan_repository(root: Path, allowlist: tuple[Path, ...], scope: Literal["runtime", "repository"]) -> tuple[Violation, ...]`; runtime scope rejects production packages/imports/entry points/default graphs, and repository scope additionally rejects current commands, benchmarks, examples, CI, and documentation.

- [ ] **Step 1: Write failing mutation tests for every forbidden seam**

```python
@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("packages/assurance-product/assurance_product/bad.py", "import assurance_agent\n"),
        ("packages/graph-engine/graph_engine/bad.py", "import assurance_kernel\n"),
        ("packages/assurance-product/pyproject.toml", "aa-next = 'x:y'\n"),
        ("packages/graph-engine/graph_engine/default.yaml", "graph: assurance-full\n"),
        ("scripts/bad.sh", "AA_RUNTIME=legacy\n"),
    ],
)
def test_no_legacy_gate_rejects_forbidden_seams(tmp_path: Path, relative: str, source: str) -> None:
    root = minimal_final_tree(tmp_path)
    write(root / relative, source)
    assert scan_repository(root, (), scope="repository")
```

Define `minimal_final_tree()` and `write()` as test-local helpers in `test_no_legacy_gate.py`; the former creates only the scanner's declared production and current-document roots.

- [ ] **Step 2: Run tests and verify the scanner is absent**

Run: `uv run pytest tests/phase6/test_no_legacy_gate.py -q`

Expected: FAIL because `scripts.check_no_legacy` is missing.

- [ ] **Step 3: Implement AST, metadata, path, and current-text scanning**

```python
FORBIDDEN_IMPORT_ROOTS = {"assurance_agent", "assurance_kernel"}
FORBIDDEN_DISTRIBUTIONS = {"assurance-agent", "assurance-kernel"}
FORBIDDEN_CURRENT_TOKENS = {"aa" + "-next", "assurance_agent" + ".products", "Product" + "Hooks"}


def scan_python(path: Path) -> list[Violation]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = imported_roots(tree)
    return [Violation(path, root) for root in sorted(roots & FORBIDDEN_IMPORT_ROOTS)]
```

Scan Python production imports, package/root entry points and dependencies, shell command invocations/runtime selectors, CI, README, AGENTS, examples, and current release docs. Detection tools may construct forbidden names from split literals but cannot import or launch them. Permit historical prose only in exact paths listed in `scripts/no_legacy_allowlist.txt`, consisting of historical specs and frozen Phase 5/6 evidence; reject directory-wide or glob allowlist entries.

- [ ] **Step 4: Add the runtime-scope wheel invocation**

Make the committed-HEAD wheel smoke invoke `check_no_legacy.py --scope runtime` against its archived production source before building. Task 14 runs repository scope after current benchmarks/docs are cut over, and Task 17 adds that full command to CI.

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run pytest tests/phase6/test_no_legacy_gate.py -q
uv run python scripts/check_no_legacy.py --repo . --allowlist scripts/no_legacy_allowlist.txt --scope runtime
uv run ruff check scripts/check_no_legacy.py tests/phase6/test_no_legacy_gate.py
```

Expected: PASS with zero violations.

```bash
git add scripts/check_no_legacy.py scripts/no_legacy_allowlist.txt tests/phase6/test_no_legacy_gate.py scripts/assurance_product_wheel_smoke_test.sh
git commit -m "test(architecture): reject legacy runtime seams"
```

---

### Task 11: Retire the Executable Comparison Baseline and Rename Final Product Tests

**Files:**
- Move: `tests/phase5/` to `tests/product/`
- Delete: `tests/product/fixtures/comparison/legacy/`
- Delete: legacy-comparison-only test files identified by `deletion-inventory.json`
- Move: retained current semantic fixtures to `tests/product/fixtures/expected/`
- Delete: `benchmark/assurance-product-phase5/{compare.py,run_comparison.py,run-comparison.sh,generate_comparison_fixtures.py,comparison-manifest.json}`
- Create: `tests/phase6/test_comparison_retired.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: frozen Phase 5 comparison evidence and deletion inventory.
- Produces: `tests.product` final product test package, new-runtime semantic expectations only, and no executable legacy runner/fixture.
- Consumers: final repository gates and benchmark validation.

- [ ] **Step 1: Write failing retirement assertions**

```python
def test_executable_legacy_comparison_is_absent(repo_root: Path) -> None:
    assert not (repo_root / "tests/phase5/fixtures/comparison/legacy").exists()
    for name in ("compare.py", "run_comparison.py", "run-comparison.sh", "generate_comparison_fixtures.py"):
        assert not (repo_root / "benchmark/assurance-product-phase5" / name).exists()


def test_expected_fixtures_are_new_runtime_only(repo_root: Path) -> None:
    expected = repo_root / "tests/product/fixtures/expected"
    assert expected.is_dir()
    assert not any("legacy" in path.parts for path in expected.rglob("*"))
```

- [ ] **Step 2: Run tests and verify the baseline is still executable**

Run: `uv run pytest tests/phase6/test_comparison_retired.py -q`

Expected: FAIL because legacy comparison files and fixtures remain.

- [ ] **Step 3: Freeze evidence before deletion**

Run `scripts/check_phase6_admission.py` and verify the frozen admission still authenticates Phase 5 comparison dispositions, manifests, and reports. Copy no legacy executable or fixture into a production/final test path.

- [ ] **Step 4: Move retained tests and remove comparison-only assets**

Move product tests/helpers/fixtures from `tests.phase5` to `tests.product`, update imports mechanically, move current semantic fixtures to `fixtures/expected`, and delete the exact comparison-only files. Update root pytest/Pyright paths to `tests/product`.

- [ ] **Step 5: Verify retained semantic coverage and commit**

Run:

```bash
uv run pytest tests/product tests/phase6/test_comparison_retired.py -q
uv run ruff check tests/product tests/phase6/test_comparison_retired.py
uv run pyright tests/product tests/phase6
```

Expected: PASS; comparison equivalence is no longer recalculated from legacy code.

```bash
git add -A tests/phase5 tests/product benchmark/assurance-product-phase5 pyproject.toml tests/phase6/test_comparison_retired.py
git commit -m "refactor(phase6): retire legacy comparison baseline"
```

---

### Task 12: Consolidate the Final Isolated Benchmark

**Files:**
- Move: `benchmark/assurance-product-phase5/run_item.py` to `benchmark/assurance-product/run_item.py`
- Move: `benchmark/assurance-product-phase5/manifest.json` to `benchmark/assurance-product/manifest.json`
- Move: `benchmark/assurance-product-phase5/run-opencode.sh` to `benchmark/assurance-product/run-opencode.sh`
- Create: `benchmark/assurance-product/run-cursor.sh`
- Move: retained projection/export validation code to `benchmark/assurance-product/validation.py`
- Delete: remaining `benchmark/assurance-product-phase5/`
- Create: `tests/phase6/test_final_benchmark.py`
- Create: `tests/phase6/test_benchmark_isolation.py`
- Create: `tests/phase6/benchmark_fixtures.py`

**Interfaces:**
- Consumes: final `aa`, final product manifest, explicit provider binding, project config tree, and run-scoped SUT copy.
- Produces: `BenchmarkFailure`, `run_checked(command: Sequence[str]) -> CompletedProcess[str]`, `run_item(provider: Literal["opencode", "cursor"], item: str, output: Path) -> int`, provider wrappers supporting exact `--item`, `--output`, `--fresh`, and `--preflight-only` flags, result layout `project/`, `engine/`, and `export/`, plus `export-validation.json`.
- Produces for tests: `run_deterministic_item(root: Path) -> Path` and `digest_tree(root: Path) -> str` in `tests.phase6.benchmark_fixtures`.

- [ ] **Step 1: Write failing final-name and layout tests**

```python
def test_final_benchmark_invokes_only_aa(repo_root: Path) -> None:
    source = (repo_root / "benchmark/assurance-product/run_item.py").read_text()
    assert '"aa"' in source
    assert "aa-next" not in source
    assert "assurance-product-phase5" not in source


def test_benchmark_layout_separates_seed_engine_and_export(tmp_path: Path) -> None:
    output = run_deterministic_item(tmp_path)
    assert (output / "project").is_dir()
    assert (output / "engine/invocations").is_dir()
    assert (output / "export/result-tree").is_dir()
    assert (output / "export/manifest.json").is_file()
    assert (output / "export-validation.json").is_file()
```

- [ ] **Step 2: Run tests and verify the final benchmark directory is absent**

Run: `uv run pytest tests/phase6/test_final_benchmark.py tests/phase6/test_benchmark_isolation.py -q`

Expected: FAIL because only the Phase 5 harness exists.

- [ ] **Step 3: Move the harness and replace command identity**

Use final commands only:

```python
start = [aa, "start", *start_args]
run = [aa, "run", *existing_args]
status = [aa, "status", *existing_args, "--json"]
export = [aa, "export", "--destination", str(output / "export"), *existing_args]
```

Reject `aa-next`, the old scripts, ambient model/provider variables, and a benchmark output rooted inside the SUT or engine root.

- [ ] **Step 4: Add isolation assertions around seed capture**

Record the cleaned seed project digest after `aa start`; after every `run/status/export`, recompute it and fail if it changes. Validate actual agent writes only below `engine/invocations/{invocation_id}/workspace/attempts`, committed trees below `workspace/trees`, and authoritative `HEAD.json`.

```python
seed_digest = digest_tree(output / "project")
run_checked(command)
if digest_tree(output / "project") != seed_digest:
    raise BenchmarkFailure("seed project changed after capture")
```

- [ ] **Step 5: Validate the export-only result**

Parse `export/manifest.json` as `ResultExportV1`; authenticate final HEAD, result-tree digest, status, artifact index, selected families, coverage/report/Issue/healing/archive/Retro/Improvement evidence declared by the item, and absence of legacy state/secrets/provider transcript. Do not copy `result-tree` over `project`.

- [ ] **Step 6: Verify deterministic harness tests and commit**

Run:

```bash
uv run pytest tests/phase6/test_final_benchmark.py tests/phase6/test_benchmark_isolation.py -q
uv run ruff check benchmark/assurance-product tests/phase6/test_final_benchmark.py tests/phase6/test_benchmark_isolation.py
```

Expected: PASS without contacting a provider.

```bash
git add -A benchmark/assurance-product-phase5 benchmark/assurance-product tests/phase6/test_final_benchmark.py tests/phase6/test_benchmark_isolation.py tests/phase6/benchmark_fixtures.py
git commit -m "test(benchmark): cut over isolated assurance product"
```

---

### Task 13: Lock Result Delivery to `aa export` Only

**Files:**
- Create: `tests/phase6/test_export_only_delivery.py`
- Modify: `tests/product/test_cli_export.py`
- Modify: `tests/product/test_export_security.py`
- Modify: `packages/assurance-product/assurance_product/export.py` only if a failing acceptance test proves a mismatch
- Modify: `packages/assurance-product/assurance_product/cli.py` only if a failing command-surface test proves a mismatch

**Interfaces:**
- Consumes: authenticated completed invocation and final `aa` command.
- Produces: exact command inventory without apply/write-back and exact fresh export layout `{result-tree,manifest.json,status.json,artifact-index.json}`.

- [ ] **Step 1: Write command-surface and original-project preservation tests**

```python
def test_final_cli_has_no_apply_or_writeback(cli_runner) -> None:
    result = cli_runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "export" in result.output
    assert "apply" not in command_names(result.output)
    assert "publish" not in command_names(result.output)


def test_export_never_changes_original_project(completed_invocation) -> None:
    before = digest_tree(completed_invocation.project_dir)
    exported = export_completed(completed_invocation)
    assert digest_tree(completed_invocation.project_dir) == before
    assert set(path.name for path in exported.iterdir()) == {
        "result-tree", "manifest.json", "status.json", "artifact-index.json"
    }
```

Import `command_names` and `completed_invocation` from the retained `tests.product.cli_support`, and `digest_tree` from `tests.phase6.benchmark_fixtures`. Define test-local `export_completed()` by calling public `export_invocation()` with `completed_invocation.engine`, ID, a fresh sibling destination, and `completed_invocation.authorization`.

- [ ] **Step 2: Add terminal-state and destination matrix tests**

```python
@pytest.mark.parametrize("status", ["running", "interrupted", "stopped", "failed", "indeterminate"])
def test_non_success_never_exports(status: str, invocation_for_status) -> None:
    destination = invocation_for_status(status).root / "export"
    with pytest.raises(ResultExportError):
        export_invocation_for_test(invocation_for_status(status), destination)
    assert not destination.exists()
```

Define `invocation_for_status(status)` and `export_invocation_for_test(invocation, destination)` as test-local helpers using the retained scripted engine/status fixtures; each helper returns an authenticated public `Engine`/invocation pair rather than mutating private projection fields.

Retain symlink, hardlink, destination race, engine-root overlap, invocation-root overlap, original-SUT, existing non-empty destination, staging failure, secret-canary, and ledger/checkpoint/HEAD disagreement cases.

- [ ] **Step 3: Run tests and classify any mismatch**

Run: `uv run pytest tests/phase6/test_export_only_delivery.py tests/product/test_cli_export.py tests/product/test_export_security.py -q`

Expected: PASS if Phase 5 export already meets the final contract; otherwise RED identifies one exact exporter/CLI mismatch.

- [ ] **Step 4: Make only proven export corrections**

If RED exists, keep the final call shape:

```python
exported = export_invocation(engine, invocation_id, destination, authorization=authorization)
```

Do not add an apply mode, overwrite flag, merge flag, latest-invocation lookup, or original project argument to the exporter.

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run pytest tests/phase6/test_export_only_delivery.py tests/product/test_cli_export.py tests/product/test_export_security.py -q
uv run ruff check packages/assurance-product/assurance_product tests/phase6/test_export_only_delivery.py tests/product/test_cli_export.py tests/product/test_export_security.py
```

```bash
git add tests/phase6/test_export_only_delivery.py tests/product/test_cli_export.py tests/product/test_export_security.py packages/assurance-product/assurance_product/export.py packages/assurance-product/assurance_product/cli.py
git commit -m "test(product): enforce export-only delivery"
```

Do not stage exporter/CLI files if no production correction was required.

---

### Task 14: Rewrite Current Documentation, Examples, and Release Instructions

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `packages/assurance-product/README.md`
- Modify: final current architecture/release docs named by `deletion-inventory.json`
- Modify: `examples/assurance-product-deployment/`
- Delete: legacy-only examples and current docs named by `deletion-inventory.json`
- Create: `tests/phase6/test_current_documentation.py`

**Interfaces:**
- Consumes: final command/package/benchmark behavior.
- Produces: one current user/developer story: install explicit product/adapter/binding, run `aa`, inspect status, and export to a fresh destination.

- [ ] **Step 1: Write failing current-document tests**

```python
def test_current_docs_describe_only_final_runtime(repo_root: Path) -> None:
    current = current_document_paths(repo_root)
    text = "\n".join(path.read_text(encoding="utf-8") for path in current)
    assert "aa-next" not in text
    assert "assurance-kernel" not in text
    assert "assurance_agent.products" not in text
    assert "aa apply" not in text
    assert "aa export" in text
    assert "result-tree" in text
```

Define `current_document_paths()` in this test as the exact tuple `(README.md, AGENTS.md, packages/assurance-product/README.md)` plus the current architecture/release paths from the frozen deletion inventory; historical specs are excluded.

- [ ] **Step 2: Run tests and capture stale Phase 5/legacy instructions**

Run: `uv run pytest tests/phase6/test_current_documentation.py -q`

Expected: FAIL on current README/AGENTS/package instructions.

- [ ] **Step 3: Rewrite the developer and user flows**

Document exact commands, deriving generated binding coordinates from `binding.json`:

```bash
uv run aa bindings build --manifest deployment.yaml --output-dir dist-bindings --json > binding.json
binding_dist="$(uv run python -c 'import json; print(json.load(open("binding.json"))["distribution"])')"
binding_decl="$(uv run python -c 'import json; print(json.load(open("binding.json"))["declaration_path"])')"
uv run aa compile --product assurance-opencode --binding-dist "$binding_dist" --binding-entrypoint deployment --binding-declaration "$binding_decl" --config-tree project-config
uv run aa start --project-dir /work/sut --engine-root /work/engine --invocation-id INV-1 --product assurance-opencode --binding-dist "$binding_dist" --binding-entrypoint deployment --binding-declaration "$binding_decl" --config-tree project-config --entrypoint full --input product-input.json --secret runtime.opencode.token=env:OPENCODE_API_KEY
uv run aa run --engine-root /work/engine --invocation-id INV-1 --product assurance-opencode --binding-dist "$binding_dist" --binding-entrypoint deployment --binding-declaration "$binding_decl" --config-tree project-config --secret runtime.opencode.token=env:OPENCODE_API_KEY
uv run aa status --engine-root /work/engine --invocation-id INV-1 --product assurance-opencode --binding-dist "$binding_dist" --binding-entrypoint deployment --binding-declaration "$binding_decl" --config-tree project-config --secret runtime.opencode.token=env:OPENCODE_API_KEY --json
uv run aa export --engine-root /work/engine --invocation-id INV-1 --product assurance-opencode --binding-dist "$binding_dist" --binding-entrypoint deployment --binding-declaration "$binding_decl" --config-tree project-config --secret runtime.opencode.token=env:OPENCODE_API_KEY --destination /work/export --json
```

Explain that cleanup deletes only the closed immediate legacy state set before seed capture, agent writes remain in engine attempt workspaces, `workspace/HEAD.json` is authoritative, and `export/result-tree/` is the only delivered project.

- [ ] **Step 4: Update AGENTS and examples**

State that the workspace ships graph-engine, adapters, six capability wheels, and assurance-product; remove old two-package and legacy deterministic CLI instructions. Keep YAML/data-only vs installed-wheel authority rules and explicitly forbid project-loaded executable plugins/default graphs.

- [ ] **Step 5: Verify docs/no-legacy gate and commit**

Run:

```bash
uv run pytest tests/phase6/test_current_documentation.py -q
uv run python scripts/check_no_legacy.py --repo . --allowlist scripts/no_legacy_allowlist.txt --scope repository
```

Expected: PASS.

```bash
git add README.md AGENTS.md packages/assurance-product/README.md examples docs tests/phase6/test_current_documentation.py
git commit -m "docs: document final pure graph product"
```

Stage only current docs/examples listed by the task; historical specs remain unchanged and exactly allowlisted.

---

### Task 15: Run the Final OpenCode Provider-Live Benchmark

**Files:**
- Modify: `benchmark/assurance-product/manifest.json` only to correct a proven final-name/path mismatch
- Modify: `benchmark/assurance-product/run_item.py` only to correct a high-confidence harness defect
- Create: `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/opencode-benchmark.md`

**Interfaces:**
- Consumes: committed final source, pinned OpenCode deployment binding/model/permission/secret handle, one benchmark item, and a fresh result root.
- Produces: one succeeded authenticated OpenCode invocation, valid export, unchanged post-seed project, and recorded command/status/digest/artifact evidence.

- [ ] **Step 1: Run the deterministic admission and committed-HEAD gates**

Run:

```bash
uv run python scripts/check_phase6_admission.py --repo . --check .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/admission.json
uv run python scripts/check_no_legacy.py --repo . --allowlist scripts/no_legacy_allowlist.txt --scope repository
bash scripts/assurance_product_wheel_smoke_test.sh
```

Expected: all exit 0 before provider contact.

- [ ] **Step 2: Run one pinned OpenCode item from a fresh result root**

Run:

```bash
bash benchmark/assurance-product/run-opencode.sh --item ret-dept-management --output benchmark/assurance-product/results/phase6-opencode-final --fresh
```

Expected: the script creates `results/phase6-opencode-final/{project,engine,export}`, follows the invocation until terminal, exits 0 only on `completed`, and writes `export-validation.json`.

- [ ] **Step 3: Validate the live result independently**

Run:

```bash
uv run python benchmark/assurance-product/validation.py \
  --run-root benchmark/assurance-product/results/phase6-opencode-final
```

Expected: terminal `completed`; manifest/ledger/checkpoint/HEAD/result-tree digests agree; selected families and declared coverage/report/Issue/healing/archive/Retro/Improvement evidence are present; project digest is unchanged after capture; no legacy state, secret, or transcript is exported.

- [ ] **Step 4: Diagnose failures without weakening the gate**

Classify any failure as product, adapter, provider, credential, quota, executable, harness, or generated-business-output. Fix only a reproducible high-confidence source defect in its owner, add a failing deterministic regression first, rerun focused tests, commit, and restart the live item from a fresh result root. Do not increase the accepted timeout, change model, add fallback, reuse a partial export, or mark blocked provider state as passed.

- [ ] **Step 5: Record and commit OpenCode evidence**

Record exact source commit, command, model route, binding/config/lock/workflow/input/seed/final-tree/export digests, status, selected families, coverage rounds, logical steps, artifact IDs, durations, and redacted blocker/fix history.

```bash
git add benchmark/assurance-product/manifest.json benchmark/assurance-product/run_item.py
git add -f .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/opencode-benchmark.md
git commit -m "test(phase6): verify final OpenCode workflow"
```

Do not stage manifest/runner files if no source correction occurred.

---

### Task 16: Run the Final Cursor Provider-Live Benchmark

**Files:**
- Modify: `benchmark/assurance-product/manifest.json` only to correct a proven final-name/path mismatch
- Modify: `benchmark/assurance-product/run_item.py` only to correct a high-confidence harness defect
- Create: `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/cursor-benchmark.md`

**Interfaces:**
- Consumes: committed final source, pinned Cursor executable/version/model/permission/secret handle, one benchmark item, and a fresh result root.
- Produces: one succeeded authenticated Cursor invocation and the same export/isolation evidence class as Task 15.

- [ ] **Step 1: Verify Cursor preflight identity**

Run:

```bash
bash benchmark/assurance-product/run-cursor.sh --item ret-dept-management --output benchmark/assurance-product/results/phase6-cursor-final --preflight-only
```

The command authenticates the exact executable path, executable digest, version, confined PATH, API-key handle, permission profile, and model route from the installed binding wheel without creating an invocation.

Expected: selected product is `assurance-cursor`, exactly one Cursor adapter is in the frozen composition, OpenCode is unselected, and no ambient override is accepted.

- [ ] **Step 2: Run one pinned Cursor item from a fresh result root**

Run:

```bash
bash benchmark/assurance-product/run-cursor.sh --item ret-dept-management --output benchmark/assurance-product/results/phase6-cursor-final --fresh
```

Expected: the script creates `results/phase6-cursor-final/{project,engine,export}`, follows the invocation until terminal, exits 0 only on `completed`, and writes `export-validation.json`.

- [ ] **Step 3: Validate the result independently**

Run:

```bash
uv run python benchmark/assurance-product/validation.py \
  --run-root benchmark/assurance-product/results/phase6-cursor-final
```

Expected: the same authenticated terminal, business, isolation, export, and no-secret checks as Task 15 pass.

- [ ] **Step 4: Diagnose without substituting credentials or providers**

For nonzero exit, use the exact typed category and bounded process evidence. Add deterministic regression coverage before a source fix. Do not invent credentials, use another model, select OpenCode, relax executable/version/confinement identity, reinterpret absent initialization as success, or raise the accepted horizon.

- [ ] **Step 5: Record and commit Cursor evidence**

Record exact source commit, executable/version/model route, binding/config/lock/workflow/input/seed/final-tree/export digests, status, selected families, coverage rounds, logical steps, artifact IDs, durations, and redacted blocker/fix history.

```bash
git add benchmark/assurance-product/manifest.json benchmark/assurance-product/run_item.py
git add -f .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/cursor-benchmark.md
git commit -m "test(phase6): verify final Cursor workflow"
```

Do not stage manifest/runner files if no source correction occurred.

---

### Task 17: Run Full Security, Fault, Business, and Repository Gates

**Files:**
- Create: `tests/phase6/test_final_acceptance.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/deletion-inventory.json` only to mark already-proven items `verified`

**Interfaces:**
- Consumes: Tasks 1-16 committed final source/evidence.
- Produces: one deterministic final acceptance suite and zero unverified deletion items.

- [ ] **Step 1: Write the final acceptance test over public evidence**

```python
def test_phase6_acceptance(repo_root: Path, deletion_inventory) -> None:
    assert all(item.status == "verified" for item in deletion_inventory.items)
    assert not (repo_root / "assurance_agent").exists()
    assert not (repo_root / "packages/assurance-kernel").exists()
    assert not (repo_root / "benchmark/assurance-product-phase5").exists()
    assert final_console_scripts(repo_root) == {"aa": "assurance_product.cli:main"}
    assert provider_evidence(repo_root, "opencode").status == "completed"
    assert provider_evidence(repo_root, "cursor").status == "completed"
```

Add `final_console_scripts(repo_root: Path) -> dict[str, str]` and `provider_evidence(repo_root: Path, provider: Literal["opencode", "cursor"]) -> ProviderEvidenceV1` to `tests.phase6.conformance`. The former reads the built product wheel metadata; the latter strictly parses the committed Task 15/16 evidence and authenticates its export-validation digest.

```python
class ProviderEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["opencode", "cursor"]
    status: Literal["completed"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    export_validation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
```

- [ ] **Step 2: Run package-focused test suites**

Run:

```bash
uv run pytest \
  packages/graph-engine/tests \
  packages/agent-runtime-contracts/tests \
  packages/agent-runtime-opencode/tests \
  packages/agent-runtime-cursor/tests \
  packages/assurance-intake/tests \
  packages/assurance-generation/tests \
  packages/assurance-execution/tests \
  packages/assurance-healing/tests \
  packages/assurance-quality/tests \
  packages/assurance-improvement/tests \
  tests/product \
  tests/phase6 -q
```

Expected: zero failures; any changed count is explained in `task-17-report.md` by deleted legacy tests or added final tests.

- [ ] **Step 3: Run static and formatting gates**

Add the final repository-scope check to CI before pytest/build:

```yaml
- name: Reject legacy runtime seams
  run: uv run python scripts/check_no_legacy.py --repo . --allowlist scripts/no_legacy_allowlist.txt --scope repository
```

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run python scripts/check_no_legacy.py --repo . --allowlist scripts/no_legacy_allowlist.txt --scope repository
git diff --check
```

Expected: all exit 0 with no violations.

- [ ] **Step 4: Run the full suite and committed-HEAD wheel smoke**

Run:

```bash
uv run pytest
bash scripts/assurance_product_wheel_smoke_test.sh
```

Expected: zero test failures and all wheel/install/source-authentication markers pass.

- [ ] **Step 5: Verify business closure explicitly**

Run:

```bash
uv run pytest \
  tests/product/test_selected_family_join.py \
  tests/product/test_execution_quality_flow.py \
  tests/product/test_coverage_loop.py \
  tests/product/test_report_flow.py \
  tests/product/test_issue_healing_flow.py \
  tests/product/test_archive_retro_improvement.py \
  tests/product/test_stop_and_interrupts.py \
  tests/product/test_result_export.py \
  tests/product/test_export_security.py -q
```

Expected: PASS; no selected API/E2E/Fuzz/Performance branch is silently inactive and every expected artifact passes typed/cross-artifact validation.

- [ ] **Step 6: Mark inventory verified and commit**

Only after the mapped test/gate succeeds, set each deletion inventory item to `status: verified` with its exact proof path/command.

```bash
git add tests/phase6/test_final_acceptance.py .github/workflows/ci.yml .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/deletion-inventory.json
git commit -m "test(phase6): close final release gates"
```

Rerun Steps 2-4 from the committed HEAD.

---

### Task 18: Publish Final Cutover and Deletion Evidence

**Files:**
- Create: `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/final-evidence.json`
- Create: `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/acceptance.md`
- Create: `scripts/freeze_phase6_evidence.py`
- Modify: `tests/phase6/conformance.py`
- Modify: `tests/phase6/test_final_acceptance.py`
- Modify: `.superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/admission.json` only to bind the final cutover commit/evidence digest
- Modify: `README.md` only if the final evidence reveals an exact command/digest documentation mismatch

**Interfaces:**
- Consumes: final committed source, Task 1 admission, Task 2 zero-activity audit, verified deletion inventory, Task 15/16 live reports, Task 17 gates, and final wheel artifacts.
- Produces: immutable `FinalEvidenceV1`, `collect_release_digests(repo: Path, required: Mapping[str, Path]) -> dict[str, str]`, and a freeze command proving command ownership, package closure, cleanup, isolation, export, both providers, and zero legacy seams.

- [ ] **Step 1: Write the failing final-evidence verifier**

```python
def test_final_evidence_authenticates_every_release_gate(evidence_root: Path) -> None:
    evidence = FinalEvidenceV1.model_validate_json((evidence_root / "final-evidence.json").read_text())
    assert evidence.schema_version == "1"
    assert evidence.console_scripts == {"aa": "assurance_product.cli:main"}
    assert evidence.legacy_distributions == ()
    assert evidence.legacy_modules == ()
    assert evidence.provider_live == {"opencode": "completed", "cursor": "completed"}
    assert evidence.delivery_commands == ("export",)
    assert all(value.startswith("sha256:") for value in evidence.digests.values())
```

- [ ] **Step 2: Run the verifier and capture the missing-evidence failure**

Run: `uv run pytest tests/phase6/test_final_acceptance.py -q`

Expected: FAIL until `final-evidence.json` exists and authenticates the committed final artifacts.

- [ ] **Step 3: Define the strict evidence model and required digest collector**

```python
class FinalEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    console_scripts: dict[Literal["aa"], Literal["assurance_product.cli:main"]]
    legacy_distributions: tuple[str, ...] = ()
    legacy_modules: tuple[str, ...] = ()
    provider_live: dict[Literal["opencode", "cursor"], Literal["completed"]]
    delivery_commands: tuple[Literal["export"]]
    digests: dict[str, str]


def collect_release_digests(repo: Path, required: Mapping[str, Path]) -> dict[str, str]:
    missing = [name for name, path in required.items() if not (repo / path).is_file()]
    if missing:
        raise EvidenceError(f"missing release evidence: {sorted(missing)}")
    return {
        name: "sha256:" + hashlib.sha256((repo / path).read_bytes()).hexdigest()
        for name, path in sorted(required.items())
    }
```

The required mapping contains exact paths for the final product/capability/adapter/binding wheels, product declarations, workflow, admission, activity audit, deletion inventory, both benchmark manifests/exports, wheel smoke log, and Task 17 gate log.

- [ ] **Step 4: Generate final evidence from verified sources**

Run:

```bash
uv run python scripts/freeze_phase6_evidence.py --repo . --output .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/final-evidence.json
```

Expected: the command rejects any missing/unhashed input and otherwise writes canonical `FinalEvidenceV1` with exact source commit, `aa` command, empty legacy sets, both completed providers, delivery command `export`, and all required digests.

- [ ] **Step 5: Write the acceptance narrative**

Record the exact cutover order, zero-activity proof, directly deleted state set, preserved data set, removed package/code/test counts, final wheel dependency graph, command mapping, both live outcomes, export paths/digests, repository gates, rollback boundary, and confirmation that no apply/compatibility/default graph exists.

- [ ] **Step 6: Verify final committed evidence and commit**

Run:

```bash
uv run pytest tests/phase6/test_final_acceptance.py -q
uv run python scripts/check_phase6_admission.py --repo . --check .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/admission.json
uv run python scripts/check_no_legacy.py --repo . --allowlist scripts/no_legacy_allowlist.txt --scope repository
git diff --check
```

Expected: PASS.

```bash
git add scripts/freeze_phase6_evidence.py tests/phase6/conformance.py tests/phase6/test_final_acceptance.py README.md
git add -f .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/final-evidence.json .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/acceptance.md .superpowers/sdd/2026-08-23-pure-graph-engine-phase6-hard-cutover/admission.json
git commit -m "chore(phase6): publish hard cutover evidence"
```

Do not stage README if it required no correction. From the final commit rerun the full Task 17 gate set and both evidence validators; Phase 6 is complete only when those fresh commands pass.

---

## Final Execution Checklist

- [ ] Phase 5 handoff is complete and both original provider-live gates are successful.
- [ ] Legacy activity audit is zero-live and zero-unresolved.
- [ ] Closed legacy state cleanup is safe, direct, exact-depth, idempotent, and pre-seed.
- [ ] Cleanup evidence is bound into root input and start output.
- [ ] `assurance-product` alone owns `aa`; `aa-next` is absent.
- [ ] Root is a non-publishable workspace.
- [ ] `assurance_agent` and `assurance_kernel` source/distributions/imports are absent.
- [ ] ProductHooks, catalogs, old runtime, forwarding imports, resources, and obsolete tests are deleted.
- [ ] Executable legacy comparison and legacy fixtures are retired after evidence freeze.
- [ ] Final benchmark writes only engine attempts/snapshots and exports to a fresh result tree.
- [ ] `aa export` is the only delivery command; no apply/write-back path exists.
- [ ] Current docs/examples describe only the final runtime.
- [ ] Final OpenCode benchmark completes and validates.
- [ ] Final Cursor benchmark completes and validates.
- [ ] Full tests, static gates, no-legacy gate, and committed-HEAD wheel smoke pass.
- [ ] Final cutover/deletion evidence authenticates the released commit and artifacts.
