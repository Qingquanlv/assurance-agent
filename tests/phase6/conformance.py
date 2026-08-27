"""Shared models and fixed source mappings for Phase 6 admission evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Literal, cast

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel
from pydantic import Field, ValidationError


class ChangeLocalWaiverV1(FrozenModel):
    waiver_id: str
    disposition: Literal["carried_forward", "deferred_out_of_scope"]
    replacement_tasks: tuple[int, ...]
    approved_by: Literal["user"]
    evidence: tuple[str, ...]


class ChangeLocalAdmissionV1(FrozenModel):
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upstream_verdict: Literal["accepted", "not_accepted"]
    admission_status: Literal["complete", "accepted_with_waivers"]
    waivers: tuple[ChangeLocalWaiverV1, ...]


class ResidualDispositionV1(FrozenModel):
    source_plan: str
    source_task: str
    disposition: Literal[
        "verified_complete",
        "carried_forward",
        "superseded",
        "deferred_out_of_scope",
    ]
    replacement_task: int | None
    evidence: tuple[str, ...]


REQUIRED_WAIVERS = {
    "change-local-openchamber-direct-discovery": ("deferred_out_of_scope", ()),
    "change-local-repository-type-package-gates": ("carried_forward", (4, 14)),
    "change-local-opencode-live": ("carried_forward", (3, 13)),
    "change-local-export-idempotency-live-proof": ("carried_forward", (12, 13)),
    "change-local-dirty-tree-closeout": ("carried_forward", (4, 14)),
    "change-local-stale-acceptance-refresh": ("carried_forward", (14,)),
    "change-local-graph-inventory": ("deferred_out_of_scope", ()),
    "change-local-unused-cli-flag": ("deferred_out_of_scope", ()),
    "change-local-skill-wording": ("deferred_out_of_scope", ()),
}

EXPECTED_RESIDUAL_MAPPINGS = {
    ("phase-1", "completed"): ("verified_complete", None),
    ("phase-2", "final 6 commits"): ("carried_forward", 2),
    ("phase-3", "main body"): ("verified_complete", None),
    ("phase-3", "OpenCode live"): ("carried_forward", 3),
    ("phase-3", "Cursor live"): ("deferred_out_of_scope", None),
    ("phase-4", "main body"): ("verified_complete", None),
    ("phase-5", "Tasks 1-23"): ("verified_complete", None),
    ("phase-5", "Task 24"): ("carried_forward", 3),
    ("phase-5", "Task 25"): ("deferred_out_of_scope", None),
    ("phase-5", "Task 26"): ("verified_complete", None),
    ("phase-5", "Task 27"): ("verified_complete", None),
    ("phase-6", "Task 1"): ("carried_forward", 1),
    ("phase-6", "Tasks 2-5"): ("superseded", 6),
    ("phase-6", "Task 6"): ("carried_forward", 7),
    ("phase-6", "Task 7"): ("carried_forward", 10),
    ("phase-6", "Task 8"): ("carried_forward", 8),
    ("phase-6", "Task 9"): ("carried_forward", 9),
    ("phase-6", "Task 10"): ("carried_forward", 10),
    ("phase-6", "Tasks 11-12"): ("carried_forward", 11),
    ("phase-6", "Tasks 13-14"): ("carried_forward", 12),
    ("phase-6", "Task 15"): ("carried_forward", 13),
    ("phase-6", "Task 16"): ("deferred_out_of_scope", None),
    ("phase-6", "Tasks 17-18"): ("carried_forward", 14),
}

_SHA256 = r"^[0-9a-f]{64}$"
_COMMIT = r"^[0-9a-f]{40}$"
HANDOFF_SOURCE_COMMIT = "e884bb88aadb9b3016f856c0c1a4b4ff3f351538"
PHASE5_SDD = Path(".superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly")
CLOSEOUT_SDD = Path(".superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout")
PHASE4_SDD = Path(".superpowers/sdd/2026-08-22-pure-graph-engine-phase4-assurance-capability-extraction")
PHASE5_HANDOFF_PATH = PHASE5_SDD / "phase6-handoff.json"
PHASE5_ACCEPTANCE_PATH = PHASE5_SDD / "acceptance.json"
PHASE5_PROGRESS_PATH = PHASE5_SDD / "progress.md"
ADMISSION_RELATIVE_PATH = CLOSEOUT_SDD / "admission.json"
TASK4_REPORT_RELATIVE_PATH = CLOSEOUT_SDD / "task-4-report.md"
PHASE4_INVENTORY_RELATIVE_PATH = PHASE4_SDD / "phase6-deletion.txt"

GRAPH_SOURCE_PREFIXES = ("packages/graph-engine",)
PRODUCT_SOURCE_PREFIXES = ("packages/assurance-product",)
CAPABILITY_SOURCE_PREFIXES = {
    "assurance_intake": ("packages/assurance-intake",),
    "assurance_generation": ("packages/assurance-generation",),
    "assurance_execution": ("packages/assurance-execution",),
    "assurance_healing": ("packages/assurance-healing",),
    "assurance_quality": ("packages/assurance-quality",),
    "assurance_improvement": ("packages/assurance-improvement",),
}
ADAPTER_SOURCE_PREFIXES = {
    "agent_runtime_contracts": ("packages/agent-runtime-contracts",),
    "agent_runtime_opencode": ("packages/agent-runtime-opencode",),
    "agent_runtime_cursor": ("packages/agent-runtime-cursor",),
}
DEPLOYMENT_SOURCE_PREFIXES = (
    "packages/assurance-product/assurance_product/resources/declarations",
    "packages/assurance-product/assurance_product/binding_builder.py",
    "packages/assurance-product/assurance_product/product-declaration-opencode.json",
    "packages/assurance-product/assurance_product/product-declaration-cursor.json",
)
REQUIRED_ACTIONS = (
    "freeze_new_legacy_starts",
    "drain_or_audit_terminate_legacy_invocations",
    "switch_aa_to_assurance_product",
    "remove_aa_next_name",
    "decide_and_implement_safe_in_place_result_application_if_required",
    "delete_legacy_runtime_and_product_implementation",
    "delete_product_hooks_catalogs_old_entrypoints_resources_and_obsolete_tests",
    "remove_comparison_only_compatibility_baseline_where_unneeded",
    "add_no_old_invocation_resume_adapter_or_forwarding_import",
)
REQUIRED_UNRESOLVED_FRAGMENTS = (
    "combined-suite isolation",
    "MinimumCoverageMatrixAuthoring",
    "opencode-terminal-before-restart",
    "opencode-provider-state-deleted-after-receipt",
)
UNRESOLVED_GATE_ITEMS = (
    "combined-suite isolation: six cursor composition/audit nodes fail only in the combined Step 4 suite",
    "committed-HEAD smoke ImportError: MinimumCoverageMatrixAuthoring",
    "opencode-terminal-before-restart",
    "opencode-provider-state-deleted-after-receipt",
)
ADMITTED_PROVIDER_STATUSES = frozenset({"admitted", "achieved", "complete", "passed"})
FORBIDDEN_RESULT_TREE_KEYS = frozenset(
    {
        "tree_id",
        "head_tree_id",
        "initial_tree_id",
        "head",
        "head_pointer",
        "workspace_head",
        "result_tree",
        "whole_tree_export_digest",
        "whole_tree_digest",
        "export_tree_id",
        "head_tree",
        "workspace_trees",
    }
)
SECRET_KEY_TOKENS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "authorization",
        "credential",
        "credentials",
        "cookie",
        "private_key",
    }
)
SPEC_CRITERIA = (
    "Phase 4's six wheels remain independently buildable and isolated",
    "assurance-product builds without legacy runtime dependencies",
    "both product entry points have exact static/live declarations",
    "each product resolves only six capability wheels, its selected adapter, one explicit deployment binding wheel, and one explicit project configuration tree",
    "the unselected adapter is absent from the frozen composition",
    "all 99 canonical agent-triplet aliases resolve exactly",
    "every one of 33 routing assignments is exact and has no fallback",
    "the deterministic builder rejects executable input and emits a wheel whose static/live declaration, source digest, and full contribution agree",
    "the project configuration tree contains no binding, model, endpoint, executable, permission, or secret authority",
    "root input and initial SUT tree are authenticated and replayable",
    "graph input projection supplies every Phase 4 handler's declared business contract without product glue handlers",
    "the production host runs installed composed handlers rather than test-constructed replacements",
    "terminal receipts, secrets, descendants, and workspaces pass the full recovery/security matrix",
    "OpenCode and Cursor installed handlers receive exact locked adapter binding data",
    "one Phase 3 provider-live fixture succeeds through each adapter",
    "the complete product graph exposes and runs every public entrypoint",
    "API, E2E, Fuzz, and Performance selected branches cannot be silently omitted",
    "coverage repair, report, issue, healing, archive, Retro, and Improvement paths have execution evidence",
    "business STOP is distinct from engine failure and nested STOP propagates correctly",
    "old/new comparison cases satisfy their declared equivalence modes",
    "one complete OpenCode and one complete Cursor single-item Assurance benchmark reach their expected terminal outcome",
    "post-success replay and comparison no longer require retained provider state",
    "no secret appears in any persisted or reported surface",
    "committed-HEAD wheel isolation and repository gates pass",
    "current aa still runs the legacy product by default",
    "no old/new import or persisted-state bridge exists",
    "the Phase 6 cutover/deletion handoff is complete and mechanically checked",
)


class CapabilitySourceDigestsV1(FrozenModel):
    assurance_intake: str = Field(pattern=_SHA256)
    assurance_generation: str = Field(pattern=_SHA256)
    assurance_execution: str = Field(pattern=_SHA256)
    assurance_healing: str = Field(pattern=_SHA256)
    assurance_quality: str = Field(pattern=_SHA256)
    assurance_improvement: str = Field(pattern=_SHA256)


class AdapterSourceDigestsV1(FrozenModel):
    agent_runtime_contracts: str = Field(pattern=_SHA256)
    agent_runtime_opencode: str = Field(pattern=_SHA256)
    agent_runtime_cursor: str = Field(pattern=_SHA256)


class SourceDigestSetV1(FrozenModel):
    graph: str = Field(pattern=_SHA256)
    product: str = Field(pattern=_SHA256)
    capability: CapabilitySourceDigestsV1
    adapter: AdapterSourceDigestsV1
    deployment: str = Field(pattern=_SHA256)


class BoundFileDigestV1(FrozenModel):
    path: str
    digest: str = Field(pattern=_SHA256)


class ChangeLocalAdmissionBindingV1(FrozenModel):
    path: str
    digest: str = Field(pattern=_SHA256)
    admission_status: Literal["accepted_with_waivers"]


class ProviderRecordV1(FrozenModel):
    status: Literal["incomplete", "deferred_out_of_scope"]
    disposition: Literal["carried_forward", "deferred_out_of_scope"]
    replacement_tasks: tuple[int, ...]
    session: None
    publish_receipt: None
    terminal: None


class ProviderEvidenceV1(FrozenModel):
    opencode: ProviderRecordV1
    cursor: ProviderRecordV1


class GateEvidenceV1(FrozenModel):
    task: Literal[4]
    local_disposition: Literal["complete"]
    release_disposition: Literal["blocked"]
    source_commit_range: str
    report_path: str
    report_digest: str = Field(pattern=_SHA256)
    unresolved: tuple[str, ...]


class Phase5ToPhase6HandoffV1(FrozenModel):
    schema_version: Literal["1"]
    handoff_id: Literal["phase5-to-phase6"]
    source_commit: str = Field(pattern=_COMMIT)
    compatibility_bridge_allowed: Literal[False]
    cursor_live: Literal["deferred_out_of_scope"]
    source_digests: SourceDigestSetV1
    change_local_admission: ChangeLocalAdmissionBindingV1
    provider_evidence: ProviderEvidenceV1
    gate_evidence: GateEvidenceV1
    phase4_deletion_inventory: BoundFileDigestV1
    required_actions: tuple[str, ...]
    project_roots: tuple[str, ...]


class AcceptanceCriterionV1(FrozenModel):
    criterion: int
    requirement: str
    status: Literal["passed", "locally_gated", "carried_forward", "deferred_out_of_scope", "blocked"]
    evidence_paths: tuple[str, ...]
    commit: str = Field(pattern=_COMMIT)


class Phase5AcceptanceV1(FrozenModel):
    schema_version: Literal["1"]
    source_commit: str = Field(pattern=_COMMIT)
    criteria: tuple[AcceptanceCriterionV1, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _normalize_key(key: str) -> str:
    return key.replace("-", "_").lower()


def _walk_mappings(value: object) -> tuple[tuple[str, object], ...]:
    found: list[tuple[str, object]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found.append((key, item))
                found.extend(_walk_mappings(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_mappings(item))
    return tuple(found)


def _reject_old_result_tree_fields(payload: object) -> None:
    for key, _item in _walk_mappings(payload):
        _require(_normalize_key(key) not in FORBIDDEN_RESULT_TREE_KEYS, "old result-tree field")


def _reject_secret_bearing_payload(payload: object) -> None:
    for key, item in _walk_mappings(payload):
        _require(_normalize_key(key) not in SECRET_KEY_TOKENS, "secret-bearing payload")
        if isinstance(item, str):
            lowered = item.lower()
            _require("api_key" not in lowered, "secret-bearing payload")
            _require(not lowered.startswith("sk-"), "secret-bearing payload")
            _require("bearer " not in lowered, "secret-bearing payload")


def _is_mutable_path(value: str) -> bool:
    candidate = Path(value)
    return (
        not value
        or candidate.is_absolute()
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.parts[0] == "tmp"
        or value != candidate.as_posix()
    )


def _collect_path_values(payload: object) -> tuple[str, ...]:
    values: list[str] = []
    if isinstance(payload, dict):
        for key, item in payload.items():
            if isinstance(key, str) and (key == "path" or key.endswith("_path") or key == "evidence_paths"):
                if isinstance(item, str):
                    values.append(item)
                elif isinstance(item, list):
                    values.extend(entry for entry in item if isinstance(entry, str))
            values.extend(_collect_path_values(item))
    elif isinstance(payload, list):
        for item in payload:
            values.extend(_collect_path_values(item))
    return tuple(values)


def _reject_mutable_paths(payload: object) -> None:
    for value in _collect_path_values(payload):
        _require(not _is_mutable_path(value), "mutable path")


def _committed_object_exists(repo_root: Path, commit: str, relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{relative_path}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def committed_file_digest(repo_root: Path, commit: str, relative_path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{commit}:{relative_path}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    _require(result.returncode == 0, f"committed source missing: {relative_path}")
    return hashlib.sha256(result.stdout).hexdigest()


def committed_prefix_digest(repo_root: Path, commit: str, prefixes: tuple[str, ...]) -> str:
    listing: dict[str, str] = {}
    for prefix in prefixes:
        result = subprocess.run(
            ["git", "ls-tree", "-r", commit, "--", prefix],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        _require(result.returncode == 0, f"cannot list committed sources for {prefix}")
        for line in result.stdout.splitlines():
            meta, path = line.split("\t", 1)
            _mode, obj_type, blob_sha = meta.split()
            if obj_type == "blob":
                listing[path] = blob_sha
    _require(bool(listing), f"no committed sources for {prefixes}")
    return canonical_digest(cast("dict[str, JSONValue]", listing))


def expected_source_digests(repo_root: Path, commit: str) -> SourceDigestSetV1:
    return SourceDigestSetV1(
        graph=committed_prefix_digest(repo_root, commit, GRAPH_SOURCE_PREFIXES),
        product=committed_prefix_digest(repo_root, commit, PRODUCT_SOURCE_PREFIXES),
        capability=CapabilitySourceDigestsV1(
            **{
                name: committed_prefix_digest(repo_root, commit, prefixes)
                for name, prefixes in CAPABILITY_SOURCE_PREFIXES.items()
            }
        ),
        adapter=AdapterSourceDigestsV1(
            **{
                name: committed_prefix_digest(repo_root, commit, prefixes)
                for name, prefixes in ADAPTER_SOURCE_PREFIXES.items()
            }
        ),
        deployment=committed_prefix_digest(repo_root, commit, DEPLOYMENT_SOURCE_PREFIXES),
    )


def _reject_missing_or_fabricated_provider_evidence(payload: dict[str, object]) -> None:
    provider = payload.get("provider_evidence")
    if not isinstance(provider, dict):
        raise ValueError("missing provider evidence")
    if "opencode" not in provider or "cursor" not in provider:
        raise ValueError("missing provider evidence")
    for name in ("opencode", "cursor"):
        record = provider[name]
        if not isinstance(record, dict):
            raise ValueError("missing provider evidence")
        status = record.get("status")
        session = record.get("session")
        receipt = record.get("publish_receipt")
        if (
            (isinstance(status, str) and status in ADMITTED_PROVIDER_STATUSES)
            or session not in {None, ""}
            or receipt not in {None, ""}
        ):
            raise ValueError("provider evidence that does not exist")


def _reject_unresolved_gate_claim(payload: dict[str, object]) -> None:
    gate = payload.get("gate_evidence")
    if not isinstance(gate, dict):
        raise ValueError("unresolved gate")
    unresolved = gate.get("unresolved")
    if not isinstance(unresolved, list):
        raise ValueError("unresolved gate")
    if gate.get("release_disposition") != "blocked":
        raise ValueError("unresolved gate")
    joined = "\n".join(item for item in unresolved if isinstance(item, str))
    for fragment in REQUIRED_UNRESOLVED_FRAGMENTS:
        _require(fragment in joined, "unresolved gate")


def _authenticate_bound_paths(repo_root: Path, commit: str, payload: object) -> None:
    for value in _collect_path_values(payload):
        _require(not _is_mutable_path(value), "mutable path")
        _require(_committed_object_exists(repo_root, commit, value), "mutable path")


def _load_mapping(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read handoff document: {error}") from error
    _require(isinstance(payload, dict), "handoff document must be a JSON object")
    return payload


def parse_phase5_handoff(path: Path, *, repo_root: Path) -> Phase5ToPhase6HandoffV1:
    payload = _load_mapping(path)
    _reject_old_result_tree_fields(payload)
    _reject_secret_bearing_payload(payload)
    _reject_mutable_paths(payload)
    _reject_missing_or_fabricated_provider_evidence(payload)
    _reject_unresolved_gate_claim(payload)
    try:
        handoff = Phase5ToPhase6HandoffV1.model_validate(payload)
    except ValidationError as error:
        raise ValueError("invalid phase5 handoff") from error
    _require(
        handoff.source_commit == HANDOFF_SOURCE_COMMIT, "handoff source_commit is not the Task 5 baseline"
    )
    _authenticate_bound_paths(repo_root, handoff.source_commit, payload)
    expected = expected_source_digests(repo_root, handoff.source_commit)
    _require(handoff.source_digests == expected, "source digest mismatch")
    _require(
        handoff.change_local_admission.path == ADMISSION_RELATIVE_PATH.as_posix(),
        "mutable path",
    )
    _require(
        handoff.change_local_admission.digest
        == committed_file_digest(repo_root, handoff.source_commit, handoff.change_local_admission.path),
        "Change-local admission digest mismatch",
    )
    _require(
        handoff.provider_evidence.opencode.status == "incomplete"
        and handoff.provider_evidence.opencode.disposition == "carried_forward"
        and handoff.provider_evidence.opencode.replacement_tasks == (3, 13),
        "missing provider evidence",
    )
    _require(
        handoff.provider_evidence.cursor.status == "deferred_out_of_scope"
        and handoff.cursor_live == "deferred_out_of_scope",
        "Cursor live must remain deferred_out_of_scope",
    )
    _require(handoff.required_actions == REQUIRED_ACTIONS, "handoff required_actions are not exact")
    _require(
        handoff.phase4_deletion_inventory.path == PHASE4_INVENTORY_RELATIVE_PATH.as_posix(),
        "mutable path",
    )
    _require(
        handoff.phase4_deletion_inventory.digest
        == committed_file_digest(repo_root, handoff.source_commit, handoff.phase4_deletion_inventory.path),
        "Phase 4 deletion inventory digest mismatch",
    )
    _require(
        handoff.gate_evidence.report_path == TASK4_REPORT_RELATIVE_PATH.as_posix(),
        "mutable path",
    )
    _require(
        handoff.gate_evidence.report_digest
        == committed_file_digest(repo_root, handoff.source_commit, handoff.gate_evidence.report_path),
        "Task 4 gate evidence digest mismatch",
    )
    return handoff


def parse_phase5_acceptance(path: Path, *, repo_root: Path) -> Phase5AcceptanceV1:
    payload = _load_mapping(path)
    _reject_old_result_tree_fields(payload)
    _reject_secret_bearing_payload(payload)
    _reject_mutable_paths(payload)
    try:
        acceptance = Phase5AcceptanceV1.model_validate(payload)
    except ValidationError as error:
        raise ValueError("invalid phase5 acceptance") from error
    _require(
        acceptance.source_commit == HANDOFF_SOURCE_COMMIT,
        "acceptance source_commit is not the Task 5 baseline",
    )
    _require(
        tuple(item.criterion for item in acceptance.criteria) == tuple(range(1, 28)),
        "acceptance criteria must be 1-27",
    )
    _require(
        tuple(item.requirement for item in acceptance.criteria) == SPEC_CRITERIA,
        "acceptance requirements must match spec Section 24",
    )
    by_number = {item.criterion: item for item in acceptance.criteria}
    _require(by_number[15].status == "carried_forward", "criterion 15 must not claim live admission")
    _require(by_number[21].status == "carried_forward", "criterion 21 must not claim live admission")
    _require(by_number[24].status == "locally_gated", "criterion 24 remains locally gated")
    _require(by_number[25].status == "passed", "criterion 25 must remain the legacy aa default")
    _require(by_number[27].status == "passed", "criterion 27 must be this authenticated handoff")
    _authenticate_bound_paths(repo_root, acceptance.source_commit, payload)
    return acceptance


def handoff_canonical_digest(handoff: Phase5ToPhase6HandoffV1) -> str:
    return canonical_digest(handoff.model_dump(mode="json"))


def build_phase5_handoff_document(repo_root: Path) -> dict[str, JSONValue]:
    commit = HANDOFF_SOURCE_COMMIT
    document = Phase5ToPhase6HandoffV1(
        schema_version="1",
        handoff_id="phase5-to-phase6",
        source_commit=commit,
        compatibility_bridge_allowed=False,
        cursor_live="deferred_out_of_scope",
        source_digests=expected_source_digests(repo_root, commit),
        change_local_admission=ChangeLocalAdmissionBindingV1(
            path=ADMISSION_RELATIVE_PATH.as_posix(),
            digest=committed_file_digest(repo_root, commit, ADMISSION_RELATIVE_PATH.as_posix()),
            admission_status="accepted_with_waivers",
        ),
        provider_evidence=ProviderEvidenceV1(
            opencode=ProviderRecordV1(
                status="incomplete",
                disposition="carried_forward",
                replacement_tasks=(3, 13),
                session=None,
                publish_receipt=None,
                terminal=None,
            ),
            cursor=ProviderRecordV1(
                status="deferred_out_of_scope",
                disposition="deferred_out_of_scope",
                replacement_tasks=(),
                session=None,
                publish_receipt=None,
                terminal=None,
            ),
        ),
        gate_evidence=GateEvidenceV1(
            task=4,
            local_disposition="complete",
            release_disposition="blocked",
            source_commit_range="c5c451c..e884bb8",
            report_path=TASK4_REPORT_RELATIVE_PATH.as_posix(),
            report_digest=committed_file_digest(repo_root, commit, TASK4_REPORT_RELATIVE_PATH.as_posix()),
            unresolved=UNRESOLVED_GATE_ITEMS,
        ),
        phase4_deletion_inventory=BoundFileDigestV1(
            path=PHASE4_INVENTORY_RELATIVE_PATH.as_posix(),
            digest=committed_file_digest(repo_root, commit, PHASE4_INVENTORY_RELATIVE_PATH.as_posix()),
        ),
        required_actions=REQUIRED_ACTIONS,
        project_roots=(),
    )
    return document.model_dump(mode="json")


def _criterion_status(number: int) -> Literal["passed", "locally_gated", "carried_forward"]:
    if number in {15, 21}:
        return "carried_forward"
    if number == 24:
        return "locally_gated"
    return "passed"


def _criterion_evidence(number: int) -> tuple[str, ...]:
    admission = ADMISSION_RELATIVE_PATH.as_posix()
    task4 = TASK4_REPORT_RELATIVE_PATH.as_posix()
    inventory = PHASE4_INVENTORY_RELATIVE_PATH.as_posix()
    binding = (PHASE5_SDD / "binding-coverage.json").as_posix()
    if number in {15, 21}:
        return (admission, task4)
    if number == 24:
        return (task4, "scripts/assurance_product_wheel_smoke_test.sh")
    if number == 26:
        return (task4, inventory)
    if number == 27:
        return (admission, task4, inventory)
    if number == 6:
        return (binding, "tests/phase5/test_phase5_final_repository_gate.py")
    if number in {13, 22, 23}:
        return (
            "tests/phase5/test_phase5_final_security_gate.py",
            "tests/phase5/test_phase5_final_fault_gate.py",
            "tests/phase5/test_replay_properties.py",
        )
    return ("tests/phase5/test_phase5_final_repository_gate.py", task4)


def build_phase5_acceptance_document(repo_root: Path) -> dict[str, JSONValue]:
    _ = repo_root
    document = Phase5AcceptanceV1(
        schema_version="1",
        source_commit=HANDOFF_SOURCE_COMMIT,
        criteria=tuple(
            AcceptanceCriterionV1(
                criterion=number,
                requirement=requirement,
                status=_criterion_status(number),
                evidence_paths=_criterion_evidence(number),
                commit=HANDOFF_SOURCE_COMMIT,
            )
            for number, requirement in enumerate(SPEC_CRITERIA, start=1)
        ),
    )
    return document.model_dump(mode="json")
