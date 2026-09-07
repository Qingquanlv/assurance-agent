"""Deterministic semantic admission for verified generated API tests."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_generation.contracts.codegen import (
    CodegenAuthoringV1,
    CodegenMapping,
    staged_generated_path,
)
from assurance_generation.contracts.compiler import PlanNotReady, case_specification, compile_case_plan
from assurance_generation.contracts.execution_plan import (
    CaseExecutionPlanSetV1,
    CasePlanContextV1,
    ExecutionBindingsV1,
    ValidationProfile,
)
from assurance_generation.contracts.mapping import ClosedMappingEntryV1, ClosedMappingV1
from assurance_intake.contracts.cases import CaseYamlAuthoring, MinimumCoverageMatrixAuthoring
from assurance_intake.contracts.plan import ResolvedAssurancePlan, decode_plan
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.contracts.reviewed_case import authenticate_reviewed_case
from assurance_intake.contracts.verification import (
    AssertionSourcesV1,
    BusinessAssertionV1,
    validate_assertion_provenance,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1


class GenerationAdmissionError(ValueError):
    """Committed generation files do not reproduce one accepted semantic closure."""


@dataclass(frozen=True)
class VerifiedGenerationAdmission:
    root_plan: ResolvedAssurancePlan
    reviewed_case: ReviewedCaseV1
    machine_plans: CaseExecutionPlanSetV1
    manifests: tuple[CodegenAuthoringV1, ...]
    closed_mapping: ClosedMappingV1
    source_refs: tuple[EvidenceArtifactRefV1, ...]
    plan_refs: tuple[EvidenceArtifactRefV1, ...]


def _regular_file(root: Path, relative: str) -> Path:
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise GenerationAdmissionError(f"accepted generation input is a symlink: {relative}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise GenerationAdmissionError(f"accepted generation input is missing: {relative}") from error
    if resolved != path or not path.is_file() or path.stat().st_nlink != 1:
        raise GenerationAdmissionError(
            f"accepted generation input must be a regular single-link file: {relative}"
        )
    return path


def _evidence_ref(root: Path, relative: str) -> EvidenceArtifactRefV1:
    data = _regular_file(root, relative).read_bytes()
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())


def _tree_refs(root: Path, relative: str) -> tuple[EvidenceArtifactRefV1, ...]:
    directory = root.joinpath(*PurePosixPath(relative).parts)
    if not directory.is_dir() or directory.is_symlink():
        raise GenerationAdmissionError(f"accepted generation directory is unavailable: {relative}")
    paths = tuple(
        sorted(
            item.relative_to(root).as_posix()
            for item in directory.rglob("*")
            if item.is_file() and not item.is_symlink() and item.stat().st_nlink == 1
        )
    )
    return tuple(_evidence_ref(root, path) for path in paths)


def _reviewed_documents(
    root: Path,
    reviewed: ReviewedCaseV1,
    *,
    capability_leafs: tuple[str, ...],
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    case_root = root / "qa" / "changes" / reviewed.change_id / "cases"
    actual_paths = {
        item.relative_to(root).as_posix()
        for item in case_root.glob("**/case.yaml")
        if item.is_file() and not item.is_symlink() and item.stat().st_nlink == 1
    }
    if actual_paths != {ref.path for ref in reviewed.case_refs}:
        raise GenerationAdmissionError("ReviewedCase case-file closure differs from the workspace")
    by_id: dict[str, dict[str, object]] = {}
    authored_by_id: dict[str, object] = {}
    for ref in reviewed.case_refs:
        try:
            raw = yaml.safe_load(_regular_file(root, ref.path).read_bytes())
            authored = CaseYamlAuthoring.model_validate(
                raw, context={"capability_leafs": frozenset(capability_leafs)}
            )
        except (yaml.YAMLError, ValidationError, ValueError) as error:
            raise GenerationAdmissionError(f"reviewed Case YAML is invalid: {ref.path}: {error}") from error
        raw_mapping = cast(dict[str, object], raw)
        for entry in (*authored.added, *authored.modified):
            if entry.case_id in by_id:
                raise GenerationAdmissionError(f"duplicate reviewed Case ID: {entry.case_id}")
            by_id[entry.case_id] = case_specification(raw_mapping, entry.case_id)
            authored_by_id[entry.case_id] = entry
    return by_id, authored_by_id


def _assertion_sources(
    root: Path,
    *,
    reviewed: ReviewedCaseV1,
    root_plan: ResolvedAssurancePlan,
    cases: dict[str, dict[str, object]],
) -> dict[str, AssertionSourcesV1]:
    policy = root_plan.verification_policy
    if policy is None:
        raise GenerationAdmissionError("verified generation requires an admitted verification policy")
    policy_path = ".aa/verification-policy.yaml"
    policy_bytes = _regular_file(root, policy_path).read_bytes()
    if hashlib.sha256(policy_bytes).hexdigest() != policy.digest:
        raise GenerationAdmissionError("verification policy resource digest changed")
    try:
        policy_document = yaml.safe_load(policy_bytes)
    except yaml.YAMLError as error:
        raise GenerationAdmissionError(f"verification policy resource is invalid: {error}") from error
    if policy_document != {"validation_profile": policy.validation_profile}:
        raise GenerationAdmissionError("verification policy resource differs from the frozen root plan")

    requirement_path = f"qa/changes/{reviewed.change_id}/requirement.md"
    requirement_refs = [ref for ref in reviewed.preparation_refs if ref.path == requirement_path]
    if len(requirement_refs) != 1 or requirement_refs[0].digest != root_plan.requirement_digest:
        raise GenerationAdmissionError("ReviewedCase does not bind the root-plan requirement")
    if _evidence_ref(root, requirement_path) != requirement_refs[0]:
        raise GenerationAdmissionError("root-plan requirement bytes changed")

    source_paths = {
        str(PurePosixPath(ref.path).parent / "assertion-sources.json") for ref in reviewed.case_refs
    }
    source_refs = {ref.path: ref for ref in reviewed.preparation_refs if ref.path in source_paths}
    if set(source_refs) != source_paths:
        raise GenerationAdmissionError("ReviewedCase assertion-source closure is incomplete")
    authority_refs = reviewed.preparation_refs
    sources_by_case: dict[str, AssertionSourcesV1] = {}
    revision = policy.validation_profile.rsplit(".v", maxsplit=1)[1]
    for path in sorted(source_paths):
        if _evidence_ref(root, path) != source_refs[path]:
            raise GenerationAdmissionError(f"assertion-source bytes changed: {path}")
        try:
            sources = AssertionSourcesV1.model_validate_json(_regular_file(root, path).read_bytes())
            case = cases[sources.case_id]
            assertions = tuple(
                BusinessAssertionV1.model_validate(item) for item in cast(list[object], case["assertions"])
            )
            validate_assertion_provenance(
                case_id=sources.case_id,
                revision=revision,
                spec_digest=root_plan.requirement_digest,
                assertions=assertions,
                sources=sources,
                requirement_ref=requirement_refs[0],
                authority_refs=authority_refs,
            )
        except (KeyError, TypeError, ValidationError, ValueError) as error:
            raise GenerationAdmissionError(f"assertion provenance is not authoritative: {error}") from error
        if sources.case_id in sources_by_case:
            raise GenerationAdmissionError(f"duplicate assertion sources for {sources.case_id}")
        sources_by_case[sources.case_id] = sources
    if set(sources_by_case) != set(cases):
        raise GenerationAdmissionError("assertion sources do not cover the exact ReviewedCase set")
    return sources_by_case


def _authenticate_review_projection(root: Path, reviewed: ReviewedCaseV1) -> str:
    matrix_path = f"qa/changes/{reviewed.change_id}/trace/minimum-coverage-matrix.json"
    matrix_refs = [ref for ref in reviewed.preparation_refs if ref.path == matrix_path]
    if len(matrix_refs) != 1 or _evidence_ref(root, matrix_path) != matrix_refs[0]:
        raise GenerationAdmissionError("ReviewedCase does not bind its minimum coverage matrix")
    try:
        matrix = MinimumCoverageMatrixAuthoring.model_validate_json(
            _regular_file(root, matrix_path).read_bytes()
        )
        review = CaseReviewResultV1.model_validate_json(
            _regular_file(root, reviewed.review_ref.path).read_bytes()
        )
    except ValidationError as error:
        raise GenerationAdmissionError(f"reviewed coverage projection is invalid: {error}") from error
    required = [row for row in matrix.root if row.required]
    expected = {
        "total_required": len(required),
        "covered": sum(row.status == "covered" for row in required),
        "skipped_by_scope": sum(row.status == "skipped_by_scope" for row in required),
        "missing": [row.key for row in required if row.status == "skipped_by_scope"],
    }
    if review.minimum_coverage.model_dump(mode="json") != expected:
        raise GenerationAdmissionError("Case review does not match the authenticated coverage matrix")
    reviewed_paths = review.source_verification.reviewed_source_files
    if len(reviewed_paths) != len(set(reviewed_paths)):
        raise GenerationAdmissionError("Case review source files must be unique")
    preparation = {ref.path: ref for ref in reviewed.preparation_refs}
    source_refs: list[EvidenceArtifactRefV1] = []
    for relative in sorted(reviewed_paths):
        ref = preparation.get(relative)
        if ref is None or _evidence_ref(root, relative) != ref:
            raise GenerationAdmissionError(
                f"Case review source is not in the authenticated preparation closure: {relative}"
            )
        source_refs.append(ref)
    return canonical_digest(cast(JSONValue, [ref.model_dump(mode="json") for ref in source_refs]))


def validate_verified_bridge_sources(
    source_root: Path,
    *,
    change_id: str,
    mapping: CodegenMapping,
) -> None:
    """Require the exact installed execute_case bridge for each mapped API Case."""

    by_target: dict[str, list[object]] = {}
    for entry in mapping.entries:
        by_target.setdefault(entry.target_file, []).append(entry)
    for target, raw_entries in by_target.items():
        relative = staged_generated_path(change_id, "api", target)
        path = _regular_file(source_root, relative)
        try:
            module = ast.parse(path.read_bytes(), filename=target)
        except (SyntaxError, ValueError) as error:
            raise GenerationAdmissionError(
                f"verified API bridge source is invalid: {target}: {error}"
            ) from error
        imports = [node for node in module.body if isinstance(node, ast.ImportFrom)]
        functions = [node for node in module.body if isinstance(node, ast.FunctionDef)]
        expected = {getattr(entry, "symbol"): getattr(entry, "case_id") for entry in raw_entries}
        if (
            len(imports) != 1
            or imports[0].module != "assurance_execution.bridge"
            or imports[0].level != 0
            or len(imports[0].names) != 1
            or imports[0].names[0].name != "execute_case"
            or imports[0].names[0].asname is not None
            or len(module.body) != 1 + len(functions)
            or len(functions) != len(expected)
        ):
            raise GenerationAdmissionError(
                f"verified API test must contain only the installed execute_case bridge: {target}"
            )
        if set(expected) != {function.name for function in functions}:
            raise GenerationAdmissionError(
                f"verified API bridge symbols do not match the frozen mapping: {target}"
            )
        for function in functions:
            arguments = function.args
            if (
                function.decorator_list
                or function.returns is not None
                or function.type_comment is not None
                or arguments.posonlyargs
                or arguments.args
                or arguments.vararg is not None
                or arguments.kwonlyargs
                or arguments.kwarg is not None
                or len(function.body) != 1
            ):
                raise GenerationAdmissionError(
                    f"verified API test must be one parameter-free bridge call: {target}"
                )
            statement = function.body[0]
            call = statement.value if isinstance(statement, ast.Expr) else None
            if (
                not isinstance(call, ast.Call)
                or not isinstance(call.func, ast.Name)
                or call.func.id != "execute_case"
                or call.keywords
                or len(call.args) != 1
                or not isinstance(call.args[0], ast.Constant)
                or call.args[0].value != expected[function.name]
            ):
                raise GenerationAdmissionError(
                    f"verified API bridge must call execute_case for its mapped Case: {target}"
                )


def admit_verified_generation(
    project_root: Path,
    source_root: Path,
    *,
    change_id: str,
    coverage_epoch: int,
    plan_digest: str,
    plan_ref: EvidenceArtifactRefV1,
    reviewed_case: ReviewedCaseV1,
    validation_profile: ValidationProfile,
    selected_test_families: tuple[str, ...],
    capability_leafs: tuple[str, ...],
    case_execution_plan_ref: EvidenceArtifactRefV1,
) -> VerifiedGenerationAdmission:
    """Rebuild the accepted verified generation closure from authoritative inputs."""

    try:
        reviewed = authenticate_reviewed_case(
            reviewed_case,
            project_root,
            change_id=change_id,
            coverage_epoch=coverage_epoch,
            require_acceptance_record=True,
        )
        root_plan = decode_plan(_regular_file(project_root, plan_ref.path).read_bytes(), plan_ref)
    except (OSError, ValidationError, ValueError) as error:
        raise GenerationAdmissionError(f"ReviewedCase is not authenticated: {error}") from error
    if (
        root_plan.change_id != change_id
        or root_plan.plan_digest != plan_digest
        or reviewed.plan_digest != plan_digest
        or reviewed.plan_ref != plan_ref
        or root_plan.selected_test_families != selected_test_families
        or root_plan.verification_policy is None
        or root_plan.verification_policy.validation_profile != validation_profile
        or root_plan.verification_policy.resource_id != "assurance.product.configuration.verification-policy"
    ):
        raise GenerationAdmissionError("verified generation differs from the frozen root plan")

    sut_digest = _authenticate_review_projection(project_root, reviewed)
    cases, authored = _reviewed_documents(project_root, reviewed, capability_leafs=capability_leafs)
    sources = _assertion_sources(
        project_root,
        reviewed=reviewed,
        root_plan=root_plan,
        cases=cases,
    )

    expected_machine_path = f"qa/changes/{change_id}/plans/api-case-execution-plan.json"
    if case_execution_plan_ref.path != expected_machine_path:
        raise GenerationAdmissionError("machine plan path differs from the accepted API plan")
    machine_path = _regular_file(project_root, expected_machine_path)
    machine_bytes = machine_path.read_bytes()
    if hashlib.sha256(machine_bytes).hexdigest() != case_execution_plan_ref.digest:
        raise GenerationAdmissionError("machine plan digest changed")
    try:
        machine_plans = CaseExecutionPlanSetV1.model_validate_json(machine_bytes)
    except ValidationError as error:
        raise GenerationAdmissionError(f"machine plan is invalid: {error}") from error
    if len(machine_plans.cases) != 1:
        raise GenerationAdmissionError("verified API v1 requires one deterministically compiled Case")
    policy = root_plan.verification_policy
    assert policy is not None
    bindings_path = f"qa/changes/{change_id}/plans/api-execution-bindings.json"
    bindings_ref = _evidence_ref(project_root, bindings_path)
    context = CasePlanContextV1(
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        plan_digest=plan_digest,
        plan_ref=plan_ref,
        reviewed_case=reviewed,
        verification_policy_digest=policy.digest,
        technical_config_digest=bindings_ref.digest,
        sut_digest=sut_digest,
    )
    try:
        bindings = ExecutionBindingsV1.model_validate_json(
            _regular_file(project_root, bindings_path).read_bytes()
        )
        source = sources[bindings.case_id]
        expected_plan = compile_case_plan(
            cases[bindings.case_id],
            source,
            cast(dict[str, object], bindings.model_dump(mode="python")["bindings"]),
            validation_profile,
            context=context,
        )
    except (KeyError, ValidationError, PlanNotReady) as error:
        raise GenerationAdmissionError(f"machine plan cannot be recompiled: {error}") from error
    expected_set = CaseExecutionPlanSetV1(change_id=change_id, cases=(expected_plan,))
    expected_bytes = canonical_json_bytes(cast(JSONValue, expected_set.model_dump(mode="json"))) + b"\n"
    if machine_plans != expected_set or machine_bytes != expected_bytes:
        raise GenerationAdmissionError("machine plan differs from deterministic compiler output")

    manifests: list[CodegenAuthoringV1] = []
    source_refs: list[EvidenceArtifactRefV1] = []
    closed_entries: list[ClosedMappingEntryV1] = []
    for family in selected_test_families:
        relative = f"qa/changes/{change_id}/codegen/{family}-generated-files.json"
        manifest_ref = _evidence_ref(source_root, relative)
        source_refs.append(manifest_ref)
        try:
            manifest = CodegenAuthoringV1.model_validate_json(
                _regular_file(source_root, relative).read_bytes(),
                context={"capability_leafs": frozenset(capability_leafs)},
            )
        except ValidationError as error:
            raise GenerationAdmissionError(f"generated-files manifest is invalid: {error}") from error
        if manifest.change_id != change_id or manifest.layer != family:
            raise GenerationAdmissionError("generated-files manifest identity changed")
        manifests.append(manifest)
        mapping = manifest.mapping
        mapped_by_target: dict[str, set[str]] = {}
        for entry in mapping.entries:
            mapped_by_target.setdefault(entry.target_file, set()).add(entry.case_id)
            case = authored.get(entry.case_id)
            if case is None or getattr(case, "type").lower() != family:
                raise GenerationAdmissionError("generation mapping references an unreviewed Case")
            capabilities = sorted(key for key in getattr(case, "trace") if key in capability_leafs)
            if not capabilities:
                raise GenerationAdmissionError("mapped Case has no admitted capability")
            closed_entries.append(
                ClosedMappingEntryV1(
                    test=f"{entry.target_file}::{entry.symbol.replace('.', '::')}",
                    case_id=entry.case_id,
                    capability=capabilities[0],
                    layer=cast(Any, family),
                )
            )
        file_rows = {item.repo_path: (item.role, set(item.case_ids)) for item in manifest.files}
        if set(mapped_by_target) != {
            item.repo_path for item in manifest.files if item.role == "test_entry"
        } or any(file_rows[target] != ("test_entry", ids) for target, ids in mapped_by_target.items()):
            raise GenerationAdmissionError("generated test files differ from their exact mapping")
        for item in manifest.files:
            source_refs.append(
                _evidence_ref(
                    source_root,
                    staged_generated_path(change_id, family, item.repo_path),
                )
            )
        if family == "api":
            expected_spec_digests = {item.case_id: item.spec_digest for item in machine_plans.cases}
            if mapping.case_spec_digests != expected_spec_digests:
                raise GenerationAdmissionError("verified API mapping carries a stale Case spec digest")
            if (
                not mapping.is_verified
                or mapping.validation_profile != validation_profile
                or mapping.coverage_epoch != coverage_epoch
                or mapping.plan_digest != plan_digest
                or mapping.plan_ref != plan_ref
                or mapping.reviewed_case != reviewed
                or mapping.case_execution_plan_ref != case_execution_plan_ref
                or mapping.case_execution_plan_digest != case_execution_plan_ref.digest
                or {item.case_id for item in mapping.entries}
                != {item.case_id for item in machine_plans.cases}
            ):
                raise GenerationAdmissionError("verified API mapping differs from recompiled semantics")
            validate_verified_bridge_sources(source_root, change_id=change_id, mapping=mapping)
    if not any(item.layer == "api" for item in manifests):
        raise GenerationAdmissionError("verified generation requires one API manifest")
    closed_entries.sort(key=lambda item: (item.layer, item.test, item.case_id))
    closed = ClosedMappingV1(
        selected=tuple(item.test for item in closed_entries), mappings=tuple(closed_entries)
    )
    return VerifiedGenerationAdmission(
        root_plan=root_plan,
        reviewed_case=reviewed,
        machine_plans=machine_plans,
        manifests=tuple(manifests),
        closed_mapping=closed,
        source_refs=tuple(sorted(source_refs, key=lambda item: item.path)),
        plan_refs=_tree_refs(project_root, f"qa/changes/{change_id}/plans"),
    )


__all__ = [
    "GenerationAdmissionError",
    "VerifiedGenerationAdmission",
    "admit_verified_generation",
    "validate_verified_bridge_sources",
]
