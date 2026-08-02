"""Observing harness for independent four-layer assurance contract fixtures.

Test-only helpers: structural skill-section parsing, canonical bundle loading,
and production-boundary observation. Does not activate packaged skills/YAML.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.assurance import PLAN_CHECK_IDS, LayerName
from assurance_agent.artifacts.models.generated_files import (
    ApiGeneratedFilesV1,
    E2eGeneratedFilesV1,
    FuzzGeneratedFilesV1,
    GeneratedFilesV1,
    PerformanceGeneratedFilesV1,
)
from assurance_agent.artifacts.models.plan_checks import (
    CheckEvidence,
    LayerApplicability,
    PlanCheckDocument,
)
from assurance_agent.artifacts.models.review import PlanReview, PlanReviewAuthoring
from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks, validate_plan_check_document
from assurance_agent.verification.generated_files import get_generated_files_contract
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend, WorkspaceError
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.orchestration.schema import GateDef, normalize_gates

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSURANCE_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "assurance"
CANONICAL_CHANGE_ID = "CH-CANONICAL"
_LAYER_NAMES: tuple[LayerName, ...] = ("api", "e2e", "fuzz", "performance")
_SECTION_HEADINGS = ("Inputs", "Outputs", "State Authority", "Runtime Context")
_PATH_GROUP_NAMES = frozenset({"required", "optional", "conditional"})
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_BACKTICK_PATH_RE = re.compile(r"^-\s+`([^`]+)`\s*$")
_STATE_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$")
_SUMMARY_BY_LAYER = {
    "api": "plans/m3-review-summary.md",
    "e2e": "plans/m4-review-summary.md",
    "fuzz": "plans/fuzz-review-summary.md",
    "performance": "plans/performance-review-summary.md",
}
_SKILL_BY_LAYER = {
    "api": "skills/aa-api-codegen/SKILL.md",
    "e2e": "skills/aa-e2e-codegen/SKILL.md",
    "fuzz": "skills/aa-fuzz-codegen/SKILL.md",
    "performance": "skills/aa-performance-codegen/SKILL.md",
}
_GENERATED_MODELS: dict[str, type[GeneratedFilesV1]] = {
    "api": ApiGeneratedFilesV1,
    "e2e": E2eGeneratedFilesV1,
    "fuzz": FuzzGeneratedFilesV1,
    "performance": PerformanceGeneratedFilesV1,
}
_EXPECTED_CHECK_STATUS: dict[str, dict[str, str]] = {
    "api": {"l1_path": "pass", "shared_factory": "pass", "assert_ideal": "pass", "capability_keys": "pass"},
    "e2e": {"l1_path": "pass", "shared_factory": "pass", "assert_ideal": "pass", "capability_keys": "pass"},
    "fuzz": {
        "l1_path": "pass",
        "shared_factory": "pass",
        "assert_ideal": "not_applicable",
        "capability_keys": "pass",
    },
    "performance": {
        "l1_path": "pass",
        "shared_factory": "pass",
        "assert_ideal": "not_applicable",
        "capability_keys": "pass",
    },
}
_FUZZ_PERF_GATES = ASSURANCE_FIXTURE_ROOT / "fuzz-performance-gates.yaml"


class SkillContractParseError(ValueError):
    """Raised when a skill Markdown contract section is structurally invalid."""


@dataclass(frozen=True, slots=True)
class SkillPathGroup:
    name: Literal["required", "optional", "conditional"]
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillContractSections:
    inputs: tuple[SkillPathGroup, ...]
    outputs: tuple[SkillPathGroup, ...]
    state_authority: tuple[tuple[str, str], ...]
    runtime_context: tuple[SkillPathGroup, ...] | None


@dataclass(frozen=True, slots=True)
class CanonicalAssuranceBundle:
    layer: LayerName
    root: Path
    change_id: str
    plan_texts: dict[str, str]
    cases: list[dict[str, object]]
    data_knowledge: dict[str, object]
    config_text: str
    review_bytes: bytes
    required_capabilities: tuple[str, ...]
    summary_relpath: str
    summary_text: str
    skill_path: Path
    adapter_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BoundaryObservation:
    boundary: str
    status: Literal["ok", "reject"]
    code: str | None = None
    locator: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AssuranceContractObservation:
    layer: LayerName
    authoring_review: PlanReviewAuthoring
    frozen_review: PlanReview
    applicability: LayerApplicability
    checks: PlanCheckDocument
    plan_gate_verdict: str
    codegen_precondition_verdict: str
    visible_codegen_inputs: tuple[str, ...]
    frozen_output_digests: tuple[tuple[str, str], ...]
    generated_files_manifest_sha256: str
    candidate_validation_receipt_id: str
    boundaries: tuple[BoundaryObservation, ...] = field(default_factory=tuple)


def parse_skill_contract_sections(path: Path) -> SkillContractSections:
    """Parse exact structured skill contract headings from Markdown."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    i = 0
    while i < len(lines):
        match = _HEADING_RE.match(lines[i])
        if match is not None and match.group(1) == "##":
            title = match.group(2).strip()
            if title in _SECTION_HEADINGS:
                if title in sections:
                    raise SkillContractParseError(f"duplicate heading: ## {title}")
                sections[title] = []
                order.append(title)
                current = title
                i += 1
                continue
            if current is not None:
                raise SkillContractParseError(
                    f"section {current!r} interrupted by unexpected heading: ## {title}"
                )
        if current is not None:
            sections[current].append(lines[i])
        i += 1

    for required in ("Inputs", "Outputs", "State Authority"):
        if required not in sections:
            raise SkillContractParseError(f"missing required heading: ## {required}")

    inputs = _parse_path_groups(sections["Inputs"], section="Inputs")
    outputs = _parse_path_groups(sections["Outputs"], section="Outputs")
    state_authority = _parse_state_authority(sections["State Authority"])
    runtime_context = None
    if "Runtime Context" in sections:
        runtime_context = _parse_path_groups(sections["Runtime Context"], section="Runtime Context")
    return SkillContractSections(
        inputs=inputs,
        outputs=outputs,
        state_authority=state_authority,
        runtime_context=runtime_context,
    )


def _parse_path_groups(body: list[str], *, section: str) -> tuple[SkillPathGroup, ...]:
    groups: list[SkillPathGroup] = []
    current_name: str | None = None
    current_paths: list[str] = []
    seen: set[str] = set()

    def flush() -> None:
        nonlocal current_name, current_paths
        if current_name is None:
            return
        groups.append(
            SkillPathGroup(name=current_name, paths=tuple(current_paths))  # type: ignore[arg-type]
        )
        current_name = None
        current_paths = []

    for line in body:
        stripped = line.strip()
        if not stripped:
            continue
        heading = _HEADING_RE.match(stripped)
        if heading is not None:
            level, title = heading.group(1), heading.group(2).strip()
            if level == "##":
                raise SkillContractParseError(
                    f"section {section!r} interrupted by unexpected heading: ## {title}"
                )
            name = title.casefold()
            if name not in _PATH_GROUP_NAMES:
                raise SkillContractParseError(f"section {section!r}: unknown group name {title!r}")
            if name in seen:
                raise SkillContractParseError(f"section {section!r}: duplicate group {name!r}")
            flush()
            seen.add(name)
            current_name = name
            continue
        if current_name is None:
            if stripped.startswith("#"):
                raise SkillContractParseError(
                    f"section {section!r} interrupted by unexpected heading: {stripped}"
                )
            # Ignore non-path prose only before the first group; path rows outside groups fail.
            if stripped.startswith("-"):
                raise SkillContractParseError(
                    f"section {section!r}: path row outside a required/optional/conditional group"
                )
            continue
        path_match = _BACKTICK_PATH_RE.match(stripped)
        if path_match is None:
            if stripped.startswith("-") or "`" in stripped:
                raise SkillContractParseError(f"section {section!r}: malformed path row: {stripped}")
            raise SkillContractParseError(
                f"section {section!r} interrupted by unexpected content: {stripped}"
            )
        path = path_match.group(1).strip()
        if not path:
            raise SkillContractParseError(f"section {section!r}: empty backticked path")
        current_paths.append(path)
    flush()
    if not groups:
        raise SkillContractParseError(f"section {section!r}: missing path groups")
    return tuple(groups)


def _parse_state_authority(body: list[str]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for line in body:
        stripped = line.strip()
        if not stripped:
            continue
        heading = _HEADING_RE.match(stripped)
        if heading is not None:
            raise SkillContractParseError(
                f"section 'State Authority' interrupted by unexpected heading: {stripped}"
            )
        path_match = _BACKTICK_PATH_RE.match(stripped)
        if path_match is None:
            if stripped.startswith("-") or "`" in stripped:
                raise SkillContractParseError(
                    f"section 'State Authority': malformed key/value row: {stripped}"
                )
            raise SkillContractParseError(
                f"section 'State Authority' interrupted by unexpected content: {stripped}"
            )
        kv = _STATE_KV_RE.match(path_match.group(1).strip())
        if kv is None:
            raise SkillContractParseError(f"section 'State Authority': malformed key/value row: {stripped}")
        pairs.append((kv.group(1), kv.group(2).strip()))
    if not pairs:
        raise SkillContractParseError("section 'State Authority': missing key/value rows")
    return tuple(pairs)


def fixture_root_for(layer: str) -> Path:
    return ASSURANCE_FIXTURE_ROOT / f"{layer}-contract"


def load_canonical_assurance_bundle(layer: str) -> CanonicalAssuranceBundle:
    if layer not in _LAYER_NAMES:
        raise ValueError(f"unknown assurance layer: {layer}")
    root = fixture_root_for(layer)
    profile = get_layer_assurance_profile(layer)
    assert_fixture_independence(root)
    plan_texts = {path: (root / path).read_text(encoding="utf-8") for path in profile.plan_artifacts}
    cases = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((root / "cases").glob("**/case.yaml"))
    ]
    data_knowledge = yaml.safe_load((root / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8"))
    config_text = (root / ".aa" / "config.yaml").read_text(encoding="utf-8")
    review_bytes = (root / profile.review_artifact).read_bytes()
    review = yaml.safe_load(review_bytes.decode("utf-8"))
    required_capabilities = tuple(review["required_capabilities"])
    summary_relpath = _SUMMARY_BY_LAYER[layer]
    summary_text = (root / summary_relpath).read_text(encoding="utf-8")
    skill_path = root / _SKILL_BY_LAYER[layer]
    adapter_paths = tuple(
        sorted(
            str(path.relative_to(root)).replace("\\", "/")
            for path in (root / "tests").rglob("*")
            if path.is_file()
        )
    )
    # Fixtures must not ship mechanical checks or codegen outputs.
    for forbidden in (
        root / profile.checks_artifact,
        root / "codegen",
    ):
        if forbidden.exists():
            raise ValueError(f"fixture must not contain checks/codegen output: {forbidden}")
    account = root / "tests" / "testdata" / "domain" / "account.py"
    if not account.is_file():
        raise ValueError(f"fixture missing shared factory: {account}")
    return CanonicalAssuranceBundle(
        layer=layer,  # type: ignore[arg-type]
        root=root,
        change_id=str(review["change_id"]),
        plan_texts=plan_texts,
        cases=cases,
        data_knowledge=data_knowledge,
        config_text=config_text,
        review_bytes=review_bytes,
        required_capabilities=required_capabilities,
        summary_relpath=summary_relpath,
        summary_text=summary_text,
        skill_path=skill_path,
        adapter_paths=adapter_paths,
    )


def assert_fixture_independence(root: Path) -> None:
    """Reject fixture roots that import/symlink another fixture or benchmark tree."""
    own_name = root.name
    other_roots = tuple(
        name
        for name in ("api-contract", "e2e-contract", "fuzz-contract", "performance-contract")
        if name != own_name
    )
    for path in root.rglob("*"):
        if path.is_symlink():
            target = str(path.resolve())
            if "benchmark" in target or any(token in target for token in other_roots):
                raise ValueError(f"fixture symlink escapes independence: {path} -> {target}")
            continue
        if not path.is_file() or path.suffix not in {".py", ".md", ".yaml", ".yml", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "eval-fixtures/" in text or "benchmark/vue-fastapi-admin" in text:
            raise ValueError(f"fixture imports benchmark content via {path}")
        for other in other_roots:
            needle = f"tests/fixtures/assurance/{other}"
            if needle in text:
                raise ValueError(f"fixture imports another fixture root via {path}")


def expected_check_statuses(layer: str) -> dict[str, str]:
    return dict(_EXPECTED_CHECK_STATUS[layer])


def _policy_text() -> str:
    lines = [
        "version: 1",
        "human_review_risk_levels: [high, critical]",
        "force_continue_allowed: true",
        "plan_checks:",
    ]
    for check_id in PLAN_CHECK_IDS:
        lines.append(f"  {check_id}: warn")
    lines.extend(
        [
            "coverage_floor:",
            "  risk_high: 0.9",
            "  risk_medium: 0.7",
            "fuzz:",
            "  required_when_endpoint_has_auth: true",
            "healing:",
            "  auth_module: require_human",
            "",
        ]
    )
    return "\n".join(lines)


def _gates_for(layer: str) -> dict[str, GateDef]:
    if layer in {"fuzz", "performance"}:
        raw = yaml.safe_load(_FUZZ_PERF_GATES.read_text(encoding="utf-8"))
        return normalize_gates(raw)
    return load_workflow_v2(Path.cwd()).gates


def check_context_for_bundle(bundle: CanonicalAssuranceBundle) -> CheckContext:
    return CheckContext(
        plan_texts=bundle.plan_texts,
        cases=bundle.cases,
        data_knowledge=bundle.data_knowledge,
        layer=bundle.layer,
        required_capabilities=bundle.required_capabilities,
    )


def _materialize_project(bundle: CanonicalAssuranceBundle, project: Path) -> Path:
    change_dir = project / "qa" / "changes" / bundle.change_id
    change_dir.mkdir(parents=True)
    profile = get_layer_assurance_profile(bundle.layer)
    for rel, text in bundle.plan_texts.items():
        target = change_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    summary_target = change_dir / bundle.summary_relpath
    summary_target.parent.mkdir(parents=True, exist_ok=True)
    summary_target.write_text(bundle.summary_text, encoding="utf-8")
    for case_path in sorted((bundle.root / "cases").glob("**/case.yaml")):
        rel = case_path.relative_to(bundle.root)
        dest = change_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case_path, dest)
    review_dest = change_dir / profile.review_artifact
    review_dest.parent.mkdir(parents=True, exist_ok=True)
    review_dest.write_bytes(bundle.review_bytes)
    aa = project / ".aa"
    aa.mkdir(parents=True, exist_ok=True)
    (aa / "config.yaml").write_text(bundle.config_text, encoding="utf-8")
    (aa / "data-knowledge.yaml").write_text(
        yaml.safe_dump(bundle.data_knowledge, sort_keys=False), encoding="utf-8"
    )
    (aa / "policy.yaml").write_text(_policy_text(), encoding="utf-8")
    for rel in bundle.adapter_paths:
        src = bundle.root / rel
        dest = project / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    skill_rel = _SKILL_BY_LAYER[bundle.layer]
    skill_dest = project / skill_rel
    skill_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle.skill_path, skill_dest)
    return change_dir


def _codegen_claims(bundle: CanonicalAssuranceBundle) -> ResourceClaims:
    profile = get_layer_assurance_profile(bundle.layer)
    gf = get_generated_files_contract(bundle.layer)
    reads = [ResourcePath.parse(f"change:{path}") for path in profile.plan_artifacts]
    reads.append(ResourcePath.parse(f"change:{bundle.summary_relpath}"))
    reads.append(ResourcePath.parse(f"change:{profile.review_artifact}"))
    reads.append(ResourcePath.parse("change:cases/**"))
    reads.append(ResourcePath.parse("repo:.aa/data-knowledge.yaml"))
    reads.append(ResourcePath.parse("repo:.aa/config.yaml"))
    reads.append(ResourcePath.parse("repo:tests/testdata/domain/**"))
    private = gf.private_test_root.replace("\\", "/")
    reads.append(ResourcePath.parse(f"repo:{private}/**"))
    writes = [
        ResourcePath.parse(gf.summary_path),
        ResourcePath.parse(gf.manifest_path),
        ResourcePath.parse(f"repo:{private}/**"),
        ResourcePath.parse("repo:tests/testdata/domain/**"),
    ]
    return ResourceClaims(
        reads=tuple(reads),
        writes=tuple(writes),
        authorization_writes=tuple(writes),
    )


def _build_manifest(bundle: CanonicalAssuranceBundle) -> GeneratedFilesV1:
    gf = get_generated_files_contract(bundle.layer)
    model = _GENERATED_MODELS[bundle.layer]
    private = gf.private_test_root.replace("\\", "/")
    case_id = "CASE_001"
    for doc in bundle.cases:
        added = doc.get("added")
        if isinstance(added, list) and added:
            first = added[0]
            if isinstance(first, Mapping) and isinstance(first.get("case_id"), str):
                case_id = first["case_id"]
                break
    repo_path = f"{private}/test_canonical_{bundle.layer}.py"
    digest = sha256_bytes(f"canonical-{bundle.layer}-{case_id}\n".encode())
    return model.model_validate(
        {
            "schema_version": "1",
            "change_id": bundle.change_id,
            "layer": bundle.layer,
            "files": [
                {
                    "repo_path": repo_path,
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": [case_id],
                    "content_sha256": digest,
                }
            ],
        }
    )


def observe_assurance_contract(
    bundle: CanonicalAssuranceBundle,
    *,
    tmp_path: Path,
    skill_path: Path | None = None,
    review_bytes: bytes | None = None,
    plan_texts: dict[str, str] | None = None,
    data_knowledge: dict[str, object] | None = None,
    required_capabilities: tuple[str, ...] | None = None,
    checks_override: PlanCheckDocument | None = None,
    malformed_checks: PlanCheckDocument | None = None,
    attempt_undeclared_read: str | None = None,
    attempt_forbidden_write: str | None = None,
    manifest_layer: str | None = None,
) -> AssuranceContractObservation:
    """Drive one bundle across production boundaries and record observations."""
    working = replace(
        bundle,
        plan_texts=plan_texts if plan_texts is not None else dict(bundle.plan_texts),
        data_knowledge=data_knowledge if data_knowledge is not None else deepcopy(bundle.data_knowledge),
        review_bytes=review_bytes if review_bytes is not None else bundle.review_bytes,
        required_capabilities=(
            required_capabilities if required_capabilities is not None else bundle.required_capabilities
        ),
        skill_path=skill_path if skill_path is not None else bundle.skill_path,
    )
    boundaries: list[BoundaryObservation] = []

    # skill_contract
    try:
        sections = parse_skill_contract_sections(working.skill_path)
        required_inputs = [
            path for group in sections.inputs if group.name == "required" for path in group.paths
        ]
        missing = []
        for logical in required_inputs:
            if logical.startswith("change:cases/"):
                if not any((working.root / "cases").glob("**/case.yaml")):
                    missing.append(logical)
                continue
            if logical.startswith("change:"):
                rel = logical[len("change:") :]
                if "*" in rel:
                    continue
                if not (working.root / rel).exists() and rel not in working.plan_texts:
                    # summary may be outside plan_artifacts
                    if not (working.root / rel).is_file():
                        missing.append(logical)
                continue
            if logical.startswith("repo:"):
                rel = logical[len("repo:") :]
                if "*" in rel:
                    continue
                if not (working.root / rel).is_file():
                    missing.append(logical)
        if missing:
            boundaries.append(
                BoundaryObservation(
                    boundary="skill_contract",
                    status="reject",
                    code="required_input_missing",
                    locator=missing[0],
                    detail="structured required skill input missing from fixture",
                )
            )
            return _rejected_observation(working, boundaries)
        boundaries.append(BoundaryObservation(boundary="skill_contract", status="ok"))
    except SkillContractParseError as exc:
        boundaries.append(
            BoundaryObservation(
                boundary="skill_contract",
                status="reject",
                code="skill_contract_invalid",
                locator=str(working.skill_path),
                detail=str(exc),
            )
        )
        return _rejected_observation(working, boundaries)

    # authoring_ingest
    try:
        authoring = PlanReviewAuthoring.model_validate_json(working.review_bytes)
        boundaries.append(BoundaryObservation(boundary="authoring_ingest", status="ok"))
    except Exception as exc:  # noqa: BLE001 - boundary capture
        boundaries.append(
            BoundaryObservation(
                boundary="authoring_ingest",
                status="reject",
                code="authoring_ingest_invalid",
                locator=get_layer_assurance_profile(working.layer).review_artifact,
                detail=str(exc),
            )
        )
        return _rejected_observation(working, boundaries)

    # runtime_ingest + freeze (authoring -> runtime model -> canonical bytes equality)
    try:
        frozen_payload = authoring.model_dump(mode="json")
        frozen_review = PlanReview.model_validate(frozen_payload)
        first = canonical_json_bytes(frozen_review)
        second = canonical_json_bytes(PlanReview.model_validate_json(first))
        if first != second:
            raise ValueError("runtime review wire bytes drifted after reload")
        if frozen_review.review_type != f"{working.layer}-plan":
            raise ValueError(f"wrong review layer: {frozen_review.review_type}")
        if frozen_review.change_id != working.change_id:
            raise ValueError(f"wrong review change_id: {frozen_review.change_id}")
        boundaries.append(BoundaryObservation(boundary="runtime_ingest", status="ok"))
        boundaries.append(
            BoundaryObservation(
                boundary="freeze",
                status="ok",
                detail=sha256_bytes(first),
            )
        )
    except Exception as exc:  # noqa: BLE001
        code = "runtime_ingest_invalid"
        if "wrong review layer" in str(exc):
            code = "wrong_review_layer"
        elif "wrong review change_id" in str(exc):
            code = "wrong_review_change"
        boundaries.append(
            BoundaryObservation(
                boundary="runtime_ingest",
                status="reject",
                code=code,
                locator=get_layer_assurance_profile(working.layer).review_artifact,
                detail=str(exc),
            )
        )
        return _rejected_observation(working, boundaries, authoring_review=authoring)

    # applicability + mechanical + wire
    try:
        profile = get_layer_assurance_profile(working.layer)
        applicability = derive_layer_applicability(working.cases, profile)
        boundaries.append(BoundaryObservation(boundary="applicability", status="ok"))
        checks = run_plan_checks(check_context_for_bundle(working), applicability=applicability)
        wire_once = canonical_json_bytes(checks)
        wire_twice = canonical_json_bytes(PlanCheckDocument.model_validate_json(wire_once))
        if wire_once != wire_twice:
            raise ValueError("plan check wire bytes drifted after reload")
        if malformed_checks is not None:
            validate_plan_check_document(malformed_checks, profile)
            raise ValueError("malformed N/A: expected validate_plan_check_document to reject")
        if checks_override is not None:
            override_bytes = canonical_json_bytes(checks_override)
            if override_bytes != wire_once:
                boundaries.append(BoundaryObservation(boundary="mechanical", status="ok"))
                boundaries.append(
                    BoundaryObservation(
                        boundary="wire",
                        status="reject",
                        code="stale_checks",
                        locator=profile.checks_artifact,
                        detail="provided checks bytes differ from freshly produced mechanical output",
                    )
                )
                return _rejected_observation(
                    working,
                    boundaries,
                    authoring_review=authoring,
                    frozen_review=frozen_review,
                    applicability=applicability,
                    checks=checks,
                )
            checks = checks_override
        expected = expected_check_statuses(working.layer)
        for check_id, expected_status in expected.items():
            item = next(c for c in checks.checks if c.check_id == check_id)
            if item.status == "fail":
                raise ValueError(f"mechanical check failed: {check_id}")
            if item.status != expected_status:
                raise ValueError(f"unexpected status for {check_id}: {item.status}")
        if tuple(item.check_id for item in checks.checks) != PLAN_CHECK_IDS:
            raise ValueError("mechanical checks out of production order")
        for check_id in profile.applicable_check_ids:
            item = next(c for c in checks.checks if c.check_id == check_id)
            if item.status == "not_applicable":
                raise ValueError(f"applicable check unexpectedly N/A: {check_id}")
        for check_id in PLAN_CHECK_IDS:
            if check_id in profile.applicable_check_ids:
                continue
            item = next(c for c in checks.checks if c.check_id == check_id)
            if item.applicability_reason != "check_not_in_profile":
                raise ValueError("malformed N/A: assert_ideal must be check_not_in_profile")
        boundaries.append(BoundaryObservation(boundary="mechanical", status="ok"))
        boundaries.append(BoundaryObservation(boundary="wire", status="ok"))
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        code = "mechanical_invalid"
        if "malformed N/A" in detail or "check_not_in_profile" in detail:
            code = "malformed_na"
        elif "mechanical check failed" in detail:
            code = detail.rsplit(": ", 1)[-1]
        elif "capability_keys" in detail or "missing_capability" in detail:
            code = "missing_capability"
        boundaries.append(
            BoundaryObservation(
                boundary="mechanical",
                status="reject",
                code=code,
                locator=get_layer_assurance_profile(working.layer).checks_artifact,
                detail=detail,
            )
        )
        return _rejected_observation(
            working,
            boundaries,
            authoring_review=authoring,
            frozen_review=frozen_review,
        )

    # plan_gate + codegen_precondition
    project = tmp_path / f"proj-{working.layer}"
    change_dir = _materialize_project(working, project)
    if attempt_undeclared_read is not None:
        # Present on the full tree but outside codegen claims.
        sibling = change_dir / "review" / "sibling-layer-review.json"
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_text('{"sibling":true}\n', encoding="utf-8")
    profile = get_layer_assurance_profile(working.layer)
    checks_path = change_dir / profile.checks_artifact
    checks_path.parent.mkdir(parents=True, exist_ok=True)
    checks_path.write_bytes(canonical_json_bytes(checks))
    gates = _gates_for(working.layer)
    gate_ctx = GateEvaluationContext(
        project_root=project,
        repo_root=project,
        change_dir=change_dir,
        change_id=working.change_id,
        params={"force_continue": False},
        state_values={},
        node_results={"review-cycle": {"status": "succeeded"}},
        artifact_overrides={
            profile.review_artifact: frozen_review.model_dump(mode="json"),
            profile.checks_artifact: checks.model_dump(mode="json"),
            "repo:.aa/data-knowledge.yaml": working.data_knowledge,
        },
    )
    plan_report = check_gate_in_view(gates, profile.gate_id, gate_ctx)
    if plan_report.verdict.value != "pass":
        boundaries.append(
            BoundaryObservation(
                boundary="plan_gate",
                status="reject",
                code="plan_gate_rejected",
                locator=profile.gate_id,
                detail=plan_report.verdict.value,
            )
        )
        return _rejected_observation(
            working,
            boundaries,
            authoring_review=authoring,
            frozen_review=frozen_review,
            applicability=applicability,
            checks=checks,
            plan_gate_verdict=plan_report.verdict.value,
        )
    boundaries.append(BoundaryObservation(boundary="plan_gate", status="ok"))

    precheck_id = f"{working.layer}-codegen-precondition-gate"
    precheck_report = check_gate_in_view(gates, precheck_id, gate_ctx)
    if precheck_report.verdict.value != "pass":
        boundaries.append(
            BoundaryObservation(
                boundary="codegen_precondition",
                status="reject",
                code="codegen_precondition_rejected",
                locator=precheck_id,
                detail=precheck_report.verdict.value,
            )
        )
        return _rejected_observation(
            working,
            boundaries,
            authoring_review=authoring,
            frozen_review=frozen_review,
            applicability=applicability,
            checks=checks,
            plan_gate_verdict=plan_report.verdict.value,
            codegen_precondition_verdict=precheck_report.verdict.value,
        )
    boundaries.append(BoundaryObservation(boundary="codegen_precondition", status="ok"))

    # codegen-workspace visibility + optional undeclared/forbidden mutations
    store = TreeStore(change_dir)
    tree_id = store.capture(project)
    claims = _codegen_claims(working)
    workspace = WorkspaceBackend(change_dir).create(
        task_id=f"codegen-{working.layer}",
        base_tree_id=tree_id,
        store=store,
        claims=claims,
        declared_reads_only=True,
        initialize_git=False,
    )
    visible: list[str] = []
    try:
        for logical in (
            *(f"change:{path}" for path in profile.plan_artifacts),
            f"change:{working.summary_relpath}",
            f"change:{profile.review_artifact}",
            "repo:.aa/data-knowledge.yaml",
            "repo:.aa/config.yaml",
        ):
            if logical.startswith("change:"):
                candidate = workspace.change_dir / logical[len("change:") :]
            else:
                candidate = workspace.repo_root / logical[len("repo:") :]
            if not candidate.is_file():
                raise WorkspaceError(f"contract_read_missing:{logical}")
            visible.append(logical)
        if attempt_undeclared_read is not None:
            rel = attempt_undeclared_read.removeprefix("change:")
            leaked = workspace.change_dir / rel
            if leaked.exists():
                raise WorkspaceError(f"undeclared_read_visible:{attempt_undeclared_read}")
            boundaries.append(
                BoundaryObservation(
                    boundary="workspace_read",
                    status="reject",
                    code="undeclared_read",
                    locator=attempt_undeclared_read,
                    detail="sibling/undeclared path is not visible under declared_only claims",
                )
            )
            return _rejected_observation(
                working,
                boundaries,
                authoring_review=authoring,
                frozen_review=frozen_review,
                applicability=applicability,
                checks=checks,
                plan_gate_verdict=plan_report.verdict.value,
                codegen_precondition_verdict=precheck_report.verdict.value,
                visible_codegen_inputs=tuple(visible),
            )
        boundaries.append(BoundaryObservation(boundary="workspace_read", status="ok"))
    except WorkspaceError as exc:
        code = "contract_read_missing"
        if "undeclared_read" in str(exc):
            code = "undeclared_read"
        boundaries.append(
            BoundaryObservation(
                boundary="workspace_read",
                status="reject",
                code=code,
                locator=str(exc).split(":", 1)[-1],
                detail=str(exc),
            )
        )
        return _rejected_observation(
            working,
            boundaries,
            authoring_review=authoring,
            frozen_review=frozen_review,
            applicability=applicability,
            checks=checks,
            plan_gate_verdict=plan_report.verdict.value,
            codegen_precondition_verdict=precheck_report.verdict.value,
            visible_codegen_inputs=tuple(visible),
        )

    gf = get_generated_files_contract(working.layer)
    summary_rel = gf.summary_path.removeprefix("change:")
    manifest_rel = gf.manifest_path.removeprefix("change:")
    summary_file = workspace.change_dir / summary_rel
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.write_text(f"# {working.layer} codegen summary\n", encoding="utf-8")
    try:
        layer_for_manifest = manifest_layer or working.layer
        if layer_for_manifest != working.layer:
            # Force wrong layer literal through the current layer model.
            payload = _build_manifest(working).model_dump(mode="json")
            payload["layer"] = layer_for_manifest
            _GENERATED_MODELS[working.layer].model_validate(payload)
            raise AssertionError("expected layer mismatch to fail")
        manifest = _build_manifest(working)
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_file = workspace.change_dir / manifest_rel
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_bytes(manifest_bytes)
        private = gf.private_test_root.replace("\\", "/")
        test_path = workspace.repo_root / private / f"test_canonical_{working.layer}.py"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(f"def test_canonical_{working.layer}():\n    assert True\n", encoding="utf-8")
        if attempt_forbidden_write is not None:
            forbidden = workspace.repo_root / attempt_forbidden_write.removeprefix("repo:")
            forbidden.parent.mkdir(parents=True, exist_ok=True)
            forbidden.write_text("forbidden\n", encoding="utf-8")
            store.freeze_write_set(workspace, claims=claims)
            raise AssertionError("expected forbidden write to fail freeze")
        write_set = store.freeze_write_set(workspace, claims=claims)
        digests = tuple(sorted((entry.logical_path, entry.after_sha256 or "") for entry in write_set.entries))
        boundaries.append(BoundaryObservation(boundary="workspace_write", status="ok"))
        boundaries.append(BoundaryObservation(boundary="codegen_workspace", status="ok"))
        manifest_sha = sha256_bytes(manifest_bytes)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        code = "workspace_write_failed"
        boundary = "workspace_write"
        locator = attempt_forbidden_write or manifest_rel
        if "forbidden write" in detail.casefold() or attempt_forbidden_write is not None:
            code = "forbidden_write"
        elif "layer" in detail.casefold() or "validation error" in detail.casefold():
            code = "wrong_summary_manifest_layer"
            boundary = "codegen_workspace"
            locator = gf.manifest_path
        boundaries.append(
            BoundaryObservation(
                boundary=boundary,
                status="reject",
                code=code,
                locator=locator,
                detail=detail,
            )
        )
        return _rejected_observation(
            working,
            boundaries,
            authoring_review=authoring,
            frozen_review=frozen_review,
            applicability=applicability,
            checks=checks,
            plan_gate_verdict=plan_report.verdict.value,
            codegen_precondition_verdict=precheck_report.verdict.value,
            visible_codegen_inputs=tuple(visible),
        )

    return AssuranceContractObservation(
        layer=working.layer,
        authoring_review=authoring,
        frozen_review=frozen_review,
        applicability=applicability,
        checks=checks,
        plan_gate_verdict=plan_report.verdict.value,
        codegen_precondition_verdict=precheck_report.verdict.value,
        visible_codegen_inputs=tuple(visible),
        frozen_output_digests=digests,
        generated_files_manifest_sha256=manifest_sha,
        candidate_validation_receipt_id="",
        boundaries=tuple(boundaries),
    )


def _rejected_observation(
    bundle: CanonicalAssuranceBundle,
    boundaries: list[BoundaryObservation],
    *,
    authoring_review: PlanReviewAuthoring | None = None,
    frozen_review: PlanReview | None = None,
    applicability: LayerApplicability | None = None,
    checks: PlanCheckDocument | None = None,
    plan_gate_verdict: str = "",
    codegen_precondition_verdict: str = "",
    visible_codegen_inputs: tuple[str, ...] = (),
) -> AssuranceContractObservation:
    profile = get_layer_assurance_profile(bundle.layer)
    if authoring_review is None:
        # Minimal placeholder when authoring itself failed; tests inspect boundaries.
        authoring_review = PlanReviewAuthoring.model_validate(
            {
                "schema_version": "1.0",
                "review_type": f"{bundle.layer}-plan",
                "change_id": bundle.change_id,
                "decision": "pass",
                "findings": [],
                "auto_fix_plan": [],
                "next_action": "continue",
                "auto_fix_allowed": False,
                "human_review_required": False,
                "codegen_readiness": "ready",
                "risk_level": "low",
                "required_capabilities": list(bundle.required_capabilities)
                or ["capabilities.domain_factories.account.make_account"],
            }
        )
    if frozen_review is None:
        try:
            frozen_review = PlanReview.model_validate(authoring_review.model_dump(mode="json"))
        except Exception:  # noqa: BLE001
            frozen_review = PlanReview.model_validate(
                {
                    **authoring_review.model_dump(mode="json"),
                    "summary": "rejected-observation-placeholder",
                    "reviewed_files": [profile.review_artifact],
                    "blockers": [],
                    "needs_review": [],
                    "created_at": "2026-08-01T00:00:00Z",
                }
            )
    if applicability is None:
        applicability = LayerApplicability(
            layer=bundle.layer,
            applicable=False,
            reason_code="no_automated_cases",
            case_ids=(),
        )
    if checks is None:
        checks = PlanCheckDocument.from_checks(
            layer=bundle.layer,
            applicability=applicability,
            checks=tuple(
                CheckEvidence(
                    check_id=check_id,
                    status="not_applicable",
                    applicability_reason="layer_not_applicable",
                )
                for check_id in PLAN_CHECK_IDS
            ),
        )
    return AssuranceContractObservation(
        layer=bundle.layer,
        authoring_review=authoring_review,
        frozen_review=frozen_review,
        applicability=applicability,
        checks=checks,
        plan_gate_verdict=plan_gate_verdict,
        codegen_precondition_verdict=codegen_precondition_verdict,
        visible_codegen_inputs=visible_codegen_inputs,
        frozen_output_digests=(),
        generated_files_manifest_sha256="",
        candidate_validation_receipt_id="",
        boundaries=tuple(boundaries),
    )


def boundary_map(observation: AssuranceContractObservation) -> dict[str, BoundaryObservation]:
    return {item.boundary: item for item in observation.boundaries}


def non_owner_boundaries_equal(
    mutated: AssuranceContractObservation,
    canonical: AssuranceContractObservation,
    *,
    owner: str,
) -> bool:
    """Compare boundary statuses for every boundary that precedes/ besides the owner."""
    canonical_map = boundary_map(canonical)
    mutated_map = boundary_map(mutated)
    for name, canonical_boundary in canonical_map.items():
        if name == owner:
            continue
        mutated_boundary = mutated_map.get(name)
        if mutated_boundary is None:
            # Owner rejected earlier; later canonical boundaries are absent — acceptable.
            owner_boundary = mutated_map.get(owner)
            if owner_boundary is not None and owner_boundary.status == "reject":
                continue
            return False
        if mutated_boundary.status != canonical_boundary.status:
            return False
    return True


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "ASSURANCE_FIXTURE_ROOT",
    "AssuranceContractObservation",
    "BoundaryObservation",
    "CANONICAL_CHANGE_ID",
    "CanonicalAssuranceBundle",
    "REPO_ROOT",
    "SkillContractParseError",
    "SkillContractSections",
    "SkillPathGroup",
    "assert_fixture_independence",
    "boundary_map",
    "check_context_for_bundle",
    "expected_check_statuses",
    "fixture_root_for",
    "load_canonical_assurance_bundle",
    "non_owner_boundaries_equal",
    "observe_assurance_contract",
    "parse_skill_contract_sections",
    "sha256_file",
]
