"""Authenticate and materialize the complete deterministic Inspect input set."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml
from pydantic import BaseModel, ValidationError

from graph_engine.attempts.context import AuthorizedAttemptScope
from graph_engine.attempts.contracts import ExecutedAttemptResult
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import SecretPort, TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.execution import (
    ExecutionCommandReceiptV1,
    ExecutionReceiptV1,
    RawTestResultV1,
)
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_execution.contracts.selection import SelectedTargets
from assurance_execution.contracts.telemetry import (
    TELEMETRY_COMPLETION_NAME,
    TELEMETRY_OTLP_NAME,
    TelemetryCompletionV1,
    check_trace_requirements,
    parse_otlp_records,
    truncated_trace_observations,
)
from assurance_execution.contracts.verification import (
    EvidenceCompletionV1,
    ManagedSutAuthorityV1,
    ObservationV1,
    VerificationEvidenceV1,
    VerificationManifestV1,
    VerifiedExecutionAuthorityV1,
    VerifiedExecutionResultV1,
    VerifiedProcessReceiptV1,
)
from assurance_execution.contracts.workflow import (
    VerifiedExecutionCycleResultV1,
    VerifiedIncompleteExecutionV1,
)
from assurance_generation.contracts.admission import (
    GenerationAdmissionError,
    admit_verified_generation,
    diagnose_verified_bridge_defect,
)
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1, CaseExecutionPlanV1
from assurance_generation.contracts.families import LayerName
from assurance_intake.contracts.cases import (
    CaseEntryAuthoring,
    CaseYamlAuthoring,
    MinimumCoverageMatrixAuthoring,
)
from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.quality_goals import (
    CoverageGoal,
    MrcCategory,
    MrcLayer,
    PreparedObligationV1,
    journey_keys_from_document,
    normalize_goal_obligations,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.contracts.plan import ResolvedAssurancePlan, decode_plan
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    MaterializeAssessmentInputV1,
)
from assurance_quality.contracts.decisions import classify_inspection_disposition
from assurance_quality.contracts.coverage import MinimumCoverageMatrixRow
from assurance_quality.contracts.goal_policy import (
    ActiveCoverageScopeV1,
    CoverageGoalPolicyV1,
    SufficiencyPolicyV1,
)
from assurance_quality.contracts.metrics import MetricScope, MetricsDocument
from assurance_quality.contracts.issues import (
    IssueEvidenceManifest,
    IssueEvidenceManifestEntry,
    Observation,
    ObservationDocument,
    ObservationSource,
)
from assurance_quality.contracts.pr_metrics import (
    AuthMatrixEvidence,
    ConstraintCoverageEvidence,
    JourneyCoverageEvidence,
)
from assurance_quality.contracts.trace import TraceProjectionV2
from assurance_quality.operations.coverage import (
    MinimumCoverageInput,
    build_coverage_gaps,
    join_minimum_coverage,
)
from assurance_quality.operations.metrics import (
    PrEvidenceBundle,
    RiskInput,
    build_metrics_document,
    resolve_case_risk,
)
from assurance_quality.operations.sufficiency import build_sufficiency_facts
from assurance_quality.operations.trace import TraceCaseInput, TraceOperationInput, project_trace
from assurance_quality.operations.common import json_digest
from assurance_quality.operations.goal_scope import has_layer_evidence, obligation_goal
from assurance_quality.contracts.verification import VerificationObligationV1, VerificationVerdictV1
from assurance_quality.operations.verification import evaluate_verification


from assurance_quality.operations.identity import ObservationIdentityInput, observation_id


class AssessmentInputError(ValueError):
    """The assessment sources could not prove one coherent execution cycle."""


_FAMILY_ORDER = ("api", "e2e", "fuzz", "performance")
_GOAL_ORDER = ("constraint_coverage", "auth_matrix_coverage", "journey_coverage")
_BATCH_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_EXECUTION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_GOAL_RESOURCE_PATHS = {
    "assurance.product.configuration.capability-catalog": ".aa/capability-catalog.json",
    "assurance.product.configuration.data-knowledge": ".aa/data-knowledge.yaml",
}


def _read_ref(root: Path, ref: EvidenceArtifactRefV1) -> bytes:
    path = root
    for part in PurePosixPath(ref.path).parts:
        path = path / part
        if path.is_symlink():
            raise AssessmentInputError(f"assessment input must not contain a symlink: {ref.path}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise AssessmentInputError(f"assessment input is missing: {ref.path}") from error
    if resolved != path or not path.is_file() or path.stat().st_nlink != 1:
        raise AssessmentInputError(f"assessment input must be a regular single-link file: {ref.path}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise AssessmentInputError(f"assessment input digest changed: {ref.path}")
    return data


def _load_json(root: Path, ref: EvidenceArtifactRefV1) -> object:
    try:
        return json.loads(_read_ref(root, ref))
    except json.JSONDecodeError as error:
        raise AssessmentInputError(f"assessment input is not JSON: {ref.path}") from error


def _cases(
    root: Path,
    request: MaterializeAssessmentInputV1,
    *,
    capability_leafs: frozenset[str],
) -> tuple[CaseEntryAuthoring, ...]:
    cases: list[CaseEntryAuthoring] = []
    for ref in request.reviewed_case.case_refs:
        try:
            raw = yaml.safe_load(_read_ref(root, ref))
            if not isinstance(raw, Mapping):
                raise AssessmentInputError(f"reviewed Case inventory is not a mapping: {ref.path}")
            document = CaseYamlAuthoring.model_validate(
                raw,
                context={"capability_leafs": capability_leafs},
            )
        except (yaml.YAMLError, ValidationError) as error:
            raise AssessmentInputError(f"invalid reviewed Case inventory: {ref.path}: {error}") from error
        cases.extend((*document.added, *document.modified))
    active = tuple(case for case in cases if case.status != "deprecated")
    case_ids = [case.case_id for case in active]
    if not active or len(case_ids) != len(set(case_ids)):
        raise AssessmentInputError("reviewed Case inventory must be non-empty with unique case ids")
    return active


def _policy(
    root: Path, request: MaterializeAssessmentInputV1
) -> tuple[CoverageGoalPolicyV1, SufficiencyPolicyV1]:
    if request.policy_resource_id != "assurance.product.configuration.product-policy":
        raise AssessmentInputError("unknown product policy resource")
    path = root / ".aa" / "policy.yaml"
    try:
        data = path.read_bytes()
    except OSError as error:
        raise AssessmentInputError("product policy resource is missing") from error
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise AssessmentInputError("product policy resource must be a regular single-link file")
    if hashlib.sha256(data).hexdigest() != request.policy_sha256:
        raise AssessmentInputError("product policy resource digest changed")
    try:
        raw = yaml.safe_load(data)
        if not isinstance(raw, Mapping):
            raise AssessmentInputError("product policy resource must be a mapping")
        return (
            CoverageGoalPolicyV1.from_product_policy(raw),
            SufficiencyPolicyV1.from_product_policy(raw),
        )
    except (yaml.YAMLError, ValidationError, ValueError) as error:
        if isinstance(error, AssessmentInputError):
            raise
        raise AssessmentInputError(f"policy_error: {error}") from error


def _prepared_obligations(
    root: Path,
    request: MaterializeAssessmentInputV1,
    *,
    plan: ResolvedAssurancePlan,
) -> tuple[tuple[PreparedObligationV1, ...], frozenset[str], frozenset[str]]:
    quality_goal = plan.quality_goal
    try:
        advisory = ExploreAdvisoryV1.model_validate(json.loads(_read_ref(root, quality_goal.obligations_ref)))
    except (json.JSONDecodeError, ValidationError) as error:
        raise AssessmentInputError(f"invalid prepared goal obligations: {error}") from error
    if advisory.change_id != request.reviewed_case.change_id:
        raise AssessmentInputError("prepared goal obligations belong to another change")

    source_bytes: dict[str, bytes] = {}
    for resource_id, digest in quality_goal.source_resource_digests:
        try:
            path = _GOAL_RESOURCE_PATHS[resource_id]
        except KeyError as error:
            raise AssessmentInputError(f"unsupported prepared goal source: {resource_id}") from error
        source_bytes[resource_id] = _read_ref(
            root,
            EvidenceArtifactRefV1(path=path, digest=digest),
        )
    if set(source_bytes) != set(_GOAL_RESOURCE_PATHS):
        raise AssessmentInputError("prepared goals require catalog and data knowledge sources")
    try:
        catalog = json.loads(source_bytes["assurance.product.configuration.capability-catalog"])
        knowledge = yaml.safe_load(source_bytes["assurance.product.configuration.data-knowledge"])
        if not isinstance(catalog, Mapping) or not isinstance(knowledge, Mapping):
            raise AssessmentInputError("prepared goal sources must be mappings")
        raw_leafs = catalog.get("typed_leafs")
        if not isinstance(raw_leafs, list) or any(not isinstance(item, str) for item in raw_leafs):
            raise AssessmentInputError("capability catalog typed_leafs are invalid")
        capability_leafs = frozenset(raw_leafs)
        journey_keys = frozenset(journey_keys_from_document(knowledge))
        obligations = normalize_goal_obligations(
            advisory,
            capability_leafs=capability_leafs,
            journey_keys=journey_keys,
        )
    except (json.JSONDecodeError, yaml.YAMLError, ValidationError, ValueError) as error:
        if isinstance(error, AssessmentInputError):
            raise
        raise AssessmentInputError(f"invalid prepared goal sources: {error}") from error
    return obligations, capability_leafs, journey_keys


def _reviewed_obligations(
    root: Path,
    request: MaterializeAssessmentInputV1,
    cases: tuple[CaseEntryAuthoring, ...],
    *,
    baseline: tuple[PreparedObligationV1, ...],
    capability_leafs: frozenset[str],
    journey_keys: frozenset[str],
) -> tuple[MinimumCoverageMatrixRow, ...]:
    matrix_path = f"qa/changes/{request.reviewed_case.change_id}/trace/minimum-coverage-matrix.json"
    matrix_ref = next(
        (ref for ref in request.reviewed_case.preparation_refs if ref.path == matrix_path),
        None,
    )
    if matrix_ref is None:
        raise AssessmentInputError("Reviewed Case is missing its minimum coverage matrix")
    try:
        matrix = MinimumCoverageMatrixAuthoring.model_validate(json.loads(_read_ref(root, matrix_ref)))
    except (json.JSONDecodeError, ValidationError) as error:
        raise AssessmentInputError(f"invalid reviewed minimum coverage matrix: {error}") from error
    case_by_id = {case.case_id: case for case in cases}
    prepared = {row.key: row for row in baseline}
    result = {row.key: MinimumCoverageMatrixRow(**row.model_dump()) for row in baseline}
    journey_cases: set[str] = set()
    for row in matrix.root:
        unknown = sorted(set(row.covered_by_cases) - set(case_by_id))
        if unknown:
            raise AssessmentInputError(f"minimum coverage matrix references unknown active cases: {unknown}")
        original = prepared.get(row.key)
        category: MrcCategory = (
            original.category
            if original is not None
            else row.category
            or (
                "e2e"
                if row.layer == "e2e" or any(case_by_id[cid].type == "E2E" for cid in row.covered_by_cases)
                else "api"
            )
        )
        layer = (
            original.layer
            if original is not None
            else row.layer or ("e2e" if category in {"e2e", "e2e_if_enabled"} else "api")
        )
        if original is not None and (
            row.category not in {None, original.category} or row.layer not in {None, original.layer}
        ):
            raise AssessmentInputError(f"reviewed MRC scope conflicts with prepared obligation: {row.key}")
        goal = obligation_goal(key=row.key, category=category)
        if goal == "journey_coverage" and row.key not in journey_keys:
            raise AssessmentInputError(f"unknown reviewed journey key: {row.key}")
        if (
            category in {"negative", "data_integrity"}
            or goal in {"constraint_coverage", "auth_matrix_coverage"}
        ) and row.key not in capability_leafs:
            raise AssessmentInputError(f"unknown reviewed closed MRC key: {row.key}")
        related_cases: list[str] = []
        for case_id in sorted(set(row.covered_by_cases)):
            case = case_by_id[case_id]
            if goal == "journey_coverage":
                if case.type != "E2E":
                    continue
                journey_cases.add(case_id)
            elif row.key in capability_leafs and row.key not in case.trace:
                continue
            related_cases.append(case_id)
        result[row.key] = MinimumCoverageMatrixRow(
            mrc_id=original.mrc_id if original is not None else row.mrc_id,
            key=row.key,
            category=category,
            required=row.required or (original.required if original is not None else False),
            layer=layer,
            covered_by_cases=related_cases,
        )
    unmapped_journeys = sorted(
        case.case_id for case in cases if case.type == "E2E" and case.case_id not in journey_cases
    )
    if unmapped_journeys:
        raise AssessmentInputError(f"reviewed E2E Cases have no valid journey mapping: {unmapped_journeys}")
    ids = [row.mrc_id for row in result.values()]
    if len(ids) != len(set(ids)):
        raise AssessmentInputError("reviewed MRC additions reuse a prepared obligation id")
    for key, obligation in result.items():
        if obligation_goal(key=key, category=obligation.category) in {
            "constraint_coverage",
            "auth_matrix_coverage",
        }:
            result[key] = obligation.model_copy(
                update={
                    "covered_by_cases": sorted(
                        case.case_id
                        for case in cases
                        if key in case.trace
                        and (
                            case.type.lower() == obligation.layer
                            or (obligation.layer == "both" and case.type in {"API", "E2E"})
                        )
                    )
                }
            )
    return tuple(sorted(result.values(), key=lambda row: (row.mrc_id, row.key)))


def _metrics(
    *,
    cases: tuple[CaseEntryAuthoring, ...],
    projection: TraceProjectionV2,
    policy_digest: str,
    computed_at: datetime,
    reviewed: Mapping[CoverageGoal, Mapping[str, tuple[str, ...]]],
    required_layers: Mapping[str, MrcLayer],
) -> MetricsDocument:
    trace = projection
    passed = {
        row.case_id
        for row in trace.rows
        if row.freshest_pass is not None and row.freshest_pass.batch_id == trace.authoritative_batch_id
    }
    case_layers = {case.case_id: case.type.lower() for case in cases}

    def covered(key: str, case_ids: tuple[str, ...]) -> bool:
        evidence = set(case_ids) & passed
        layer = required_layers.get(key)
        return bool(evidence) if layer is None else has_layer_evidence(layer, evidence, case_layers)

    def scope(goal: CoverageGoal) -> MetricScope | None:
        obligations = reviewed[goal]
        if not obligations:
            return None
        uncovered = tuple(key for key, case_ids in sorted(obligations.items()) if not covered(key, case_ids))
        return MetricScope.of(
            total=len(obligations),
            covered=len(obligations) - len(uncovered),
            uncovered=uncovered,
        )

    source_digest = json_digest(trace.model_dump(mode="json"))
    constraint = scope("constraint_coverage")
    auth = scope("auth_matrix_coverage")
    journey = scope("journey_coverage")
    risk = resolve_case_risk(cases)
    preliminary = build_metrics_document(
        PrEvidenceBundle(
            change_id=trace.change_id,
            batch_id=trace.authoritative_batch_id,
            computed_at=computed_at,
            policy_digest=policy_digest,
            risk=RiskInput(
                tier=risk.tier,
                lower_bound=risk.lower_bound,
                declared=risk.declared,
            ),
            constraint=(
                ConstraintCoverageEvidence(
                    schema_version="1",
                    change_id=trace.change_id,
                    batch_id=trace.authoritative_batch_id,
                    declared=constraint,
                    value=constraint.value,
                    source_digest=source_digest,
                )
                if constraint is not None
                else None
            ),
            auth=(
                AuthMatrixEvidence(
                    schema_version="1",
                    change_id=trace.change_id,
                    batch_id=trace.authoritative_batch_id,
                    declared=auth,
                    value=auth.value,
                    source_digest=source_digest,
                )
                if auth is not None
                else None
            ),
            journey=(
                JourneyCoverageEvidence(
                    schema_version="1",
                    change_id=trace.change_id,
                    batch_id=trace.authoritative_batch_id,
                    declared=journey,
                    value=journey.value,
                    source_digest=source_digest,
                )
                if journey is not None
                else None
            ),
        )
    )
    return MetricsDocument.of(
        risk=risk,
        change_id=trace.change_id,
        cadence="pr",
        computed_at=computed_at,
        metrics=preliminary.metrics,
        policy_digest=policy_digest,
        collection_gaps=preliminary.collection_gaps,
        shortboards=preliminary.shortboards,
        floor_ratio=preliminary.floor_ratio,
    )


def _write_document(write_root: Path, path: str, value: BaseModel | JSONValue) -> EvidenceArtifactRefV1:
    payload: JSONValue = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    data = canonical_json_bytes(payload)
    destination = write_root.joinpath(*PurePosixPath(path).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return EvidenceArtifactRefV1(path=path, digest=hashlib.sha256(data).hexdigest())


_JOURNAL_NAMES = ("action_started", "action_terminal", "process_terminal", "cleanup_terminal")


def _host_authority(
    *,
    secret_port: SecretPort | None,
    authority_handle: str | None,
    execution_id: str,
) -> tuple[ManagedSutAuthorityV1, bytes]:
    if secret_port is None or authority_handle is None:
        raise AssessmentInputError("verified assessment requires independent host authority")
    try:
        raw = secret_port.resolve(authority_handle)
        document = json.loads(raw)
        if isinstance(document, dict) and document.get("kind") == "user-invocation-host.v1":
            if _EXECUTION_ID.fullmatch(execution_id) is None:
                raise AssessmentInputError("independent host authority is unavailable or invalid")
            retained = Path(document["authority_root"]) / f"{execution_id}.json"
            document = json.loads(retained.read_bytes())
            if isinstance(document, dict) and "authority" in document:
                document = document["authority"]
        authority = ManagedSutAuthorityV1.model_validate(document)
    except (KeyError, OSError, ValidationError, ValueError, TypeError, json.JSONDecodeError):
        raise AssessmentInputError("independent host authority is unavailable or invalid") from None
    token_path = Path(authority.ownership_token.path)
    if token_path != Path(authority.run_root) / ".ownership-token":
        raise AssessmentInputError("independent host authority token path differs from its run root")
    try:
        details = token_path.stat()
        token = token_path.read_bytes()
    except OSError:
        raise AssessmentInputError("independent host authority ownership token is unavailable") from None
    if (
        token_path.is_symlink()
        or not token_path.is_file()
        or details.st_nlink != 1
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
        or details.st_dev != authority.ownership_token.device
        or details.st_ino != authority.ownership_token.inode
        or len(token) != 32
        or f"sha256:{hashlib.sha256(token).hexdigest()}" != authority.ownership_token.digest
    ):
        raise AssessmentInputError("independent host authority ownership token is invalid")
    return authority, token


def _journal_payload(
    root: Path,
    ref: EvidenceArtifactRefV1,
    *,
    record: str,
    manifest_digest: str,
    ownership_token: bytes,
) -> object:
    try:
        document = json.loads(_read_ref(root, ref))
    except json.JSONDecodeError:
        raise AssessmentInputError(f"authenticated journal record is not JSON: {record}") from None
    if not isinstance(document, dict) or set(document) != {
        "manifest_digest",
        "record",
        "payload",
        "seal",
    }:
        raise AssessmentInputError(f"authenticated journal record has an invalid envelope: {record}")
    seal = document.pop("seal")
    expected = hmac.new(
        ownership_token,
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()
    if (
        type(seal) is not str
        or not hmac.compare_digest(seal, expected)
        or document["manifest_digest"] != manifest_digest
        or document["record"] != record
    ):
        raise AssessmentInputError(f"authenticated journal seal does not match: {record}")
    return document["payload"]


def _exact_keys(value: object, keys: set[str], *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AssessmentInputError(f"authenticated journal payload has an invalid shape: {label}")
    return value


def _sqlite_observation(value: object, *, label: str, allow_skipped: bool) -> dict[str, object]:
    if not isinstance(value, dict) or type(value.get("state")) is not str:
        raise AssessmentInputError(f"authenticated journal payload has an invalid shape: {label}")
    state = value["state"]
    if state == "skipped" and allow_skipped:
        row = _exact_keys(value, {"state", "reason", "rows"}, label=label)
        if type(row["reason"]) is not str or row["rows"] != []:
            raise AssessmentInputError(f"authenticated journal payload has invalid values: {label}")
        return row
    row = _exact_keys(
        value,
        {"state", "rows", "reason", "database_identity", "database_metadata"},
        label=label,
    )
    if state in {"error", "timeout"}:
        if (
            type(row["reason"]) is not str
            or row["rows"] != []
            or row["database_identity"] is not None
            or row["database_metadata"] is not None
        ):
            raise AssessmentInputError(f"authenticated journal payload has invalid values: {label}")
        return row
    if state != "observed" or row["reason"] is not None or not isinstance(row["rows"], list):
        raise AssessmentInputError(f"authenticated journal payload has invalid values: {label}")
    identity = _exact_keys(
        row["database_identity"], {"path", "device", "inode"}, label=f"{label}.database_identity"
    )
    metadata = _exact_keys(row["database_metadata"], {"size", "mtime_ns"}, label=f"{label}.database_metadata")
    if (
        type(identity["path"]) is not str
        or type(identity["device"]) is not int
        or type(identity["inode"]) is not int
        or type(metadata["size"]) is not int
        or type(metadata["mtime_ns"]) is not int
        or min(identity["device"], identity["inode"], metadata["size"], metadata["mtime_ns"]) < 0  # type: ignore[type-var]
    ):
        raise AssessmentInputError(f"authenticated journal payload has invalid values: {label}")
    for index, item in enumerate(row["rows"]):  # type: ignore[union-attr]
        user = _exact_keys(
            item,
            {"username", "email", "is_active", "is_superuser", "dept_id"},
            label=f"{label}.rows[{index}]",
        )
        if (
            type(user["username"]) is not str
            or type(user["email"]) is not str
            or type(user["is_active"]) is not bool
            or type(user["is_superuser"]) is not bool
            or user["dept_id"] is not None
        ):
            raise AssessmentInputError(f"authenticated journal payload has invalid values: {label}")
    return row


def _http_observation(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or type(value.get("state")) is not str:
        raise AssessmentInputError("authenticated journal payload has an invalid shape: action.http")
    state = value["state"]
    if state == "observed":
        row = _exact_keys(value, {"state", "status", "code"}, label="action.http")
        if type(row["status"]) is not int:
            raise AssessmentInputError("authenticated journal payload has invalid values: action.http")
        canonical_json_bytes(cast(JSONValue, row["code"]))
        return row
    keys = {"state", "status", "reason"} if state == "error" else {"state", "reason"}
    row = _exact_keys(value, keys, label="action.http")
    if state not in {"error", "timeout", "skipped"} or type(row["reason"]) is not str:
        raise AssessmentInputError("authenticated journal payload has invalid values: action.http")
    if state == "error" and type(row["status"]) is not int:
        raise AssessmentInputError("authenticated journal payload has invalid values: action.http")
    return row


def _action_terminal(value: object) -> dict[str, dict[str, object]]:
    payload = _exact_keys(value, {"initial", "http", "oracle"}, label="action_terminal")
    return {
        "initial": _sqlite_observation(payload["initial"], label="action.initial", allow_skipped=False),
        "http": _http_observation(payload["http"]),
        "oracle": _sqlite_observation(payload["oracle"], label="action.oracle", allow_skipped=True),
    }


def _process_terminal(value: object) -> VerifiedProcessReceiptV1:
    row = _exact_keys(
        value,
        {
            "schema_version",
            "command",
            "limits",
            "exit_code",
            "report",
            "reason",
            "request_count",
            "stderr",
            "cleanup_confirmed",
        },
        label="process_terminal",
    )
    limits = _exact_keys(
        row["limits"], {"timeout_seconds", "max_frame_bytes", "max_stderr_bytes"}, label="process.limits"
    )
    if (
        row["schema_version"] != "1"
        or not isinstance(row["command"], list)
        or any(type(item) is not str for item in row["command"])
        or (row["exit_code"] is not None and type(row["exit_code"]) is not int)
        or (row["report"] is not None and not isinstance(row["report"], dict))
        or (row["reason"] is not None and type(row["reason"]) is not str)
        or type(row["request_count"]) is not int
        or type(row["stderr"]) is not str
        or type(row["cleanup_confirmed"]) is not bool
        or type(limits["timeout_seconds"]) not in {int, float}
        or type(limits["max_frame_bytes"]) is not int
        or type(limits["max_stderr_bytes"]) is not int
    ):
        raise AssessmentInputError("authenticated journal payload has invalid values: process_terminal")
    try:
        return VerifiedProcessReceiptV1.model_validate(row)
    except ValidationError:
        raise AssessmentInputError(
            "authenticated journal payload has invalid values: process_terminal"
        ) from None


def _journal_observations(
    *,
    plan: CaseExecutionPlanV1,
    execution_id: str,
    action: dict[str, dict[str, object]] | None,
    action_ref: EvidenceArtifactRefV1 | None,
) -> tuple[ObservationV1, ...]:
    values: dict[str, object] = {}
    if action is not None:
        initial, http, oracle = action["initial"], action["http"], action["oracle"]
        if initial["state"] == "observed":
            values["initial.user_absent"] = len(cast(list[object], initial["rows"]))
        if http["state"] == "observed":
            values.update({"action.finished": True, "api.http_status": http["status"]})
            if http["code"] is not None:
                values["api.code"] = http["code"]
        if oracle["state"] == "observed":
            rows = cast(list[dict[str, object]], oracle["rows"])
            values["oracle.executed"] = True
            # Unknown HTTP termination leaves the rowset diagnostic only.
            if http["state"] == "observed":
                values["user.row_count"] = len(rows)
                if len(rows) == 1:
                    values.update({f"user.{key}": value for key, value in rows[0].items()})
    return tuple(
        ObservationV1(
            execution_id=execution_id,
            obligation_id=obligation,
            state="observed" if obligation in values else "missing",
            actual=values.get(obligation),
            evidence_ref=action_ref,
            reason=(
                None
                if obligation in values
                else "action_terminal_unknown"
                if action is None
                else "http_terminal_unknown"
                if action["http"].get("reason") == "http_terminal_unknown"
                and (obligation == "action.finished" or obligation.startswith("user."))
                else "runtime_fact_unavailable"
            ),
        )
        for obligation in plan.required
    )


def _expected_collector_completion(
    plan: CaseExecutionPlanV1, payloads: dict[str, object]
) -> EvidenceCompletionV1:
    if plan.validation_profile == "api_db.v1":
        if TELEMETRY_OTLP_NAME in payloads or TELEMETRY_COMPLETION_NAME in payloads:
            raise AssessmentInputError("api_db.v1 execution must not carry sealed telemetry")
        return EvidenceCompletionV1(state="not_required")
    raw = payloads.get(TELEMETRY_COMPLETION_NAME)
    if not isinstance(raw, (bytes, bytearray)):
        raise AssessmentInputError("verified trace raw closure is missing sealed telemetry")
    try:
        completion = TelemetryCompletionV1.model_validate_json(raw)
    except ValidationError as error:
        raise AssessmentInputError("verified telemetry completion is invalid") from error
    if completion.state == "complete":
        return EvidenceCompletionV1(state="complete")
    if completion.collector_drain.state == "timeout":
        return EvidenceCompletionV1(state="timeout", reason=completion.collector_drain.reason)
    return EvidenceCompletionV1(
        state="error",
        reason=completion.collector_drain.reason or completion.archive.reason or "collector_incomplete",
    )


def _replay_trace_observations(
    *,
    plan: CaseExecutionPlanV1,
    manifest: VerificationManifestV1,
    cycle: VerifiedExecutionCycleResultV1,
    payloads: dict[str, object],
    replayed: tuple[ObservationV1, ...],
) -> tuple[ObservationV1, ...]:
    names = {Path(ref.path).name for ref in cycle.raw_evidence_refs}
    if plan.validation_profile != "api_db_trace.v1":
        if TELEMETRY_OTLP_NAME in names or TELEMETRY_COMPLETION_NAME in names:
            raise AssessmentInputError("api_db.v1 execution must not carry sealed telemetry")
        return replayed
    if TELEMETRY_COMPLETION_NAME not in names:
        raise AssessmentInputError("verified trace raw closure is missing sealed telemetry")
    raw_completion = payloads.get(TELEMETRY_COMPLETION_NAME)
    if not isinstance(raw_completion, (bytes, bytearray)):
        raise AssessmentInputError("verified trace raw closure is missing sealed telemetry")
    try:
        completion = TelemetryCompletionV1.model_validate_json(raw_completion)
    except ValidationError as error:
        raise AssessmentInputError("verified telemetry completion is invalid") from error
    if completion.execution_id != cycle.execution_id:
        raise AssessmentInputError("verified telemetry belongs to a different execution")
    if completion.sut_instance_id != manifest.sut.instance_id:
        raise AssessmentInputError("verified telemetry belongs to a different instance")
    otlp = payloads.get(TELEMETRY_OTLP_NAME)
    if TELEMETRY_OTLP_NAME not in names or not isinstance(otlp, (bytes, bytearray)):
        if completion.state == "complete":
            raise AssessmentInputError("verified trace raw closure is missing sealed telemetry")
        otlp = b""
    try:
        spans = parse_otlp_records(bytes(otlp))
        trace = check_trace_requirements(plan, manifest, spans, completion)
    except ValueError as error:
        if "conflict" in str(error).lower():
            raise AssessmentInputError("verified telemetry contains conflicting spans") from error
        merged = {item.obligation_id: item for item in replayed}
        for item in truncated_trace_observations(plan, cycle.execution_id):
            merged[item.obligation_id] = item
        return tuple(merged[key] for key in plan.required)
    merged = {item.obligation_id: item for item in replayed}
    for item in trace:
        merged[item.obligation_id] = item
    return tuple(merged[key] for key in plan.required)


def _verified_materials(
    root: Path,
    request: MaterializeAssessmentInputV1,
    cycle: VerifiedExecutionCycleResultV1,
    *,
    secret_port: SecretPort | None,
    authority_handle: str | None,
) -> tuple[ClosedMappingV1, ExecutionEvidenceV1, VerificationVerdictV1]:
    """Authenticate the complete verified closure before deriving assessment facts."""

    try:
        preliminary_mapping = ClosedMappingV1.model_validate(_load_json(root, cycle.mapping_ref))
        capability_leafs = tuple(sorted({item.capability for item in preliminary_mapping.mappings}))
        frozen_plan = decode_plan(_read_ref(root, request.plan_ref), request.plan_ref)
        admission = admit_verified_generation(
            root,
            root,
            change_id=cycle.change_id,
            coverage_epoch=cycle.coverage_epoch,
            plan_digest=cycle.plan_digest,
            plan_ref=cycle.plan_ref,
            reviewed_case=cycle.reviewed_case,
            validation_profile=cycle.validation_profile,
            selected_test_families=frozen_plan.selected_test_families,
            capability_leafs=capability_leafs,
            case_execution_plan_ref=cycle.case_execution_plan_ref,
        )
        if (
            admission.closed_mapping != preliminary_mapping
            or admission.reviewed_case != request.reviewed_case
            or admission.source_refs != request.generation.source_refs
            or admission.plan_refs != request.generation.plan_refs
            or request.generation.case_execution_plan_ref != cycle.case_execution_plan_ref
            or request.generation.mapping_ref != cycle.mapping_ref
            or cycle.source_refs != request.generation.source_refs
        ):
            raise AssessmentInputError("verified generation closure differs from the committed cycle")
        authority, ownership_token = _host_authority(
            secret_port=secret_port,
            authority_handle=authority_handle,
            execution_id=cycle.execution_id,
        )
        verified = VerifiedExecutionResultV1.model_validate_json(_read_ref(root, cycle.execution_index_ref))
        manifest = VerificationManifestV1.model_validate_json(_read_ref(root, cycle.manifest_ref))
        manifest_digest = hashlib.sha256(
            canonical_json_bytes(cast(JSONValue, manifest.model_dump(mode="json")))
        ).hexdigest()
        if (
            authority.authorization_scope_digest != manifest.authorization_scope_digest
            or authority.activity_receipt_digest != manifest.activity_receipt_digest
        ):
            raise AssessmentInputError("independent host authority does not match execution manifest")
        expected_manifest_path = f"{manifest.evidence_root}/manifest.json"
        if cycle.manifest_ref.path != expected_manifest_path:
            raise AssessmentInputError("verified manifest path differs from its evidence root")
        journal_root = root.joinpath(*PurePosixPath(manifest.evidence_root).parts)
        try:
            journal_root.resolve(strict=True).relative_to(root.resolve())
        except (OSError, ValueError):
            raise AssessmentInputError("verified journal root is unavailable") from None
        actual_refs: list[EvidenceArtifactRefV1] = []
        payloads: dict[str, object] = {}
        for name in _JOURNAL_NAMES:
            path = journal_root / f"{name}.json"
            if not path.exists() and not path.is_symlink():
                continue
            relative = f"{manifest.evidence_root}/{name}.json"
            data = path.read_bytes()
            ref = EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())
            actual_refs.append(ref)
            payloads[name] = _journal_payload(
                root,
                ref,
                record=name,
                manifest_digest=manifest_digest,
                ownership_token=ownership_token,
            )
        for filename in (TELEMETRY_COMPLETION_NAME, TELEMETRY_OTLP_NAME):
            path = journal_root / filename
            if not path.exists() and not path.is_symlink():
                continue
            relative = f"{manifest.evidence_root}/{filename}"
            data = path.read_bytes()
            ref = EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())
            actual_refs.append(ref)
            payloads[filename] = data
        actual_raw_refs = tuple(sorted(actual_refs, key=lambda item: (item.path, item.digest)))
        if actual_raw_refs != cycle.raw_evidence_refs:
            raise AssessmentInputError("verified journal raw closure differs from the committed cycle")
        allowed_json = {
            "execution_terminal.json",
            "manifest.json",
            "outcome.json",
            TELEMETRY_COMPLETION_NAME,
            *(f"{name}.json" for name in _JOURNAL_NAMES),
        }
        if any(path.name not in allowed_json for path in journal_root.glob("*.json")):
            raise AssessmentInputError("verified journal contains an uncommitted JSON record")
        if any(path.name != TELEMETRY_OTLP_NAME for path in journal_root.glob("*.jsonl")):
            raise AssessmentInputError("verified journal contains an uncommitted JSON record")
        outcome = TaskOutcome.model_validate(
            _journal_payload(
                root,
                cycle.evidence_ref,
                record="outcome",
                manifest_digest=manifest_digest,
                ownership_token=ownership_token,
            )
        )
        evidence = VerificationEvidenceV1.model_validate(outcome.output)
        if cycle.execution_authority_ref.path != f"{manifest.evidence_root}/execution_terminal.json":
            raise AssessmentInputError("authenticated host execution result path differs")
        terminal = VerifiedExecutionAuthorityV1.model_validate(
            _journal_payload(
                root,
                cycle.execution_authority_ref,
                record="execution_terminal",
                manifest_digest=manifest_digest,
                ownership_token=ownership_token,
            )
        )
        indexed_projection = VerifiedExecutionAuthorityV1.model_validate(
            verified.model_dump(mode="json", exclude={"execution_authority_ref"})
        )
        if terminal != indexed_projection:
            raise AssessmentInputError("execution index differs from authenticated host execution result")
        try:
            machine_plan: CaseExecutionPlanV1 = next(
                item for item in admission.machine_plans.cases if item.case_id == cycle.case_id
            )
        except StopIteration:
            raise AssessmentInputError(
                "verified execution case is absent from the authenticated machine plans"
            ) from None
        process = _process_terminal(payloads.get("process_terminal"))
        action_started = payloads.get("action_started")
        if action_started is not None:
            started = _exact_keys(action_started, {"execution_id", "state"}, label="action_started")
            if started != {"execution_id": cycle.execution_id, "state": "started"}:
                raise AssessmentInputError("authenticated action start identity differs")
        if process.request_count != int(action_started is not None):
            raise AssessmentInputError("authenticated process request count differs from action history")
        cleanup = payloads.get("cleanup_terminal")
        if cleanup is not None:
            expected_cleanup = {
                "container_name": f"aa-verify-{cycle.execution_id}",
                "confirmed": True,
            }
            if (
                _exact_keys(cleanup, {"container_name", "confirmed"}, label="cleanup_terminal")
                != expected_cleanup
            ):
                raise AssessmentInputError("authenticated cleanup identity differs")
        action_payload = payloads.get("action_terminal")
        action = _action_terminal(action_payload) if action_payload is not None else None
        if action is not None:
            expected_sqlite_identity = manifest.sqlite.model_dump(mode="json")
            for name in ("initial", "oracle"):
                observation = action[name]
                if (
                    observation["state"] == "observed"
                    and observation["database_identity"] != expected_sqlite_identity
                ):
                    raise AssessmentInputError(
                        "authenticated database observation differs from the execution manifest"
                    )
        action_ref = next(
            (ref for ref in actual_raw_refs if ref.path.endswith("/action_terminal.json")), None
        )
        replayed = _journal_observations(
            plan=machine_plan,
            execution_id=cycle.execution_id,
            action=action,
            action_ref=action_ref,
        )
        replayed = _replay_trace_observations(
            plan=machine_plan,
            manifest=manifest,
            cycle=cycle,
            payloads=payloads,
            replayed=replayed,
        )
        if evidence.observations != replayed:
            raise AssessmentInputError("verified observations differ from authenticated action history")
        process_ref = next(
            (ref for ref in actual_raw_refs if ref.path.endswith("/process_terminal.json")), None
        )
        if process_ref is None or evidence.receipt_ref != process_ref:
            raise AssessmentInputError("verified receipt differs from authenticated process history")
        host_reason = process.reason
        if host_reason == "container_cleanup_unconfirmed" and cleanup is not None:
            host_reason = None
        if process.exit_code != 0:
            host_reason = host_reason or "runner_exit_nonzero"
        if any(item.state != "observed" for item in replayed):
            host_reason = host_reason or "required_facts_missing"
        expected_host = (
            EvidenceCompletionV1(state="error", reason=host_reason)
            if host_reason
            else EvidenceCompletionV1(state="complete")
        )
        expected_collector = _expected_collector_completion(machine_plan, payloads)
        collector_incomplete = expected_collector.state not in {"complete", "not_required"}
        if (
            evidence.host_completion != expected_host
            or evidence.collector_completion != expected_collector
            or evidence.state != ("incomplete" if host_reason or collector_incomplete else "collected")
        ):
            raise AssessmentInputError("verified host completion differs from authenticated history")
        expected_cycle = {
            "validation_profile": verified.validation_profile,
            "change_id": verified.change_id,
            "case_id": verified.case_id,
            "reviewed_case": verified.reviewed_case,
            "coverage_epoch": verified.coverage_epoch,
            "repair_round": verified.repair_round,
            "plan_digest": verified.plan_digest,
            "plan_ref": verified.plan_ref,
            "case_execution_plan_ref": verified.case_execution_plan_ref,
            "case_execution_plan_digest": verified.case_execution_plan_digest,
            "spec_digest": verified.spec_digest,
            "execution_id": verified.execution_id,
            "attempt_key": verified.attempt_key,
            "batch_id": verified.batch_id,
            "executed_at": verified.executed_at,
            "completion_status": verified.completion_status,
            "mapping_digest": verified.mapping_digest,
            "manifest_ref": verified.manifest_ref,
            "evidence_ref": verified.evidence_ref,
            "execution_authority_ref": verified.execution_authority_ref,
            "raw_evidence_refs": verified.raw_evidence_refs,
        }
        if any(getattr(cycle, key) != value for key, value in expected_cycle.items()):
            raise AssessmentInputError("verified execution index differs from its committed cycle")
        observation_refs = tuple(
            item.evidence_ref for item in evidence.observations if item.evidence_ref is not None
        )
        if (
            outcome.status != "succeeded"
            or verified.evidence != evidence
            or evidence.manifest_digest != manifest_digest
            or evidence.receipt_ref not in cycle.raw_evidence_refs
            or any(ref not in cycle.raw_evidence_refs for ref in observation_refs)
        ):
            raise AssessmentInputError("verified outcome differs from its authenticated evidence")
        if (
            manifest.execution_id != cycle.execution_id
            or manifest.change_id != cycle.change_id
            or manifest.case_id != cycle.case_id
            or manifest.attempt_key != cycle.attempt_key
            or manifest.coverage_epoch != cycle.coverage_epoch
            or manifest.repair_round != cycle.repair_round
            or manifest.plan_digest != cycle.plan_digest
            or manifest.plan_ref != cycle.plan_ref.path
            or manifest.case_execution_plan_ref != cycle.case_execution_plan_ref.path
            or manifest.case_execution_plan_digest != cycle.case_execution_plan_digest
            or manifest.spec_digest != cycle.spec_digest
            or manifest.mapping_digest != cycle.mapping_digest
            or manifest.validation_profile != cycle.validation_profile
            or canonical_json_bytes(cast(JSONValue, manifest.inputs.model_dump(mode="json")))
            != canonical_json_bytes(cast(JSONValue, machine_plan.inputs))
        ):
            raise AssessmentInputError("verified manifest identity differs from the committed cycle")
        evidence_prefix = f"{manifest.evidence_root}/"
        if cycle.evidence_ref.path != f"{manifest.evidence_root}/outcome.json" or any(
            not ref.path.startswith(evidence_prefix) for ref in cycle.raw_evidence_refs
        ):
            raise AssessmentInputError("verified raw evidence does not belong to this execution")
        verdict = evaluate_verification(
            machine_plan,
            evidence,
            completion_status=cycle.completion_status,
            execution_id=cycle.execution_id,
        )
    except (GenerationAdmissionError, ValidationError, ValueError) as error:
        if isinstance(error, AssessmentInputError):
            raise
        raise AssessmentInputError(f"invalid verified execution evidence: {error}") from error

    # Existing coverage projection consumes test execution facts. This adapter
    # is derived from the business verdict, never from pytest's self-report or
    # from the cycle's collection status.
    passed = verdict.verdict == "PASSED"
    results = tuple(
        RawTestResultV1(
            test=item.test,
            case_id=item.case_id,
            status="passed" if passed else "failed",
            duration_ms=0,
            message="" if passed else f"verification_{verdict.verdict.lower()}",
        )
        for item in admission.closed_mapping.mappings
    )
    selected = SelectedTargets(
        api=any(item.layer == "api" for item in admission.closed_mapping.mappings),
        e2e=any(item.layer == "e2e" for item in admission.closed_mapping.mappings),
        fuzz=any(item.layer == "fuzz" for item in admission.closed_mapping.mappings),
        performance=any(item.layer == "performance" for item in admission.closed_mapping.mappings),
    )
    commands = tuple(
        ExecutionCommandReceiptV1(
            family=family,
            command=("pytest",),
            exit_code=0 if passed else 1,
            collected=sum(item.layer == family for item in admission.closed_mapping.mappings),
            passed=sum(item.layer == family for item in admission.closed_mapping.mappings) if passed else 0,
            failed=sum(item.layer == family for item in admission.closed_mapping.mappings)
            if not passed
            else 0,
            skipped=0,
        )
        for family in _FAMILY_ORDER
        if getattr(selected, family)
    )
    projected = ExecutionEvidenceV1(
        status="passed" if passed else "failed",
        change_id=cycle.change_id,
        batch_id=cycle.batch_id,
        selected_targets=selected,
        mapping=admission.closed_mapping,
        baseline_tree_id=cycle.manifest_ref.digest,
        runner_profile_digest=cycle.execution_authority_ref.digest,
        receipt=ExecutionReceiptV1(commands=commands),
        results=results,
        plan_digest=cycle.plan_digest,
        plan_ref=cycle.plan_ref,
        executed_at=cycle.executed_at,
        mapping_digest=cycle.mapping_digest,
        receipt_digest=cycle.execution_authority_ref.digest,
    )
    return admission.closed_mapping, projected, verdict


def _generation_defect_materials(
    root: Path,
    request: MaterializeAssessmentInputV1,
    cycle: VerifiedIncompleteExecutionV1,
) -> tuple[ClosedMappingV1, VerificationVerdictV1]:
    """Re-diagnose the exact generation-owned defect before quality classifies it."""

    defect = cycle.defect
    mapping = ClosedMappingV1.model_validate(_load_json(root, defect.generation.mapping_ref))
    capability_leafs = tuple(sorted({item.capability for item in mapping.mappings}))
    frozen_plan = decode_plan(_read_ref(root, request.plan_ref), request.plan_ref)
    diagnosed = diagnose_verified_bridge_defect(
        root,
        generation=defect.generation,
        validation_profile=defect.validation_profile,
        selected_test_families=frozen_plan.selected_test_families,
        capability_leafs=capability_leafs,
        attempt_key=defect.attempt_key,
    )
    if diagnosed != defect or defect.generation != request.generation:
        raise AssessmentInputError("bridge defect differs from deterministic generation diagnosis")
    machine_ref = defect.generation.case_execution_plan_ref
    if machine_ref is None:
        raise AssessmentInputError("bridge defect generation has no machine plan")
    plans = CaseExecutionPlanSetV1.model_validate(_load_json(root, machine_ref))
    try:
        plan = next(item for item in plans.cases if item.case_id == defect.case_id)
    except StopIteration:
        raise AssessmentInputError("bridge defect case is absent from the machine plan") from None
    obligations = tuple(
        VerificationObligationV1(
            obligation_id=obligation_id,
            kind="business"
            if obligation_id in {item.assertion_id for item in plan.assertions}
            else "completion",
            evidence_status="missing",
            business_status="not_evaluated",
            reason="generated bridge is not dispatchable",
        )
        for obligation_id in plan.required
    )
    verdict = VerificationVerdictV1(
        validation_profile=defect.validation_profile,
        execution_id=None,
        case_id=defect.case_id,
        verdict="INCOMPLETE",
        required=len(obligations),
        executed=0,
        evaluated=0,
        satisfied=0,
        obligations=obligations,
        reason_codes=("verification.required_evidence_missing",),
        repairable_bridge_defect=True,
    )
    return mapping, verdict


def _issue_observations(
    *,
    evidence: ExecutionEvidenceV1,
    execution_ref: EvidenceArtifactRefV1,
    metrics: MetricsDocument,
    metrics_ref: EvidenceArtifactRefV1,
) -> tuple[Observation, ...]:
    mapping_by_test = {entry.test: entry for entry in evidence.mapping.mappings}
    observed_at = evidence.executed_at.isoformat() if evidence.executed_at is not None else "unknown"
    observations: dict[str, Observation] = {}
    for index, result in enumerate(evidence.results):
        if result.status != "failed":
            continue
        mapped = mapping_by_test[result.test]
        signature = result.message or f"{mapped.layer} test failure {result.test}"
        identity = ObservationIdentityInput(
            change_id=evidence.change_id,
            batch_id=evidence.batch_id,
            kind="test_failure",
            target=mapped.layer,
            case_id=result.case_id,
            source_artifact=execution_ref.path,
            source_json_pointer=f"/results/{index}",
            signature=signature,
        )
        item = Observation(
            observation_id=observation_id(identity),
            change_id=evidence.change_id,
            batch_id=evidence.batch_id,
            kind="test_failure",
            target=cast(Any, mapped.layer),
            case_id=result.case_id,
            source=ObservationSource(
                artifact=execution_ref.path,
                json_pointer=f"/results/{index}",
            ),
            evidence_refs=[execution_ref.path],
            signature=signature,
            observed_at=observed_at,
        )
        observations[item.observation_id] = item

    adversarial = metrics.metrics["adversarial_clean"]
    if adversarial.status == "evaluated" and adversarial.holds is False:
        target = "fuzz" if evidence.selected_targets.fuzz else "coverage"
        signature = "adversarial open counterexample"
        identity = ObservationIdentityInput(
            change_id=evidence.change_id,
            batch_id=evidence.batch_id,
            kind="anomaly",
            target=target,
            case_id=None,
            source_artifact=metrics_ref.path,
            source_json_pointer="/metrics/adversarial_clean",
            signature=signature,
        )
        item = Observation(
            observation_id=observation_id(identity),
            change_id=evidence.change_id,
            batch_id=evidence.batch_id,
            kind="anomaly",
            target=target,
            source=ObservationSource(
                artifact=metrics_ref.path,
                json_pointer="/metrics/adversarial_clean",
            ),
            evidence_refs=[metrics_ref.path],
            signature=signature,
            observed_at=observed_at,
        )
        observations[item.observation_id] = item
    return tuple(observations[key] for key in sorted(observations))


def _verification_issue_observations(
    *,
    verification: VerificationVerdictV1,
    verification_ref: EvidenceArtifactRefV1,
    change_id: str,
    batch_id: str,
    observed_at: str,
) -> tuple[Observation, ...]:
    if verification.verdict == "PASSED":
        return ()
    signature = f"verification_{verification.verdict.lower()}"
    identity = ObservationIdentityInput(
        change_id=change_id,
        batch_id=batch_id,
        kind="anomaly",
        target="api",
        case_id=verification.case_id,
        source_artifact=verification_ref.path,
        source_json_pointer="/verdict",
        signature=signature,
    )
    item = Observation(
        observation_id=observation_id(identity),
        change_id=change_id,
        batch_id=batch_id,
        kind="anomaly",
        target="api",
        case_id=verification.case_id,
        source=ObservationSource(
            artifact=verification_ref.path,
            json_pointer="/verdict",
        ),
        evidence_refs=[verification_ref.path],
        signature=signature,
        observed_at=observed_at,
    )
    return (item,)


def _issue_evidence_manifest(
    *,
    change_id: str,
    batch_id: str,
    refs: tuple[EvidenceArtifactRefV1, ...],
) -> IssueEvidenceManifest:
    refs_by_path = {ref.path: ref for ref in refs}
    if any(ref.digest != refs_by_path[ref.path].digest for ref in refs):
        raise AssessmentInputError("conflicting issue evidence digests")
    entries = [
        IssueEvidenceManifestEntry(path=path, digest=f"sha256:{refs_by_path[path].digest}")
        for path in sorted(refs_by_path)
    ]
    projection = [entry.model_dump(mode="json") for entry in entries]
    return IssueEvidenceManifest(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        digest=f"sha256:{canonical_digest(cast(JSONValue, projection))}",
        entries=entries,
    )


def materialize_assessment_inputs(
    request: MaterializeAssessmentInputV1,
    *,
    project_root: Path,
    write_root: Path,
    secret_port: SecretPort | None = None,
    authority_handle: str | None = None,
) -> AssessmentInputsV1:
    if _BATCH_TOKEN.fullmatch(request.execution.batch_id) is None:
        raise AssessmentInputError("execution batch_id must be a canonical path token")
    try:
        plan = decode_plan(_read_ref(project_root, request.plan_ref), request.plan_ref)
    except (ValidationError, ValueError) as error:
        raise AssessmentInputError(f"invalid frozen assurance plan: {error}") from error
    if plan.plan_digest != request.plan_digest:
        raise AssessmentInputError("assessment plan digest does not match frozen plan")
    # The exact missing bridge is authenticated by re-diagnosis below, not by
    # pretending its frozen source reference still resolves to evidence bytes.
    missing_bridge_ref = (
        request.execution.defect.bridge_ref
        if isinstance(request.execution, VerifiedIncompleteExecutionV1)
        and request.execution.defect.defect_kind == "missing_bridge"
        else None
    )
    source_refs = tuple(ref for ref in request.generation.source_refs if ref != missing_bridge_ref)
    for ref in (
        *request.reviewed_case.preparation_refs,
        request.reviewed_case.review_ref,
        *source_refs,
        *request.generation.plan_refs,
    ):
        _read_ref(project_root, ref)
    if request.healing_ref is not None:
        healing_prefix = f"qa/changes/{request.reviewed_case.change_id}/healing/"
        if not request.healing_ref.path.startswith(healing_prefix):
            raise AssessmentInputError("healing evidence must belong to the current change")
        _read_ref(project_root, request.healing_ref)
    if request.issue_ref is not None:
        change_prefix = f"qa/changes/{request.reviewed_case.change_id}/inspect/"
        if request.issue_ref.path != "issues/snapshot.json" and not request.issue_ref.path.startswith(
            change_prefix
        ):
            raise AssessmentInputError("issue evidence must be a current-change or canonical snapshot")
        _read_ref(project_root, request.issue_ref)
    baseline, goal_leafs, journey_keys = _prepared_obligations(project_root, request, plan=plan)
    cases = _cases(project_root, request, capability_leafs=goal_leafs)
    case_ids = frozenset(case.case_id for case in cases)
    capability_leafs = frozenset(key for case in cases for key in case.trace)
    verification: VerificationVerdictV1 | None = None
    incomplete_execution: VerifiedIncompleteExecutionV1 | None = None
    if isinstance(request.execution, VerifiedIncompleteExecutionV1):
        mapping, verification = _generation_defect_materials(
            project_root,
            request,
            request.execution,
        )
        evidence = None
        incomplete_execution = request.execution
    elif isinstance(request.execution, VerifiedExecutionCycleResultV1):
        mapping, evidence, verification = _verified_materials(
            project_root,
            request,
            request.execution,
            secret_port=secret_port,
            authority_handle=authority_handle,
        )
    else:
        try:
            mapping = ClosedMappingV1.model_validate(
                _load_json(project_root, request.generation.mapping_ref),
                context={"case_ids": case_ids, "capability_leafs": capability_leafs},
            )
            evidence = ExecutionEvidenceV1.model_validate(
                _load_json(project_root, request.execution.evidence_ref),
                context={"case_ids": case_ids, "capability_leafs": capability_leafs},
            )
        except ValidationError as error:
            raise AssessmentInputError(f"invalid mapping or execution evidence: {error}") from error
        if evidence.executed_at != request.execution.executed_at:
            raise AssessmentInputError("execution evidence time differs from its committed cycle")
        if (
            evidence.change_id != request.execution.change_id
            or evidence.batch_id != request.execution.batch_id
        ):
            raise AssessmentInputError("execution evidence identity does not match the execution cycle")
        if evidence.mapping != mapping:
            raise AssessmentInputError(
                "execution evidence mapping differs from the locked generation mapping"
            )
        expected_status = "PASS" if evidence.status == "passed" else "FAIL"
        if request.execution.final_status != expected_status:
            raise AssessmentInputError("execution cycle final_status disagrees with evidence")
    policy, sufficiency_policy = _policy(project_root, request)
    if plan.policy_resource_id != request.policy_resource_id:
        raise AssessmentInputError("assessment policy resource differs from frozen plan")
    if plan.policy_digest != request.policy_sha256:
        raise AssessmentInputError("assessment policy digest differs from frozen plan")
    if plan.quality_goal.coverage_policy != policy:
        raise AssessmentInputError("current coverage policy differs from frozen plan")
    if plan.quality_goal.sufficiency_policy != sufficiency_policy:
        raise AssessmentInputError("current sufficiency policy differs from frozen plan")
    obligations = _reviewed_obligations(
        project_root,
        request,
        cases,
        baseline=baseline,
        capability_leafs=goal_leafs,
        journey_keys=journey_keys,
    )
    reviewed_goal_maps: dict[CoverageGoal, dict[str, tuple[str, ...]]] = {
        "constraint_coverage": {},
        "auth_matrix_coverage": {},
        "journey_coverage": {},
    }
    trace_keys = {key for case in cases for key in case.trace}
    for obligation in obligations:
        goal = obligation_goal(key=obligation.key, category=obligation.category)
        if goal is not None and (
            obligation.required or obligation.covered_by_cases or obligation.key in trace_keys
        ):
            reviewed_goal_maps[goal][obligation.key] = tuple(obligation.covered_by_cases)
    mrc_keys = {obligation.key for obligation in obligations}
    for case in cases:
        for key in case.trace:
            if key in mrc_keys:
                continue
            goal = obligation_goal(key=key, category="api")
            if goal is not None:
                mapped = reviewed_goal_maps[goal].get(key, ())
                reviewed_goal_maps[goal][key] = tuple(sorted({*mapped, case.case_id}))
    trace_cases = tuple(
        TraceCaseInput(
            case_id=case.case_id,
            module=case.module,
            case_type=case.type,
            automation_required=case.automation.required,
            assertions=tuple(str(item) for item in case.assertions),
            capability=next(iter(sorted(case.trace))),
        )
        for case in cases
    )
    projection = project_trace(
        TraceOperationInput(
            change_id=request.reviewed_case.change_id,
            batch_id=request.execution.batch_id,
            closed_mapping=mapping.selected,
            case_covering={
                case_id: tuple(item.test for item in mapping.mappings if item.case_id == case_id)
                for case_id in case_ids
            },
            cases=trace_cases,
            capability_leafs=tuple(sorted(capability_leafs)),
            case_ids=tuple(sorted(case_ids)),
            execution_evidence=evidence,
            executed_at=request.execution_at,
        )
    )
    sufficiency = build_sufficiency_facts(
        projection,
        policy_digest=request.policy_sha256,
        as_of=request.execution_at,
        recency_hours=sufficiency_policy.recency_hours,
    )
    minimum_coverage = join_minimum_coverage(
        MinimumCoverageInput(
            change_id=request.reviewed_case.change_id,
            items=obligations,
            executed_case_ids=tuple(
                row.case_id
                for row in projection.rows
                if row.latest_execution is not None
                and row.latest_execution.batch_id == request.execution.batch_id
                and row.latest_execution.status in {"passed", "failed"}
            ),
            failed_case_ids=tuple(
                row.case_id
                for row in projection.rows
                if row.latest_execution is not None and row.latest_execution.status == "failed"
            ),
            case_layers={case.case_id: cast(LayerName, case.type.lower()) for case in cases},
        )
    )
    # Numeric obligations retain their authenticated coverage floors. MRCs
    # without a numeric goal still require execution evidence through the
    # existing sufficiency route.
    if any(
        item.required
        and obligation_goal(key=item.key, category=item.category) is None
        and item.status != "covered"
        for item in minimum_coverage.items
    ):
        sufficiency = sufficiency.model_copy(update={"sufficient": False})
    gaps = build_coverage_gaps(
        projection,
        sufficiency,
        change_id=request.reviewed_case.change_id,
        batch_id=request.execution.batch_id,
    ).model_copy(update={"computed_at": request.execution_at, "minimum_coverage": minimum_coverage})
    metrics = _metrics(
        cases=cases,
        projection=projection,
        policy_digest=request.policy_sha256,
        computed_at=request.execution_at,
        reviewed=reviewed_goal_maps,
        required_layers={obligation.key: obligation.layer for obligation in obligations},
    )
    goals = tuple(goal for goal in _GOAL_ORDER if reviewed_goal_maps[cast(CoverageGoal, goal)])
    selected = (
        {item.layer for item in mapping.mappings}
        if evidence is None
        else {family for family in _FAMILY_ORDER if getattr(evidence.selected_targets, family)}
    )
    applicability_refs = (
        *request.reviewed_case.preparation_refs,
        plan.quality_goal.obligations_ref,
        *(
            EvidenceArtifactRefV1(path=_GOAL_RESOURCE_PATHS[resource_id], digest=digest)
            for resource_id, digest in plan.quality_goal.source_resource_digests
        ),
    )
    applicability_by_identity = {(ref.path, ref.digest): ref for ref in applicability_refs}
    scope = ActiveCoverageScopeV1.model_validate(
        {
            "change_id": request.reviewed_case.change_id,
            "coverage_epoch": request.reviewed_case.coverage_epoch,
            "required_case_ids": tuple(sorted(case.case_id for case in cases if case.automation.required)),
            "selected_families": tuple(name for name in _FAMILY_ORDER if name in selected),
            "applicable_goals": goals,
            "applicability_refs": tuple(
                sorted(
                    applicability_by_identity.values(),
                    key=lambda item: (item.path, item.digest),
                )
            ),
            "risk_tier": metrics.risk_tier,
            "policy_digest": request.policy_sha256,
        }
    )
    base = (
        f"qa/changes/{request.reviewed_case.change_id}/inspect/epochs/"
        f"{request.reviewed_case.coverage_epoch}/batches/{request.execution.batch_id}"
    )
    trace_ref = _write_document(write_root, f"{base}/trace.json", projection)
    gaps_ref = _write_document(write_root, f"{base}/coverage-gaps.json", gaps)
    metrics_ref = _write_document(write_root, f"{base}/metrics.json", metrics)
    sufficiency_ref = _write_document(write_root, f"{base}/trace-sufficiency.json", sufficiency)
    verification_ref = (
        _write_document(write_root, f"{base}/verification.json", verification)
        if verification is not None
        else None
    )
    execution_ref = (
        request.execution.execution_index_ref
        if isinstance(request.execution, VerifiedExecutionCycleResultV1)
        else None
        if isinstance(request.execution, VerifiedIncompleteExecutionV1)
        else request.execution.evidence_ref
    )
    observations: tuple[Observation, ...] = ()
    if evidence is not None and execution_ref is not None:
        observations = _issue_observations(
            evidence=evidence,
            execution_ref=execution_ref,
            metrics=metrics,
            metrics_ref=metrics_ref,
        )
    if (
        not observations
        and verification is not None
        and verification_ref is not None
        and verification.verdict != "PASSED"
    ):
        observations = _verification_issue_observations(
            verification=verification,
            verification_ref=verification_ref,
            change_id=request.reviewed_case.change_id,
            batch_id=request.execution.batch_id,
            observed_at=request.execution_at.isoformat(),
        )
    observations_ref = _write_document(
        write_root,
        f"{base}/observations.json",
        ObservationDocument(
            schema_version="1.0",
            change_id=request.reviewed_case.change_id,
            batch_id=request.execution.batch_id,
            observations=list(observations),
        ),
    )
    issue_manifest = _issue_evidence_manifest(
        change_id=request.reviewed_case.change_id,
        batch_id=request.execution.batch_id,
        refs=(
            *((execution_ref,) if execution_ref is not None else ()),
            *((verification_ref,) if verification_ref is not None else ()),
            observations_ref,
            trace_ref,
            gaps_ref,
            sufficiency_ref,
            metrics_ref,
            request.plan_ref,
            request.generation.mapping_ref,
            request.reviewed_case.review_ref,
            *request.reviewed_case.case_refs,
            *request.reviewed_case.preparation_refs,
            *source_refs,
            *request.generation.plan_refs,
            *((request.healing_ref,) if request.healing_ref else ()),
            *((request.issue_ref,) if request.issue_ref else ()),
        ),
    )
    issue_evidence_manifest_ref = _write_document(
        write_root,
        f"{base}/issue-evidence-manifest.json",
        issue_manifest,
    )
    return AssessmentInputsV1(
        change_id=request.reviewed_case.change_id,
        coverage_epoch=request.reviewed_case.coverage_epoch,
        batch_id=request.execution.batch_id,
        plan_digest=request.plan_digest,
        plan_ref=request.plan_ref,
        scope=scope,
        policy=policy,
        trace_ref=trace_ref,
        gaps_ref=gaps_ref,
        metrics_ref=metrics_ref,
        sufficiency_ref=sufficiency_ref,
        execution_ref=execution_ref,
        incomplete_execution=incomplete_execution,
        verification_ref=verification_ref,
        observations_ref=observations_ref,
        issue_evidence_manifest_ref=issue_evidence_manifest_ref,
        owned_evidence_ids=tuple(item.observation_id for item in observations),
        evidence_bundle_digest=issue_manifest.digest,
        healing_ref=request.healing_ref,
        issue_ref=request.issue_ref,
    )


class MaterializeAssessmentExecutor:
    async def execute(
        self,
        validated_input: MaterializeAssessmentInputV1,
        scope: AuthorizedAttemptScope,
    ) -> ExecutedAttemptResult[AssessmentInputsV1]:
        output = materialize_assessment_inputs(
            validated_input,
            project_root=scope.workspace.project_root,
            write_root=scope.workspace.write_root,
        )
        return ExecutedAttemptResult(output=output)


class MaterializeAssessmentHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            validated = MaterializeAssessmentInputV1.model_validate(request.input)
            authority_handle = None
            if isinstance(
                validated.execution, (VerifiedExecutionCycleResultV1, VerifiedIncompleteExecutionV1)
            ):
                profile = (
                    validated.execution.validation_profile
                    if isinstance(validated.execution, VerifiedExecutionCycleResultV1)
                    else validated.execution.defect.validation_profile
                )
                binding = _exact_keys(
                    thaw_json(request.binding_data),
                    {
                        "managed_sut_authority_handle",
                        "verification_config_digest",
                        "validation_profile",
                    },
                    label="verified assessment binding",
                )
                if (
                    type(binding["managed_sut_authority_handle"]) is not str
                    or type(binding["verification_config_digest"]) is not str
                    or binding["validation_profile"] != profile
                ):
                    raise AssessmentInputError("verified assessment binding does not match execution")
                authority_handle = cast(str, binding["managed_sut_authority_handle"])
            output = materialize_assessment_inputs(
                validated,
                project_root=context.project_root,
                write_root=context.write_root,
                secret_port=context.secrets,
                authority_handle=authority_handle,
            )
        except (AssessmentInputError, ValidationError, OSError) as error:
            return TaskOutcome.failed("invalid_input", str(error), retryable=False)
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


__all__ = [
    "AssessmentInputError",
    "MaterializeAssessmentExecutor",
    "MaterializeAssessmentHandler",
    "classify_inspection_disposition",
    "materialize_assessment_inputs",
]
