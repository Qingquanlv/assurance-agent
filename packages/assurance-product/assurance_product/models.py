from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Literal
import unicodedata
from urllib.parse import urlparse

from pydantic import AnyHttpUrl, ConfigDict, Field, field_validator, model_validator

from graph_engine.frozen_json import FrozenJSONValue
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import FrozenModel

PRODUCT_ID = "assurance"
ENGINE_API = "2.0"

AdapterName = Literal["opencode", "cursor"]
PLUGIN_ID = "assurance.product.agent"
PLUGIN_VERSION = "1.0.0"
CONFIGURATION_PLUGIN_ID = "assurance.product.configuration"
CONFIGURATION_PLUGIN_VERSION = "1.0.0"

PREPARE_IDS: tuple[str, ...] = (
    "assurance.intake.case-design.prepare",
    "assurance.intake.case-review.prepare",
    "assurance.intake.explore.prepare",
    "assurance.intake.intake.prepare",
    "assurance.generation.api.codegen-fix.prepare",
    "assurance.generation.api.codegen.prepare",
    "assurance.generation.api.plan-review.prepare",
    "assurance.generation.api.plan.prepare",
    "assurance.generation.e2e.codegen-fix.prepare",
    "assurance.generation.e2e.codegen.prepare",
    "assurance.generation.e2e.plan-review.prepare",
    "assurance.generation.e2e.plan.prepare",
    "assurance.generation.fuzz.codegen.prepare",
    "assurance.generation.fuzz.plan-review.prepare",
    "assurance.generation.fuzz.plan.prepare",
    "assurance.generation.performance.codegen.prepare",
    "assurance.generation.performance.plan-review.prepare",
    "assurance.generation.performance.plan.prepare",
    "assurance.execution.execute.prepare",
    "assurance.execution.run.prepare",
    "assurance.healing.coverage-repair.prepare",
    "assurance.healing.fix-proposal.prepare",
    "assurance.quality.fact-baseline.prepare",
    "assurance.quality.inspect.prepare",
    "assurance.quality.issue-analysis.prepare",
    "assurance.quality.issue-triage.prepare",
    "assurance.quality.report.prepare",
    "assurance.improvement.archive.prepare",
    "assurance.improvement.improvement-review.prepare",
    "assurance.improvement.retro-eval-analysis.prepare",
    "assurance.improvement.retro-issue-analysis.prepare",
    "assurance.improvement.retro-workflow-analysis.prepare",
    "assurance.improvement.retro.prepare",
)

_SHA256 = r"^[0-9a-f]{64}$"
_ROUTING_MARKERS = (",", ";", "|", "->", "fallback", "route:", "candidates")
_TEMPLATE_OR_GLOB = ("{", "}", "*", "?", "[", "]", "\\", "`", "$", "<", ">")
_CURSOR_ENVIRONMENT_NAMES = frozenset({"PATH", "CURSOR_API_KEY"})
_SHELL_FRAGMENTS = (
    ";",
    "|",
    "&",
    "$",
    "`",
    "(",
    ")",
    "<",
    ">",
    "*",
    "?",
    "[",
    "]",
    "{",
    "}",
    "~",
    "\n",
    "\r",
)


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


class CursorBindingV1(FrozenModel):
    schema_version: Literal["1"]
    executable: str
    executable_digest: str = Field(pattern=_SHA256)
    expected_version: str = Field(min_length=1)
    protocol_profile: Literal["confined_process"]
    secret_handle: str | None = None
    environment_names: tuple[str, ...]
    graceful_cancel_seconds: float = Field(gt=0, le=60)
    forced_cancel_seconds: float = Field(gt=0, le=60)
    max_output_bytes: int = Field(gt=0, le=16_000_000)
    max_line_bytes: int = Field(gt=0, le=1_000_000)
    adapter_configuration_digest: str = Field(pattern=_SHA256)

    @field_validator("executable")
    @classmethod
    def _executable(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("executable must be a single token")
        if any(fragment in value for fragment in _SHELL_FRAGMENTS):
            raise ValueError("executable must not contain shell fragments")
        if value.startswith("\\\\") or not value.startswith("/"):
            raise ValueError("executable must be an exact absolute path")
        if any(part in {".", ".."} for part in value.split("/") if part):
            raise ValueError("executable must not contain relative path segments")
        return value

    @field_validator("expected_version")
    @classmethod
    def _expected_version(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("expected_version must be a single token")
        if any(fragment in value for fragment in _SHELL_FRAGMENTS):
            raise ValueError("expected_version must not contain shell fragments")
        return value

    @field_validator("secret_handle")
    @classmethod
    def _secret_handle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _secret_handle_name(value)

    @field_validator("environment_names")
    @classmethod
    def _environment_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("environment_names must be unique")
        unknown = [name for name in value if name not in _CURSOR_ENVIRONMENT_NAMES]
        if unknown:
            raise ValueError("environment_names must be the closed PATH/CURSOR_API_KEY allowlist")
        return value

    @model_validator(mode="after")
    def _authenticate_secret_injection(self) -> CursorBindingV1:
        injects_key = "CURSOR_API_KEY" in self.environment_names
        if injects_key != (self.secret_handle is not None):
            raise ValueError("CURSOR_API_KEY injection requires secret_handle and nothing else")
        return self


class DeploymentBindingsV1(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    runtime_plugin_id: Literal["runtime.opencode", "runtime.cursor"]
    adapter_binding: OpenCodeBindingV1 | CursorBindingV1
    routes: Mapping[str, RouteAssignmentV1]
    permission_profiles: Mapping[str, PermissionProfileV1]
    request_policies: Mapping[str, RequestPolicyV1]
    secret_handles: tuple[str, ...]

    @model_validator(mode="before")
    @classmethod
    def _validate_adapter_schema(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        runtime = value.get("runtime_plugin_id")
        adapter = value.get("adapter_binding")
        if not isinstance(adapter, dict):
            return value
        copied = dict(value)
        if runtime == "runtime.opencode":
            copied["adapter_binding"] = OpenCodeBindingV1.model_validate(adapter)
        elif runtime == "runtime.cursor":
            copied["adapter_binding"] = CursorBindingV1.model_validate(adapter)
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
        expected = set(PREPARE_IDS)
        actual = set(self.routes)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            raise ValueError(
                f"routes must contain exactly the 33 prepare IDs; missing={missing!r} extra={extra!r}"
            )
        for assignment in self.routes.values():
            if assignment.permission_profile_id not in self.permission_profiles:
                raise ValueError("permission_profile_id must resolve in permission_profiles")
            if assignment.request_policy_id not in self.request_policies:
                raise ValueError("request_policy_id must resolve in request_policies")
        if self.runtime_plugin_id == "runtime.opencode" and not isinstance(
            self.adapter_binding, OpenCodeBindingV1
        ):
            raise ValueError("runtime.opencode requires OpenCodeBindingV1")
        if self.runtime_plugin_id == "runtime.cursor" and not isinstance(
            self.adapter_binding, CursorBindingV1
        ):
            raise ValueError("runtime.cursor requires CursorBindingV1")
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
    plugin_version: Literal["1.0.0"]


def adapter_secret_handles(binding: OpenCodeBindingV1 | CursorBindingV1) -> tuple[str, ...]:
    handle = binding.secret_handle
    if handle is None:
        return ()
    return (handle,)


def alias_ids_for_prepare(prepare_id: str) -> tuple[str, str, str]:
    stem = prepare_id.removesuffix(".prepare")
    key = stem.removeprefix("assurance.")
    return (
        f"assurance.product.agent.{key}.prepare",
        f"assurance.product.agent.{key}.execute",
        f"assurance.product.agent.{key}.finalize",
    )


def all_binding_ids() -> tuple[str, ...]:
    return tuple(alias for prepare_id in PREPARE_IDS for alias in alias_ids_for_prepare(prepare_id))


def finalize_aliases() -> tuple[str, ...]:
    return tuple(alias_ids_for_prepare(prepare_id)[2] for prepare_id in PREPARE_IDS)


def expected_finalize_ids() -> frozenset[str]:
    return frozenset(prepare_id.removesuffix(".prepare") + ".finalize" for prepare_id in PREPARE_IDS)


TEST_FAMILY_ORDER: tuple[Literal["api", "e2e", "fuzz", "performance"], ...] = (
    "api",
    "e2e",
    "fuzz",
    "performance",
)
FAMILY_NONEMPTY_ENTRYPOINTS = frozenset({"full", "execute"})
FAMILY_EMPTY_ENTRYPOINTS = frozenset(
    {
        "intake",
        "case",
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


class ProductInputV1(FrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    change_id: str
    requirement: str
    run_mode: Literal["case", "implement", "verify"]
    selected_test_families: tuple[Literal["api", "e2e", "fuzz", "performance"], ...]
    auto_archive: bool
    capability_catalog: ResourceRefV1
    product_policy: ResourceRefV1
    data_knowledge: ResourceRefV1
    allowed_artifact_paths: tuple[str, ...]
    budgets: BusinessBudgetsV1

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_token(value, "change_id")

    @field_validator("requirement")
    @classmethod
    def _requirement(cls, value: str) -> str:
        return _canonical_text(value, "requirement")

    @field_validator("selected_test_families")
    @classmethod
    def _selected_test_families(
        cls, value: tuple[Literal["api", "e2e", "fuzz", "performance"], ...]
    ) -> tuple[Literal["api", "e2e", "fuzz", "performance"], ...]:
        if len(set(value)) != len(value):
            raise ValueError("selected_test_families must be unique")
        order = {name: index for index, name in enumerate(TEST_FAMILY_ORDER)}
        if tuple(sorted(value, key=order.__getitem__)) != value:
            raise ValueError("selected_test_families must be in canonical family order")
        return value

    @field_validator("allowed_artifact_paths")
    @classmethod
    def _allowed_artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_artifact_prefixes(value)

    def validate_for_entrypoint(self, entrypoint: str) -> ProductInputV1:
        validate_entrypoint_families(entrypoint, self.selected_test_families)
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
        raise ValueError(f"{entrypoint} requires a non-empty selected_test_families tuple")
    if entrypoint in FAMILY_EMPTY_ENTRYPOINTS and families:
        raise ValueError(f"{entrypoint} requires an empty selected_test_families tuple")


def authenticate_product_input_resources(value: ProductInputV1, composition: object) -> None:
    resources = getattr(getattr(getattr(composition, "registries", None), "resources", None), "entries", None)
    if not isinstance(resources, Mapping):
        raise ValueError("composition resource registry is unavailable")
    refs = (
        value.capability_catalog,
        value.product_policy,
        value.data_knowledge,
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


class StatusV1(FrozenModel):
    schema_version: Literal["1"]
    invocation_id: str
    lock_digest: str
    root_input_digest: str
    initial_tree_id: str
    current_head_tree_id: str
    status: Literal["running", "blocked", "interrupted", "stopped", "failed", "completed"]
    entrypoint: str
    graph_hierarchy: tuple[GraphStatusV1, ...]
    node_states: tuple[NodeStatusV1, ...]
    selected_test_families: tuple[str, ...]
    coverage_progress: CoverageProgressV1 | None
    durable_effects: tuple[EffectStatusV1, ...]
    adapter_evidence: tuple[AdapterEvidenceRefV1, ...]
    pending_interrupt: PendingInterruptStatusV1 | None
    terminal_reason: str | None


class ExportedArtifactV1(FrozenModel):
    artifact_id: str
    relative_path: str
    media_type: str
    sha256: str = Field(pattern=_SHA256)

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id(cls, value: str) -> str:
        return _canonical_token(value, "artifact_id")

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        prefixes = _canonical_artifact_prefixes((value,))
        return prefixes[0]

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str) -> str:
        return _canonical_token(value, "media_type")


class ResultExportV1(FrozenModel):
    schema_version: Literal["1"]
    invocation_id: str
    lock_digest: str = Field(pattern=_SHA256)
    event_stream_digest: str = Field(pattern=_SHA256)
    result_tree_digest: str
    status: StatusV1
    artifact_index: tuple[ExportedArtifactV1, ...]

    @field_validator("result_tree_digest")
    @classmethod
    def _result_tree_digest(cls, value: str) -> str:
        prefix, separator, digest = value.partition(":")
        if prefix != "sha256" or not separator or len(digest) != 64:
            raise ValueError("result_tree_digest must be sha256: plus 64 lowercase hex")
        if any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("result_tree_digest must be sha256: plus 64 lowercase hex")
        return value
