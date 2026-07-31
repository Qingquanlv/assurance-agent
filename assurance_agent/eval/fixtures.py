"""Eval fixture tiers and change seeding.

Semantic port of the TypeScript ``seed_change`` / ``fixture_utils`` flow:
load a tier manifest (with ``extends`` chain), copy golden sample paths into
an isolated SUT sandbox, then reset ``workflow-state.yaml`` via
``read_state`` / ``write_state`` so integrity hashes stay valid for audits.

v2: tiers may declare structural ``imports`` per entrypoint; seeding hashes
inputs/outputs/gates and writes ``.graph-runtime/import-manifest.yaml``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance_agent.eval.types import SeedResult
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.core.state import read_state_lenient, write_state
from assurance_agent.workflow.execution.tree_hash import sha256_file
from assurance_agent.workflow.orchestration.gates import (
    GateEvaluationContext,
    check_gate_in_view,
)


class FixtureResets(BaseModel):
    workflow_state: dict[str, Any] = Field(default_factory=dict)
    qa_yaml: dict[str, Any] = Field(default_factory=dict)


class FixtureImportTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    graph: str
    node: str
    task_key: str | None = None
    outputs: list[str] = Field(default_factory=list)
    gate: str | None = None


class FixtureImportDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entrypoint: str
    inputs: list[str] = Field(default_factory=list)
    completed: list[FixtureImportTask] = Field(default_factory=list)


class TierManifest(BaseModel):
    name: str
    extends: str | None = None
    description: str = ""
    paths: list[str] = Field(default_factory=list)
    resets: FixtureResets = Field(default_factory=FixtureResets)
    source_prefix: str | None = None
    imports: dict[str, FixtureImportDef] = Field(default_factory=dict)


class FixtureLockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_dir: str
    aggregate_sha256: str
    files: dict[str, str]


class FixtureLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    fixtures: dict[str, FixtureLockEntry]


_ASSURANCE_SEED_LAYERS = ("fuzz", "performance")


def _fixture_entry(fixtures_root: Path, sample_dir: str) -> FixtureLockEntry:
    relative = Path(sample_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise AaError(f"unsafe fixture sample_dir: {sample_dir!r}")
    sample_root = fixtures_root / relative
    if not sample_root.is_dir():
        raise AaError(f"golden sample not found: {sample_root}")
    files: dict[str, str] = {}
    for path in sorted(sample_root.rglob("*")):
        if path.is_symlink():
            raise AaError(f"fixture contains symlink: {path}")
        if path.is_file():
            rel = path.relative_to(sample_root).as_posix()
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    canonical = "\n".join(f"{name}:{digest}" for name, digest in sorted(files.items()))
    return FixtureLockEntry(
        sample_dir=relative.as_posix(),
        aggregate_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        files=files,
    )


def write_fixture_lock(fixtures_root: Path, fixtures: dict[str, str]) -> Path:
    """Capture explicit fixture identities for trusted fixture maintenance."""
    lock = FixtureLock(
        fixtures={
            fixture_id: _fixture_entry(fixtures_root, sample_dir)
            for fixture_id, sample_dir in sorted(fixtures.items())
        }
    )
    path = fixtures_root / "fixture-lock.json"
    path.write_text(lock.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def resolve_locked_fixture(fixtures_root: Path, fixture_id: str) -> Path:
    lock_path = fixtures_root / "fixture-lock.json"
    try:
        lock = FixtureLock.model_validate_json(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as err:
        raise AaError(f"invalid fixture lock {lock_path}: {err}") from err
    expected = lock.fixtures.get(fixture_id)
    if expected is None:
        raise AaError(f"fixture_id not found in fixture lock: {fixture_id}")
    current = _fixture_entry(fixtures_root, expected.sample_dir)
    if current != expected:
        changed = sorted(set(current.files) | set(expected.files))
        changed = [name for name in changed if current.files.get(name) != expected.files.get(name)]
        suffix = f" ({', '.join(changed[:5])})" if changed else ""
        raise AaError(f"fixture lock mismatch for {fixture_id}{suffix}")
    return fixtures_root / expected.sample_dir


def fixture_digest(fixtures_root: Path, fixture_id: str) -> str:
    lock_path = fixtures_root / "fixture-lock.json"
    try:
        lock = FixtureLock.model_validate_json(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as err:
        raise AaError(f"invalid fixture lock {lock_path}: {err}") from err
    entry = lock.fixtures.get(fixture_id)
    if entry is None:
        raise AaError(f"fixture_id not found in fixture lock: {fixture_id}")
    return entry.aggregate_sha256


def _deep_merge_resets(parent: FixtureResets, child: FixtureResets) -> FixtureResets:
    return FixtureResets(
        workflow_state={**parent.workflow_state, **child.workflow_state},
        qa_yaml={**parent.qa_yaml, **child.qa_yaml},
    )


def _task_identity(task: FixtureImportTask) -> tuple[str, str, str, str | None]:
    return (task.path, task.graph, task.node, task.task_key)


def _merge_import_defs(parent: FixtureImportDef, child: FixtureImportDef) -> FixtureImportDef:
    by_id = {_task_identity(t): t for t in parent.completed}
    for task in child.completed:
        by_id[_task_identity(task)] = task
    return FixtureImportDef(
        entrypoint=child.entrypoint or parent.entrypoint,
        inputs=list(dict.fromkeys([*parent.inputs, *child.inputs])),
        completed=list(by_id.values()),
    )


def _merge_imports(
    parent: dict[str, FixtureImportDef],
    child: dict[str, FixtureImportDef],
) -> dict[str, FixtureImportDef]:
    merged = dict(parent)
    for key, child_def in child.items():
        if key not in merged:
            merged[key] = child_def
            continue
        merged[key] = _merge_import_defs(merged[key], child_def)
    return merged


def _tier_file(tiers_dir: Path, tier_name: str) -> Path:
    by_name = tiers_dir / f"{tier_name}.yaml"
    if by_name.is_file():
        return by_name
    for path in sorted(tiers_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict) and raw.get("name") == tier_name:
            return path
    raise AaError(f"fixture tier not found: {tier_name} (looked in {tiers_dir})")


def load_tier(fixtures_root: Path, tier_name: str, *, _seen: set[str] | None = None) -> TierManifest:
    """Load and expand a tier manifest, merging ``extends`` parents."""
    seen = _seen or set()
    if tier_name in seen:
        raise AaError(f"fixture tier extends cycle involving {tier_name!r}")
    seen.add(tier_name)
    tiers_dir = fixtures_root / "tiers"
    path = _tier_file(tiers_dir, tier_name)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AaError(f"invalid fixture tier manifest: {path}")
    child = TierManifest.model_validate({**raw, "name": raw.get("name") or tier_name})
    if not child.extends:
        return child
    parent = load_tier(fixtures_root, child.extends, _seen=seen)
    return TierManifest(
        name=child.name,
        extends=child.extends,
        description=child.description or parent.description,
        paths=list(dict.fromkeys([*parent.paths, *child.paths])),
        resets=_deep_merge_resets(parent.resets, child.resets),
        source_prefix=child.source_prefix or parent.source_prefix,
        imports=_merge_imports(parent.imports, child.imports),
    )


def _set_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor: dict[str, Any] = target
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _split_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    change_paths: list[str] = []
    test_paths: list[str] = []
    for rel in paths:
        if rel.startswith("tests/"):
            test_paths.append(rel)
        else:
            change_paths.append(rel)
    return change_paths, test_paths


def _copy_rel(src_root: Path, dest_root: Path, rel: str) -> None:
    src = src_root / rel
    if not src.exists():
        raise AaError(f"fixture path missing: {src}")
    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)


def _apply_workflow_state_resets(change_dir: Path, resets: dict[str, Any]) -> None:
    if not resets:
        # Still re-hash if a state file was copied from archive with stale integrity.
        if (change_dir / "workflow-state.yaml").exists():
            state = read_state_lenient(change_dir)
            write_state(change_dir, state)
        return
    state = read_state_lenient(change_dir)
    dumped = state.model_dump(mode="json")
    for key, value in resets.items():
        _set_dotted(dumped, key, value)
    from assurance_agent.artifacts.models import WorkflowState

    write_state(change_dir, WorkflowState.model_validate(dumped))


def _apply_qa_yaml_resets(change_dir: Path, resets: dict[str, Any]) -> None:
    if not resets:
        return
    qa_path = change_dir / ".qa.yaml"
    raw: dict[str, Any] = {}
    if qa_path.exists():
        loaded = yaml.safe_load(qa_path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            raw = loaded
    for key, value in resets.items():
        _set_dotted(raw, key, value)
    qa_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _logical_to_path(change_dir: Path, project_root: Path, logical: str) -> Path:
    root, sep, rest = logical.partition(":")
    if not sep or root not in {"change", "project", "repo"}:
        raise AaError(f"unsafe logical path root: {logical!r}")
    if rest.startswith("/") or any(seg in ("", ".", "..") for seg in rest.split("/")):
        raise AaError(f"unsafe logical path: {logical!r}")
    base = change_dir if root == "change" else project_root
    return base / rest


def _hash_logical(change_dir: Path, project_root: Path, logical: str) -> str:
    path = _logical_to_path(change_dir, project_root, logical)
    digest = sha256_file(path)
    if digest is None:
        raise AaError(f"fixture import source missing or not a file: {logical} ({path})")
    return digest


def _ensure_assurance_seed_artifacts(
    change_dir: Path,
    project_root: Path,
    change_id: str,
    import_def: FixtureImportDef,
) -> None:
    """Materialize typed PlanReview + mechanical checks for gated Fuzz/Performance imports."""
    from assurance_agent.artifacts.models.review import PlanReview
    from assurance_agent.verification.checks.registry import run_plan_checks
    from assurance_agent.verification.profiles import get_layer_assurance_profile

    gate_ids = {t.gate for t in import_def.completed if t.gate}
    logicals = set(import_def.inputs)
    for task in import_def.completed:
        logicals.update(task.outputs)

    fixture_root = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "assurance"
    for layer in _ASSURANCE_SEED_LAYERS:
        profile = get_layer_assurance_profile(layer)
        needed = bool(
            gate_ids
            & {
                profile.gate_id,
                f"{layer}-codegen-precondition-gate",
            }
        ) or any(f"change:review/{layer}-" in item for item in logicals)
        if not needed:
            continue

        contract_dir = fixture_root / f"{layer}-contract"
        knowledge_src = contract_dir / ".aa" / "data-knowledge.yaml"
        knowledge_dst = project_root / ".aa" / "data-knowledge.yaml"
        if knowledge_src.is_file() and (
            not knowledge_dst.is_file()
            or knowledge_dst.read_text(encoding="utf-8").strip() == "capabilities: {}"
        ):
            knowledge_dst.parent.mkdir(parents=True, exist_ok=True)
            knowledge_dst.write_text(knowledge_src.read_text(encoding="utf-8"), encoding="utf-8")

        review_rel = profile.review_artifact
        review_path = change_dir / review_rel
        if not review_path.is_file():
            src = contract_dir / "review" / Path(review_rel).name
            payload = json.loads(src.read_text(encoding="utf-8"))
            payload["change_id"] = change_id
            review = PlanReview.model_validate(payload)
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text(
                json.dumps(review.model_dump(mode="json"), indent=2) + "\n",
                encoding="utf-8",
            )

        summary_rel = review_rel.replace(".json", "-summary.md")
        summary_path = change_dir / summary_rel
        if not summary_path.is_file():
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                f"# {layer} plan review summary\n\ndecision: pass\n",
                encoding="utf-8",
            )

        checks_path = change_dir / profile.checks_artifact
        if not checks_path.is_file():
            from assurance_agent.verification.checks.base import CheckContext

            plan_texts = {
                path: (change_dir / path).read_text(encoding="utf-8")
                for path in profile.plan_artifacts
                if (change_dir / path).is_file()
            }
            cases = []
            for case_path in sorted((change_dir / "cases").glob("**/case.yaml")):
                payload = yaml.safe_load(case_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    cases.append(payload)
            knowledge_payload = yaml.safe_load(knowledge_dst.read_text(encoding="utf-8")) or {}
            review_payload = json.loads(review_path.read_text(encoding="utf-8"))
            document = run_plan_checks(
                CheckContext(
                    plan_texts=plan_texts,
                    cases=cases,
                    data_knowledge=knowledge_payload if isinstance(knowledge_payload, dict) else {},
                    layer=profile.layer,
                    required_capabilities=tuple(review_payload.get("required_capabilities") or ()),
                )
            )
            checks_path.parent.mkdir(parents=True, exist_ok=True)
            checks_path.write_text(
                json.dumps(document.model_dump(mode="json"), indent=2) + "\n",
                encoding="utf-8",
            )

    knowledge = project_root / ".aa" / "data-knowledge.yaml"
    if not knowledge.is_file():
        knowledge.parent.mkdir(parents=True, exist_ok=True)
        knowledge.write_text("capabilities: {}\n", encoding="utf-8")

    for name in ("failure-analysis.json", "quality-gate-result.json"):
        legacy = change_dir / "execution" / name
        target = change_dir / "inspect" / name
        if legacy.is_file() and not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)


def _state_values(change_dir: Path) -> dict[str, Any]:
    path = change_dir / "workflow-state.yaml"
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if key not in {"_integrity", "schema_version"} and not str(key).startswith("_")
    }


def _evaluate_gate_for_seed(
    *,
    project_root: Path,
    change_dir: Path,
    change_id: str,
    gate_id: str,
    node_results: Mapping[str, Mapping[str, Any]],
) -> tuple[str, dict[str, str]]:
    from assurance_agent.workflow.graph.compiler import compile_packaged_workflow, compile_workflow
    from assurance_agent.workflow.graph.contracts import load_execution_contracts
    from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2_with_origin

    loaded = load_workflow_v2_with_origin(project_root)
    contracts = load_execution_contracts(project_root)
    compiled = (
        compile_packaged_workflow(loaded.schema, contracts)
        if loaded.origin == "packaged"
        else compile_workflow(loaded.schema, contracts)
    )
    if gate_id not in compiled.schema.gates:
        raise AaError(f"unknown gate in fixture import: {gate_id}")
    report = check_gate_in_view(
        compiled.schema.gates,
        gate_id,
        GateEvaluationContext(
            project_root=project_root,
            repo_root=project_root,
            change_dir=change_dir,
            change_id=change_id,
            params={},
            state_values=_state_values(change_dir),
            node_results=node_results,
        ),
    )
    return report.verdict.value, dict(report.reads_sha256)


def _write_import_manifest(
    *,
    change_dir: Path,
    project_root: Path,
    change_id: str,
    fixture_id: str,
    fixtures_root: Path,
    import_def: FixtureImportDef,
) -> Path:
    _ensure_assurance_seed_artifacts(change_dir, project_root, change_id, import_def)
    digest = fixture_digest(fixtures_root, fixture_id)
    inputs: dict[str, str] = {}
    for logical in import_def.inputs:
        inputs[logical] = _hash_logical(change_dir, project_root, logical)

    completed: list[dict[str, Any]] = []
    node_results_by_structural_path: dict[str, dict[str, dict[str, Any]]] = {}
    for task in import_def.completed:
        outputs = {logical: _hash_logical(change_dir, project_root, logical) for logical in task.outputs}
        entry: dict[str, Any] = {
            "path": task.path,
            "graph": task.graph,
            "node": task.node,
            "outputs": outputs,
        }
        if task.task_key is not None:
            entry["task_key"] = task.task_key
        local_results = node_results_by_structural_path.setdefault(task.path, {})
        local_payload = local_results.setdefault(task.node, {})
        local_payload["status"] = "succeeded"
        if task.gate is not None:
            verdict, reads = _evaluate_gate_for_seed(
                project_root=project_root,
                change_dir=change_dir,
                change_id=change_id,
                gate_id=task.gate,
                node_results=local_results,
            )
            entry["gate"] = {
                "id": task.gate,
                "verdict": verdict,
                "reads_sha256": reads,
            }
            gate_payload = local_payload.setdefault("gate", {})
            if isinstance(gate_payload, dict):
                gate_payload.update({"gate_id": task.gate, "verdict": verdict, "reads_sha256": reads})
            else:
                local_payload["gate"] = {
                    "gate_id": task.gate,
                    "verdict": verdict,
                    "reads_sha256": reads,
                }
        completed.append(entry)

    payload = {
        "schema_version": "2",
        "entrypoint": import_def.entrypoint,
        "source": {
            "kind": "eval-fixture",
            "fixture_id": fixture_id,
            "fixture_digest": f"sha256:{digest}",
        },
        "inputs": inputs,
        "completed": completed,
        "budgets": [],
    }
    out_dir = change_dir / ".graph-runtime"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "import-manifest.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def seed_change(
    *,
    sut_sandbox: Path,
    change_id: str,
    tier_name: str,
    fixtures_root: Path,
    fixture_id: str,
    entrypoint: str | None = None,
) -> SeedResult:
    """Reset a sandbox change directory from a golden fixture tier.

    When ``entrypoint`` matches a tier import declaration, also writes a
    hash-complete ``.graph-runtime/import-manifest.yaml``.
    """
    assert_change_id_safe(change_id)
    tier = load_tier(fixtures_root, tier_name)
    sample_root = resolve_locked_fixture(fixtures_root, fixture_id)

    change_dir = sut_sandbox / "qa" / "changes" / change_id
    staging = sut_sandbox / "qa" / "changes" / f".seed-staging-{change_id}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    change_paths, test_paths = _split_paths(tier.paths)
    # Always seed the whole sample tree when paths empty (full snapshot).
    if not tier.paths:
        for entry in sample_root.rglob("*"):
            if entry.is_file():
                rel = entry.relative_to(sample_root).as_posix()
                if rel.startswith("tests/"):
                    test_paths.append(rel)
                else:
                    change_paths.append(rel)

    for rel in change_paths:
        _copy_rel(sample_root, staging, rel)
    for rel in test_paths:
        _copy_rel(sample_root, sut_sandbox, rel)

    if change_dir.exists():
        shutil.rmtree(change_dir)
    staging.rename(change_dir)

    _apply_workflow_state_resets(change_dir, tier.resets.workflow_state)
    _apply_qa_yaml_resets(change_dir, tier.resets.qa_yaml)

    import_manifest_path: Path | None = None
    if entrypoint:
        import_def = tier.imports.get(entrypoint)
        if import_def is not None:
            if import_def.entrypoint != entrypoint:
                raise AaError(f"tier import key {entrypoint!r} declares entrypoint {import_def.entrypoint!r}")
            import_manifest_path = _write_import_manifest(
                change_dir=change_dir,
                project_root=sut_sandbox,
                change_id=change_id,
                fixture_id=fixture_id,
                fixtures_root=fixtures_root,
                import_def=import_def,
            )

    return SeedResult(change_dir=change_dir, import_manifest_path=import_manifest_path)
