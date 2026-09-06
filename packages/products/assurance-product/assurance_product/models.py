from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path, PurePosixPath
from typing import Literal
import unicodedata
from urllib.parse import urlparse

from pydantic import AnyHttpUrl, ConfigDict, Field, field_validator, model_validator

from graph_engine.frozen_json import FrozenJSONValue
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import FrozenModel

from assurance_improvement.contracts.retro import RetroWindow
from assurance_quality.contracts.assessment import InspectionOutcomeV1, ReportOutcomeV1
from assurance_intake.contracts.workflow import require_same_plan

from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS

PRODUCT_ID = "assurance"
ENGINE_API = "2.0"
PRODUCT_WORKFLOW_MODULE_ID = "assurance.product.workflow"
FEATURE_WORKFLOW_OWNERS: tuple[str, ...] = (
    "assurance.execution",
    "assurance.generation",
    "assurance.healing",
    "assurance.improvement",
    "assurance.intake",
    "assurance.quality",
)
PUBLIC_WORKFLOW_IMPORT_ALIASES: tuple[str, ...] = (
    "execution.execute",
    "execution.rerun",
    "generation.generate",
    "healing.repair-coverage",
    "healing.repair-failure",
    "improvement.apply",
    "improvement.archive",
    "improvement.evaluate",
    "improvement.export",
    "improvement.review",
    "improvement.rollback",
    "improvement.retro",
    "intake.case",
    "intake.prepare",
    "quality.assess",
    "quality.issue-analyze",
    "quality.issue-reconcile",
    "quality.issue-review",
    "quality.report",
)

PLUGIN_ID = "assurance.product.agent"
PLUGIN_VERSION = "1.1.0"
CONFIGURATION_PLUGIN_ID = "assurance.product.configuration"
CONFIGURATION_PLUGIN_VERSION = "1.0.0"
ADAPTER_BINDING_RESOURCE_ID = "assurance.product.agent.adapter-binding"

_SHA256 = r"^[0-9a-f]{64}$"
_ROUTING_MARKERS = (",", ";", "|", "->", "fallback", "route:", "candidates")
_TEMPLATE_OR_GLOB = ("{", "}", "*", "?", "[", "]", "\\", "`", "$", "<", ">")


def _qualified_id(value: str, label: str) -> str:
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"{label} must be a qualified identifier") from error


def _exact_token(value: str, label: str) -> str:
    if value != value.strip() or any(character.isspace() for character in value):
        raise ValueError(f"{label} must be one exact selection")
    lowered = value.lower()
    if any(marker in lowered for marker in _ROUTING_MARKERS):
        raise ValueError(f"{label} must not contain routing, fallbacks, or candidate lists")
    if any(fragment in value for fragment in _TEMPLATE_OR_GLOB):
        raise ValueError(f"{label} must not contain templates or globs")
    if "sk-" in lowered or ":" in value:
        raise ValueError(f"{label} must not contain secret values")
    return value


def _secret_handle_name(value: str) -> str:
    handle = _qualified_id(value, "secret_handle")
    if handle != handle.strip() or any(character.isspace() for character in handle):
        raise ValueError("secret_handle must be a single handle token")
    if any(marker in handle for marker in (":", "/", "\\", "@")):
        raise ValueError("secret_handle must not contain credentials or paths")
    if "sk-" in handle.lower():
        raise ValueError("secret_handle must not contain secret values")
    return handle


class ExecutionLimitsV1(FrozenModel):
    max_seconds: int = Field(gt=0)


class RouteAssignmentV1(FrozenModel):
    provider_model: str = Field(min_length=1)
    worker_profile: str = Field(min_length=1)
    permission_profile_id: str
    request_policy_id: str
    limits: ExecutionLimitsV1

    @field_validator("provider_model")
    @classmethod
    def _provider_model(cls, value: str) -> str:
        return _exact_token(value, "provider_model")

    @field_validator("worker_profile")
    @classmethod
    def _worker_profile(cls, value: str) -> str:
        return _exact_token(value, "worker_profile")

    @field_validator("permission_profile_id")
    @classmethod
    def _permission_profile_id(cls, value: str) -> str:
        return _qualified_id(value, "permission_profile_id")

    @field_validator("request_policy_id")
    @classmethod
    def _request_policy_id(cls, value: str) -> str:
        return _qualified_id(value, "request_policy_id")


class PermissionProfileV1(FrozenModel):
    schema_version: Literal["1"]
    allowed_tools: tuple[str, ...]

    @field_validator("allowed_tools")
    @classmethod
    def _allowed_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_exact_token(item, "allowed tool") for item in value)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("allowed_tools must be unique")
        return cleaned


class RequestPolicyV1(FrozenModel):
    schema_version: Literal["1"]
    max_output_bytes: int = Field(gt=0)


class OpenCodeBindingV1(FrozenModel):
    schema_version: Literal["1"]
    endpoint: AnyHttpUrl
    tls_identity_digest: str = Field(pattern=_SHA256)
    secret_handle: str = Field(min_length=1)
    protocol_profile: Literal["opencode-http-v1"]
    project_scope: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0, le=300)
    observation_horizon_seconds: float = Field(gt=0, le=3600)
    progress_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    poll_interval_seconds: float = Field(gt=0, le=60)
    cancel_timeout_seconds: float = Field(gt=0, le=300)
    max_response_bytes: int = Field(gt=0, le=4_000_000)
    adapter_configuration_digest: str = Field(pattern=_SHA256)

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        parsed = urlparse(str(value))
        if parsed.username or parsed.password or value.username or value.password:
            raise ValueError("endpoint must not contain URL credentials")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("endpoint must be an absolute HTTP origin")
        if parsed.query:
            raise ValueError("endpoint must not include a query")
        if parsed.fragment:
            raise ValueError("endpoint must not include a fragment")
        path = parsed.path or "/"
        if path != "/":
            raise ValueError("endpoint must not include a path prefix")
        return value

    @field_validator("secret_handle")
    @classmethod
    def _secret_handle(cls, value: str) -> str:
        return _secret_handle_name(value)

    @field_validator("project_scope")
    @classmethod
    def _project_scope(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("project_scope must not include surrounding whitespace")
        if any(fragment in value for fragment in _TEMPLATE_OR_GLOB):
            raise ValueError("project_scope must not contain templates or globs")
        return value


class VerificationRunnerConfigV1(FrozenModel):
    qualification_digest: str = Field(pattern=_SHA256)
    source_root: str = Field(min_length=1)
    qualification_path: str = Field(min_length=1)


class VerificationHostConfigV1(FrozenModel):
    runner: VerificationRunnerConfigV1 | None = None
    managed_sut_authority_handle: str | None = None
    credential_handle: str | None = None
    collector_readiness_handle: str | None = None


class DeploymentBindingsV1(FrozenModel):
    validation_profile: Literal["api_db.v1", "api_db_trace.v1"] | None = None
    verification_host: VerificationHostConfigV1 = Field(default_factory=VerificationHostConfigV1)

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    runtime_plugin_id: Literal["runtime.opencode"]
    adapter_binding: OpenCodeBindingV1
    routes: Mapping[str, RouteAssignmentV1]
    permission_profiles: Mapping[str, PermissionProfileV1]
    request_policies: Mapping[str, RequestPolicyV1]
    secret_handles: tuple[str, ...]

    @model_validator(mode="before")
    @classmethod
    def _validate_adapter_schema(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        adapter = value.get("adapter_binding")
        if not isinstance(adapter, dict):
            return value
        copied = dict(value)
        copied["adapter_binding"] = OpenCodeBindingV1.model_validate(adapter)
        return copied

    @field_validator("permission_profiles")
    @classmethod
    def _permission_profile_keys(
        cls, value: Mapping[str, PermissionProfileV1]
    ) -> Mapping[str, PermissionProfileV1]:
        for key in value:
            _qualified_id(key, "permission profile id")
        return value

    @field_validator("request_policies")
    @classmethod
    def _request_policy_keys(cls, value: Mapping[str, RequestPolicyV1]) -> Mapping[str, RequestPolicyV1]:
        for key in value:
            _qualified_id(key, "request policy id")
        return value

    @field_validator("secret_handles")
    @classmethod
    def _secret_handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        handles = tuple(_secret_handle_name(item) for item in value)
        if len(handles) != len(set(handles)):
            raise ValueError("secret_handles must be unique")
        return tuple(sorted(handles))

    @model_validator(mode="after")
    def _validate_closed_document(self) -> DeploymentBindingsV1:
        expected = set(AGENT_EXECUTION_CONTRACTS)
        actual = set(self.routes)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            raise ValueError(
                f"routes must contain exactly the 34 semantic Agent contract IDs; missing={missing!r} extra={extra!r}"
            )
        for assignment in self.routes.values():
            if assignment.permission_profile_id not in self.permission_profiles:
                raise ValueError("permission_profile_id must resolve in permission_profiles")
            if assignment.request_policy_id not in self.request_policies:
                raise ValueError("request_policy_id must resolve in request_policies")
        if not isinstance(self.adapter_binding, OpenCodeBindingV1):
            raise ValueError("runtime.opencode requires OpenCodeBindingV1")
        expected_handles = adapter_secret_handles(self.adapter_binding)
        if tuple(self.secret_handles) != expected_handles:
            raise ValueError("secret_handles must equal the adapter binding handle union")
        return self


class ConfiguredResourceV1(FrozenModel):
    resource_id: str
    schema_id: str
    media_type: Literal["application/json", "application/yaml", "text/markdown"]
    content: FrozenJSONValue | str

    @field_validator("resource_id")
    @classmethod
    def _resource_id(cls, value: str) -> str:
        return _qualified_id(value, "resource_id")

    @field_validator("schema_id")
    @classmethod
    def _schema_id(cls, value: str) -> str:
        return _qualified_id(value, "schema_id")


class ProjectConfigV1(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    product_policy: FrozenJSONValue
    data_knowledge: FrozenJSONValue
    capability_catalog: FrozenJSONValue
    node_policy_values: Mapping[str, FrozenJSONValue] = Field(default_factory=dict)
    resources: tuple[ConfiguredResourceV1, ...] = ()

    @field_validator("node_policy_values")
    @classmethod
    def _node_policy_keys(cls, value: Mapping[str, FrozenJSONValue]) -> Mapping[str, FrozenJSONValue]:
        for key in value:
            _qualified_id(key, "node policy id")
        return value


class BuiltDeploymentWheel(FrozenModel):
    wheel: Path
    manifest_digest: str
    wheel_digest: str
    distribution: str
    import_package: str
    entry_point_value: str
    declaration_path: str
    plugin_id: Literal["assurance.product.agent"]
    plugin_version: Literal["1.1.0"]


def adapter_secret_handles(binding: OpenCodeBindingV1) -> tuple[str, ...]:
    handle = binding.secret_handle
    if handle is None:
        return ()
    return (handle,)


def all_binding_ids() -> tuple[str, ...]:
    return tuple(sorted(AGENT_EXECUTION_CONTRACTS))


TEST_FAMILY_ORDER: tuple[Literal["api", "e2e", "fuzz", "performance"], ...] = (
    "api",
    "e2e",
    "fuzz",
    "performance",
)
FAMILY_NONEMPTY_ENTRYPOINTS = frozenset({"full", "intake"})
FAMILY_EMPTY_ENTRYPOINTS = frozenset(
    {
        "case",
        "execute",
        "archive",
        "retro",
        "issue-review",
        "issue-analyze",
        "issue-reconcile",
        "improvement-review",
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    }
)
PRODUCT_ENTRYPOINTS = FAMILY_NONEMPTY_ENTRYPOINTS | FAMILY_EMPTY_ENTRYPOINTS
THIN_ENTRYPOINTS = PRODUCT_ENTRYPOINTS - {"full", "execute"}


def _canonical_token(value: str, label: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError(f"{label} must be one non-empty token")
    return normalized


def _canonical_text(value: str, label: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        raise ValueError(f"{label} must be non-empty")
    return normalized


def _canonical_artifact_prefixes(values: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(unicodedata.normalize("NFC", item.strip()) for item in values)
    if any(not item for item in cleaned):
        raise ValueError("allowed_artifact_paths must be non-empty prefixes")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("allowed_artifact_paths must be unique")
    ordered = tuple(sorted(cleaned))
    if ordered != cleaned:
        raise ValueError("allowed_artifact_paths must be sorted unique relative POSIX prefixes")
    for path in ordered:
        posix = PurePosixPath(path)
        if (
            posix.is_absolute()
            or "\\" in path
            or (len(path) >= 2 and path[1] == ":")
            or posix.as_posix() != path
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise ValueError("allowed_artifact_paths must be canonical relative POSIX prefixes")
    return ordered


def _exact_case_delta_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    if values != tuple(sorted(set(values))):
        raise ValueError("case_delta_paths must be sorted and unique")
    allowed = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
    for path in values:
        posix = PurePosixPath(path)
        if (
            not path
            or path != path.strip()
            or not path.isascii()
            or posix.is_absolute()
            or "\\" in path
            or posix.as_posix() != path
            or any(
                part in {"", ".", ".."} or any(character not in allowed for character in part)
                for part in posix.parts
            )
        ):
            raise ValueError("case_delta_paths must be exact ASCII relative POSIX paths")
    return values


class ResourceRefV1(FrozenModel):
    resource_id: str
    sha256: str = Field(pattern=_SHA256)

    @field_validator("resource_id")
    @classmethod
    def _resource_id(cls, value: str) -> str:
        return _qualified_id(value, "resource_id")


class BusinessBudgetsV1(FrozenModel):
    review_rounds: int = Field(ge=0)
    coverage_rounds: int = Field(ge=0)
    healing_rounds: int = Field(ge=0)
    execution_retries: int = Field(ge=0)


class ArtifactRefV1(FrozenModel):
    path: str = Field(min_length=1)
    digest: str = Field(pattern=_SHA256)


class ProductInputV1(FrozenModel):
    validation_profile: Literal["api_db.v1", "api_db_trace.v1"] | None = None
    verification_config_digest: str | None = Field(default=None, pattern=_SHA256)
    verification_policy: ResourceRefV1 | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    change_id: str
    requirement: str
    run_mode: Literal["case", "implement", "verify"]
    candidate_test_families: tuple[Literal["api", "e2e", "fuzz", "performance"], ...] = ()
    resolved_plan_ref: ArtifactRefV1 | None = None
    case_delta_paths: tuple[str, ...] = ()
    capability_leafs: tuple[str, ...]
    capability_catalog: ResourceRefV1
    product_policy: ResourceRefV1
    data_knowledge: ResourceRefV1
    allowed_artifact_paths: tuple[str, ...]
    budgets: BusinessBudgetsV1
    artifacts: tuple[ArtifactRefV1, ...] = ()
    retro_window: RetroWindow | None = None
    decision: str = "pass"

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_token(value, "change_id")

    @field_validator("requirement")
    @classmethod
    def _requirement(cls, value: str) -> str:
        return _canonical_text(value, "requirement")

    @field_validator("candidate_test_families")
    @classmethod
    def _candidate_test_families(
        cls, value: tuple[Literal["api", "e2e", "fuzz", "performance"], ...]
    ) -> tuple[Literal["api", "e2e", "fuzz", "performance"], ...]:
        if len(set(value)) != len(value):
            raise ValueError("candidate_test_families must be unique")
        order = {name: index for index, name in enumerate(TEST_FAMILY_ORDER)}
        if tuple(sorted(value, key=order.__getitem__)) != value:
            raise ValueError("candidate_test_families must be in canonical family order")
        return value

    @field_validator("allowed_artifact_paths")
    @classmethod
    def _allowed_artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_artifact_prefixes(value)

    @field_validator("case_delta_paths")
    @classmethod
    def _case_delta_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _exact_case_delta_paths(value)

    @model_validator(mode="after")
    def _case_delta_paths_match_change(self) -> ProductInputV1:
        prefix = ("qa", "changes", self.change_id, "cases")
        for path in self.case_delta_paths:
            parts = PurePosixPath(path).parts
            if len(parts) < 6 or parts[:4] != prefix or parts[-1] != "case.yaml":
                raise ValueError(
                    "case_delta_paths must be exact current-change cases/<module>/case.yaml paths"
                )
        return self

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item != item.strip() or "." not in item for item in value):
            raise ValueError("capability_leafs must contain canonical dotted keys")
        if value != tuple(sorted(set(value))):
            raise ValueError("capability_leafs must be sorted and unique")
        return value

    def validate_for_entrypoint(self, entrypoint: str) -> ProductInputV1:
        validate_entrypoint_families(entrypoint, self.candidate_test_families)
        creator = entrypoint in {"full", "intake"}
        consumer = entrypoint in {"case", "execute"}
        if creator and self.resolved_plan_ref is not None:
            raise ValueError(f"{entrypoint} creates a plan and cannot accept resolved_plan_ref")
        if consumer and self.resolved_plan_ref is None:
            raise ValueError(f"{entrypoint} requires resolved_plan_ref")
        if not creator and not consumer and self.resolved_plan_ref is not None:
            raise ValueError(f"{entrypoint} does not consume resolved_plan_ref")
        requires_case_delta = entrypoint in {"full", "intake", "case"}
        if requires_case_delta and not self.case_delta_paths:
            raise ValueError(f"{entrypoint} requires non-empty exact case_delta_paths")
        if not requires_case_delta and self.case_delta_paths:
            raise ValueError(f"{entrypoint} does not consume case_delta_paths")
        if entrypoint != "retro" and self.retro_window is not None:
            raise ValueError(f"{entrypoint} does not consume retro_window")
        return self

    def authenticate_against(self, composition: object) -> ProductInputV1:
        authenticate_product_input_resources(self, composition)
        return self


def validate_entrypoint_families(
    entrypoint: str,
    families: tuple[str, ...],
) -> None:
    if entrypoint not in PRODUCT_ENTRYPOINTS:
        raise ValueError(f"unknown product entrypoint: {entrypoint}")
    if entrypoint in FAMILY_NONEMPTY_ENTRYPOINTS and not families:
        raise ValueError(f"{entrypoint} requires a non-empty candidate_test_families tuple")
    if entrypoint in FAMILY_EMPTY_ENTRYPOINTS and families:
        raise ValueError(f"{entrypoint} requires an empty candidate_test_families tuple")


def authenticate_product_input_resources(value: ProductInputV1, composition: object) -> None:
    from assurance_product.verification_execution import verification_configuration

    config, digest = verification_configuration(composition)
    if value.validation_profile != config.validation_profile:
        raise ValueError("validation profile disagrees with authenticated deployment")
    if value.validation_profile is not None:
        if value.verification_config_digest != digest or value.verification_policy is None:
            raise ValueError("verification configuration digest or policy disagrees with deployment")
    elif value.verification_config_digest is not None or value.verification_policy is not None:
        raise ValueError("legacy input cannot carry verification configuration")
    resources = getattr(getattr(getattr(composition, "registries", None), "resources", None), "entries", None)
    if not isinstance(resources, Mapping):
        raise ValueError("composition resource registry is unavailable")
    refs = (
        value.capability_catalog,
        value.product_policy,
        value.data_knowledge,
        *((value.verification_policy,) if value.verification_policy is not None else ()),
    )
    seen: set[str] = set()
    for ref in refs:
        if ref.resource_id in seen:
            raise ValueError(f"duplicate product resource reference: {ref.resource_id}")
        seen.add(ref.resource_id)
        entry = resources.get(ref.resource_id)
        if entry is None:
            raise ValueError(f"resource is not registered: {ref.resource_id}")
        digest = getattr(entry, "sha256", None)
        if digest != ref.sha256:
            raise ValueError(f"resource digest drifted: {ref.resource_id}")
    if value.verification_policy is not None:
        if value.verification_policy.resource_id != "assurance.product.configuration.verification-policy":
            raise ValueError("verification policy resource identity drifted")
        policy_entry = resources[value.verification_policy.resource_id]
        import yaml

        policy = yaml.safe_load(policy_entry.content)
        if not isinstance(policy, Mapping) or policy.get("validation_profile") != value.validation_profile:
            raise ValueError("verification policy profile disagrees with authenticated deployment")
    catalog_entry = resources.get(value.capability_catalog.resource_id)
    if catalog_entry is None:
        raise ValueError("capability catalog is not registered")
    try:
        catalog = json.loads(catalog_entry.content.decode("utf-8"))
    except (AttributeError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("capability catalog is not canonical JSON") from error
    leafs = catalog.get("typed_leafs") if isinstance(catalog, Mapping) else None
    if not isinstance(leafs, list) or any(not isinstance(item, str) for item in leafs):
        raise ValueError("capability catalog is missing typed_leafs")
    if value.capability_leafs != tuple(leafs):
        raise ValueError("product capability_leafs disagree with the authenticated catalog")


class ProductReceiptRefV1(FrozenModel):
    receipt_id: str = Field(min_length=1)
    receipt_digest: str = Field(pattern=_SHA256)


class ProductPublicOutput(FrozenModel):
    change_id: str
    status: Literal["completed", "failed"]
    receipts: tuple[ProductReceiptRefV1, ...] = ()

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_token(value, "change_id")


class GraphStatusV1(FrozenModel):
    graph_instance_id: str
    graph_id: str
    parent_graph_instance_id: str | None
    state: Literal["inactive", "running", "failed", "stopped", "interrupted", "completed"]


class NodeStatusV1(FrozenModel):
    graph_instance_id: str
    node_id: str
    state: Literal[
        "inactive",
        "ready",
        "running",
        "retrying",
        "succeeded",
        "failed",
        "stopped",
        "interrupted",
        "skipped",
    ]
    attempt: int | None
    lease_state: str | None
    failure_category: str | None
    activity_reference_digest: str | None


class CoverageProgressV1(FrozenModel):
    round: int
    maximum_rounds: int
    measured: FrozenJSONValue
    decision: str


class EffectStatusV1(FrozenModel):
    effect_id: str
    kind: str
    state: str
    receipt_digest: str | None


class AdapterEvidenceRefV1(FrozenModel):
    activation_id: str
    activity_id: str
    reference_digest: str
    terminal_receipt_digest: str | None


class PendingInterruptStatusV1(FrozenModel):
    node_id: str
    actions: tuple[str, ...]
    reason_category: str


class ExecutionGateRefV1(FrozenModel):
    semantic_node_id: Literal["execution.execute", "execution.run"]
    batch_id: str = Field(min_length=1)
    execution_digest: str = Field(pattern=_SHA256)


class QualityGateRefV1(FrozenModel):
    inspection: InspectionOutcomeV1
    report: ReportOutcomeV1

    @model_validator(mode="after")
    def _same_inspection(self) -> QualityGateRefV1:
        if (
            self.inspection.change_id,
            self.inspection.coverage_epoch,
            self.inspection.batch_id,
            self.inspection.inspection_receipt,
        ) != (
            self.report.change_id,
            self.report.coverage_epoch,
            self.report.batch_id,
            self.report.inspection_receipt,
        ):
            raise ValueError("report must bind the current inspection")
        require_same_plan(
            self.inspection.plan_digest,
            self.inspection.plan_ref,
            self.report.plan_digest,
            self.report.plan_ref,
        )
        return self


class ChangeProjectionV1(FrozenModel):
    change_id: str
    state: Literal["running", "blocked", "interrupted", "stopped", "failed", "achieved"]

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_token(value, "change_id")


class ApplyProjectionV1(FrozenModel):
    manifest_digest: str | None
    file_count: int = Field(ge=0)


class PublicationProjectionV1(FrozenModel):
    status: Literal["not_ready", "ready", "published", "drifted"]


class ApplyManifestFileV1(FrozenModel):
    target_path: str
    source_path: str
    source_sha256: str
    baseline_sha256: str | None
    mode: int
    operation: str

    @field_validator("target_path", "source_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return _canonical_artifact_prefixes((value,))[0]


class ApplyManifestV1(FrozenModel):
    schema_version: Literal["1"]
    change_id: str
    digest: str
    files: tuple[ApplyManifestFileV1, ...]

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_token(value, "change_id")


class StatusV1(FrozenModel):
    schema_version: Literal["1"]
    invocation_id: str
    lock_digest: str
    root_input_digest: str
    status: Literal["running", "blocked", "interrupted", "stopped", "failed", "completed"]
    entrypoint: str
    graph_hierarchy: tuple[GraphStatusV1, ...]
    node_states: tuple[NodeStatusV1, ...]
    selected_test_families: tuple[str, ...]
    coverage_progress: CoverageProgressV1 | None
    durable_effects: tuple[EffectStatusV1, ...]
    adapter_evidence: tuple[AdapterEvidenceRefV1, ...]
    execution_gate: ExecutionGateRefV1 | None
    quality_gate: QualityGateRefV1 | None
    pending_interrupt: PendingInterruptStatusV1 | None
    terminal_reason: str | None
    change: ChangeProjectionV1
    apply: ApplyProjectionV1
    publication: PublicationProjectionV1


class PublishFileV1(FrozenModel):
    target_path: str
    source_path: str
    source_sha256: str
    baseline_sha256: str | None
    final_sha256: str
    temp_name: str
    backup_name: str

    @field_validator("target_path", "source_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return _canonical_artifact_prefixes((value,))[0]

    @field_validator("temp_name", "backup_name")
    @classmethod
    def _component(cls, value: str) -> str:
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("temp/backup name must be one path component")
        return value


class PublishReceiptV1(FrozenModel):
    schema_version: Literal["1"]
    change_id: str
    manifest_digest: str = Field(pattern=_SHA256)
    source_digest: str = Field(pattern=_SHA256)
    target_baseline: str = Field(pattern=_SHA256)
    final_digest: str = Field(pattern=_SHA256)
    files: tuple[PublishFileV1, ...]

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_token(value, "change_id")


class PublishJournalRecordV1(FrozenModel):
    phase: Literal["prepared", "replacing", "committed", "rolled_back"]
    change_id: str
    manifest_digest: str = Field(pattern=_SHA256)
    source_digest: str = Field(pattern=_SHA256)
    target_baseline: str = Field(pattern=_SHA256)
    temp_identity: str = Field(pattern=_SHA256)
    backup_identity: str = Field(pattern=_SHA256)
    final_digest: str = Field(pattern=_SHA256)
    files: tuple[PublishFileV1, ...]

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_token(value, "change_id")


class PublishJournalV1(FrozenModel):
    schema_version: Literal["1"]
    records: tuple[PublishJournalRecordV1, ...]
