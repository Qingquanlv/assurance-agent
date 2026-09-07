"""Pure contracts for deterministic case execution plans."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self, cast

from pydantic import Field, TypeAdapter, field_validator, model_validator

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.verification import (
    AssertionSourcesV1,
    BusinessAssertionV1,
    InputExpectedV1,
    LiteralExpectedV1,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1, require_same_plan

_SHA256 = r"^[0-9a-f]{64}$"
ValidationProfile = Literal["api_db.v1", "api_db_trace.v1"]
InputKey = Literal["username", "email", "is_active", "is_superuser", "dept_id"]
UserField = InputKey

HTTP_ACTION_BINDING_ID = "assurance.execution.http.user-create.v1"
USER_SQLITE_BINDING_ID = "assurance.execution.sqlite.user.v1"
TRACE_USER_WRITE_BINDING_ID = "assurance.execution.trace.sqlite-user-write.v1"
TRACE_DRAIN_BINDING_ID = "assurance.execution.trace.drain.v1"
BINDING_VERSION = "1"

BASE_RUNTIME_OBLIGATIONS = frozenset({"initial.user_absent", "action.finished", "oracle.executed"})
TRACE_OBLIGATIONS = frozenset({"trace.http", "trace.user_write", "trace.user_completed", "trace.drained"})
USER_INPUT_KEYS = frozenset({"username", "email", "is_active", "is_superuser", "dept_id"})
USER_ASSERTION_SHAPES: dict[str, tuple[str, str, str | None]] = {
    "api.code": ("create.response.business_code", "eq", None),
    "api.http_status": ("create.response.http_status", "eq", None),
    "user.dept_id": ("created_user.dept_id", "eq", "dept_id"),
    "user.email": ("created_user.email", "eq", "email"),
    "user.is_active": ("created_user.is_active", "eq", "is_active"),
    "user.is_superuser": ("created_user.is_superuser", "eq", "is_superuser"),
    "user.row_count": ("created_user.count", "row_count_eq", None),
    "user.username": ("created_user.username", "eq", "username"),
}


def required_obligations(assertion_ids: frozenset[str], profile: str) -> frozenset[str]:
    if profile not in {"api_db.v1", "api_db_trace.v1"}:
        raise ValueError("unknown validation profile")
    trace_ids = TRACE_OBLIGATIONS if profile == "api_db_trace.v1" else frozenset()
    return assertion_ids | BASE_RUNTIME_OBLIGATIONS | trace_ids


def validate_user_assertions(assertions: tuple[BusinessAssertionV1, ...]) -> None:
    """Require the complete first-version User assertion catalog and legal references."""
    by_id = {assertion.assertion_id: assertion for assertion in assertions}
    if set(by_id) != set(USER_ASSERTION_SHAPES) or len(by_id) != len(assertions):
        raise ValueError("business assertion obligations must be complete and unique")
    for assertion_id, (subject, comparator, input_key) in USER_ASSERTION_SHAPES.items():
        assertion = by_id[assertion_id]
        if assertion.subject != subject:
            raise ValueError(f"business assertion subject is unsupported: {assertion_id}")
        if assertion.comparator != comparator:
            raise ValueError(f"business assertion comparator is unsupported: {assertion_id}")
        if input_key is None:
            if not isinstance(assertion.expected, LiteralExpectedV1):
                raise ValueError(f"business assertion expected reference is unsupported: {assertion_id}")
        elif not isinstance(assertion.expected, InputExpectedV1) or assertion.expected.key != input_key:
            raise ValueError(f"business assertion expected reference is unsupported: {assertion_id}")


class CasePlanContextV1(FrozenModel):
    """Authenticated identities supplied by the generation host, never the planner."""

    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    reviewed_case: ReviewedCaseV1
    verification_policy_digest: str = Field(pattern=_SHA256)
    technical_config_digest: str = Field(pattern=_SHA256)
    sut_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _bind_reviewed_case(self) -> Self:
        if self.reviewed_case.change_id != self.change_id:
            raise ValueError("reviewed case change_id does not match plan context")
        if self.reviewed_case.coverage_epoch != self.coverage_epoch:
            raise ValueError("reviewed case epoch does not match plan context")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        return self


class HttpActionBindingV1(FrozenModel):
    kind: Literal["http_action"]
    binding_id: Literal["assurance.execution.http.user-create.v1"]
    binding_version: Literal["1"]
    action_key: Literal["create_user"]
    method: Literal["POST"]
    path: Literal["/api/v1/user/create"]
    request_fields: dict[InputKey, InputKey]
    credential_ref: str = Field(min_length=1)

    @field_validator("request_fields")
    @classmethod
    def _all_input_fields(cls, value: dict[InputKey, InputKey]) -> dict[InputKey, InputKey]:
        expected = {"username", "email", "is_active", "is_superuser", "dept_id"}
        if set(value) != expected or any(key != source for key, source in value.items()):
            raise ValueError("HTTP request fields must bind every frozen User input exactly once")
        return value


class HttpStatusBindingV1(FrozenModel):
    kind: Literal["http_status"]
    action_key: Literal["create_user"]


class HttpJsonFieldBindingV1(FrozenModel):
    kind: Literal["http_json_field"]
    action_key: Literal["create_user"]
    field: Literal["code"]


class SqliteUserAbsenceBindingV1(FrozenModel):
    kind: Literal["sqlite_user_absence"]
    oracle_key: Literal["created_user"]
    binding_id: Literal["assurance.execution.sqlite.user.v1"]
    binding_version: Literal["1"]


class SqliteUserObservationBindingV1(FrozenModel):
    kind: Literal["sqlite_user_observation"]
    oracle_key: Literal["created_user"]
    binding_id: Literal["assurance.execution.sqlite.user.v1"]
    binding_version: Literal["1"]


class SqliteUserRowCountBindingV1(FrozenModel):
    kind: Literal["sqlite_user_row_count"]
    oracle_key: Literal["created_user"]


class SqliteUserFieldBindingV1(FrozenModel):
    kind: Literal["sqlite_user_field"]
    oracle_key: Literal["created_user"]
    field: UserField


class TraceHttpBindingV1(FrozenModel):
    kind: Literal["trace_http"]
    action_key: Literal["create_user"]


class TraceUserWriteBindingV1(FrozenModel):
    kind: Literal["trace_user_write"]
    binding_id: Literal["assurance.execution.trace.sqlite-user-write.v1"]
    binding_version: Literal["1"]


class TraceCheckpointBindingV1(FrozenModel):
    kind: Literal["trace_checkpoint"]
    checkpoint_id: Literal["user.create.completed"]
    checkpoint_version: Literal["1"]


class TraceDrainBindingV1(FrozenModel):
    kind: Literal["trace_drain"]
    binding_id: Literal["assurance.execution.trace.drain.v1"]
    binding_version: Literal["1"]


ActualBindingV1 = Annotated[
    HttpActionBindingV1
    | HttpStatusBindingV1
    | HttpJsonFieldBindingV1
    | SqliteUserAbsenceBindingV1
    | SqliteUserObservationBindingV1
    | SqliteUserRowCountBindingV1
    | SqliteUserFieldBindingV1
    | TraceHttpBindingV1
    | TraceUserWriteBindingV1
    | TraceCheckpointBindingV1
    | TraceDrainBindingV1,
    Field(discriminator="kind"),
]
ACTUAL_BINDING_ADAPTER = TypeAdapter(ActualBindingV1)


class ExecutionBindingsV1(FrozenModel):
    """Agent-authored actual-value locators; no expected or readiness fields."""

    schema_version: Literal["1"] = "1"
    case_id: str = Field(min_length=1)
    bindings: dict[str, ActualBindingV1]

    @field_validator("bindings")
    @classmethod
    def _nonempty_bindings(cls, value: dict[str, ActualBindingV1]) -> dict[str, ActualBindingV1]:
        if not value:
            raise ValueError("execution bindings must not be empty")
        return value


class ObligationBindingV1(FrozenModel):
    obligation_id: str = Field(min_length=1)
    actual: ActualBindingV1
    expected_id: str | None = Field(default=None, min_length=1)
    comparator: Literal["eq", "row_count_eq"] | None = None


class HttpActionV1(FrozenModel):
    action_key: Literal["create_user"]
    binding_id: Literal["assurance.execution.http.user-create.v1"]
    binding_version: Literal["1"]
    method: Literal["POST"]
    path: Literal["/api/v1/user/create"]
    request_fields: dict[InputKey, InputKey]
    credential_ref: str = Field(min_length=1)
    response_obligations: tuple[Literal["api.code", "api.http_status"], ...]

    @field_validator("response_obligations")
    @classmethod
    def _response_obligations(
        cls, value: tuple[Literal["api.code", "api.http_status"], ...]
    ) -> tuple[Literal["api.code", "api.http_status"], ...]:
        if value != ("api.code", "api.http_status"):
            raise ValueError("HTTP response obligations must be sorted and complete")
        return value


class SqliteUserOracleV1(FrozenModel):
    oracle_key: Literal["created_user"]
    binding_id: Literal["assurance.execution.sqlite.user.v1"]
    binding_version: Literal["1"]
    lookup_inputs: tuple[Literal["username"], Literal["email"]]
    fields: tuple[UserField, ...]
    assertion_obligations: tuple[str, ...]

    @model_validator(mode="after")
    def _fixed_query_shape(self) -> Self:
        if self.lookup_inputs != ("username", "email"):
            raise ValueError("SQLite User lookup must bind username and email")
        if self.fields != ("username", "email", "is_active", "is_superuser", "dept_id"):
            raise ValueError("SQLite User binding must use the fixed projected fields")
        expected = (
            "user.dept_id",
            "user.email",
            "user.is_active",
            "user.is_superuser",
            "user.row_count",
            "user.username",
        )
        if self.assertion_obligations != expected:
            raise ValueError("SQLite User assertion obligations must be sorted and complete")
        return self


class InitialStateV1(FrozenModel):
    obligation_id: Literal["initial.user_absent"]
    oracle_key: Literal["created_user"]
    lookup_inputs: tuple[Literal["username"], Literal["email"]]
    expected_row_count: Literal[0]


class TraceNotRequiredV1(FrozenModel):
    status: Literal["not_required"]


class TraceRequirementsV1(FrozenModel):
    status: Literal["required"]
    http_obligation: Literal["trace.http"]
    user_write_obligation: Literal["trace.user_write"]
    checkpoint_obligation: Literal["trace.user_completed"]
    checkpoint_id: Literal["user.create.completed"]
    checkpoint_version: Literal["1"]
    drain_obligation: Literal["trace.drained"]
    require_same_action_and_sut: Literal[True]


TracePlanV1 = Annotated[TraceNotRequiredV1 | TraceRequirementsV1, Field(discriminator="status")]


class CompletionV1(FrozenModel):
    action_semantics: Literal["synchronous"]
    http_timeout_seconds: Literal[10]
    oracle_timeout_seconds: Literal[2]
    telemetry_timeout_seconds: Literal[10]
    missing_evidence: Literal["incomplete"]
    obligations: tuple[str, ...]


class CaseExecutionPlanV1(FrozenModel):
    """One case's frozen specification plus installed execution bindings."""

    schema_version: Literal["1"] = "1"
    change_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    reviewed_case: ReviewedCaseV1
    case_ref: EvidenceArtifactRefV1
    spec_digest: str = Field(pattern=_SHA256)
    assertion_sources_digest: str = Field(pattern=_SHA256)
    verification_policy_digest: str = Field(pattern=_SHA256)
    technical_config_digest: str = Field(pattern=_SHA256)
    sut_digest: str = Field(pattern=_SHA256)
    validation_profile: ValidationProfile
    inputs: dict[InputKey, Any]
    assertions: tuple[BusinessAssertionV1, ...] = Field(min_length=1)
    action: HttpActionV1
    initial_state: InitialStateV1
    oracle: SqliteUserOracleV1
    trace: TracePlanV1
    completion: CompletionV1
    required: tuple[str, ...] = Field(min_length=1)
    bindings: tuple[ObligationBindingV1, ...] = Field(min_length=1)

    def obligation_projection(self) -> JSONValue:
        """Return the generation-owned business contract a repair must preserve.

        Technical binding versions, credentials and authenticated SUT/config
        digests may change when generation recompiles the same specification.
        Everything that determines business meaning remains in this projection.
        """

        technical = {"binding_id", "binding_version", "credential_ref"}
        action = self.action.model_dump(mode="json", exclude=technical)
        oracle = self.oracle.model_dump(mode="json", exclude=technical)
        bindings = [
            {
                "obligation_id": binding.obligation_id,
                "expected_id": binding.expected_id,
                "comparator": binding.comparator,
                "actual": binding.actual.model_dump(mode="json", exclude=technical),
            }
            for binding in self.bindings
        ]
        return cast(
            JSONValue,
            self.model_dump(
                mode="json",
                exclude={
                    "technical_config_digest",
                    "sut_digest",
                    "reviewed_case",
                    "action",
                    "oracle",
                    "bindings",
                },
            )
            | {"action": action, "oracle": oracle, "bindings": bindings},
        )

    @field_validator("inputs")
    @classmethod
    def _complete_inputs(cls, value: dict[InputKey, Any]) -> dict[InputKey, Any]:
        if set(value) != USER_INPUT_KEYS:
            raise ValueError("User inputs must be complete")
        return value

    @model_validator(mode="after")
    def _closed_plan(self) -> Self:
        if self.reviewed_case.change_id != self.change_id:
            raise ValueError("reviewed case change_id does not match execution plan")
        if self.reviewed_case.coverage_epoch != self.coverage_epoch:
            raise ValueError("reviewed case epoch does not match execution plan")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        if self.case_ref not in self.reviewed_case.case_refs:
            raise ValueError("case_ref is not authenticated by ReviewedCase")
        assertion_ids = tuple(assertion.assertion_id for assertion in self.assertions)
        if assertion_ids != tuple(sorted(set(assertion_ids))):
            raise ValueError("business assertion IDs must be sorted and unique")
        validate_user_assertions(self.assertions)
        derived = tuple(sorted(required_obligations(frozenset(assertion_ids), self.validation_profile)))
        if self.required != derived:
            raise ValueError("required obligations must exactly match assertions and validation profile")
        if self.completion.obligations != self.required:
            raise ValueError("completion obligations must exactly match required obligations")
        binding_ids = tuple(binding.obligation_id for binding in self.bindings)
        if binding_ids != tuple(sorted(set(binding_ids))):
            raise ValueError("obligation bindings must be sorted and unique")
        if binding_ids != self.required:
            raise ValueError("every required obligation must have exactly one binding")
        assertions = {assertion.assertion_id: assertion for assertion in self.assertions}
        for binding in self.bindings:
            assertion = assertions.get(binding.obligation_id)
            if assertion is None:
                if binding.expected_id is not None or binding.comparator is not None:
                    raise ValueError("runtime obligations cannot carry expected references")
            elif binding.expected_id != assertion.assertion_id or binding.comparator != assertion.comparator:
                raise ValueError(f"dangling or mismatched expected: {binding.obligation_id}")
            expected_kind = {
                "action.finished": "http_action",
                "api.code": "http_json_field",
                "api.http_status": "http_status",
                "initial.user_absent": "sqlite_user_absence",
                "oracle.executed": "sqlite_user_observation",
                "trace.drained": "trace_drain",
                "trace.http": "trace_http",
                "trace.user_completed": "trace_checkpoint",
                "trace.user_write": "trace_user_write",
                "user.dept_id": "sqlite_user_field",
                "user.email": "sqlite_user_field",
                "user.is_active": "sqlite_user_field",
                "user.is_superuser": "sqlite_user_field",
                "user.row_count": "sqlite_user_row_count",
                "user.username": "sqlite_user_field",
            }.get(binding.obligation_id)
            if expected_kind is None or binding.actual.kind != expected_kind:
                raise ValueError(f"unsupported actual locator for {binding.obligation_id}")
            expected_field = {
                "api.code": "code",
                "user.dept_id": "dept_id",
                "user.email": "email",
                "user.is_active": "is_active",
                "user.is_superuser": "is_superuser",
                "user.username": "username",
            }.get(binding.obligation_id)
            if expected_field is not None and getattr(binding.actual, "field", None) != expected_field:
                raise ValueError(f"unsupported actual field for {binding.obligation_id}")
        trace_required = self.validation_profile == "api_db_trace.v1"
        if trace_required != isinstance(self.trace, TraceRequirementsV1):
            raise ValueError("validation profile conflicts with Trace requirements")
        return self


class CaseExecutionPlanSetV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    change_id: str = Field(min_length=1)
    cases: tuple[CaseExecutionPlanV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_set(self) -> Self:
        ids = tuple(case.case_id for case in self.cases)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("case execution plans must be sorted and unique")
        if any(case.change_id != self.change_id for case in self.cases):
            raise ValueError("case execution plan change_id does not match plan set")
        identities = {
            (
                case.plan_digest,
                case.plan_ref.path,
                case.plan_ref.digest,
                case.coverage_epoch,
                case.verification_policy_digest,
                case.technical_config_digest,
                case.sut_digest,
                case.validation_profile,
            )
            for case in self.cases
        }
        if len(identities) != 1:
            raise ValueError("case execution plans must share one authenticated context")
        return self


def validate_case_plan_sources(
    plan: CaseExecutionPlanV1,
    *,
    case_id: str,
    revision: str,
    spec_digest: str,
    assertions: tuple[BusinessAssertionV1, ...],
    sources: AssertionSourcesV1,
) -> None:
    """Recheck a formal plan against independently authenticated specification inputs."""
    if (plan.case_id, plan.revision, plan.spec_digest) != (case_id, revision, spec_digest):
        raise ValueError("case execution plan does not match the frozen specification digest")
    if plan.assertions != assertions:
        raise ValueError("case execution plan assertions do not match the frozen specification")
    if (sources.case_id, sources.revision, sources.spec_digest) != (case_id, revision, spec_digest):
        raise ValueError("assertion sources do not match the frozen specification digest")
    source_ids = {source.source_id for source in sources.sources}
    missing = sorted(assertion.source_id for assertion in assertions if assertion.source_id not in source_ids)
    if missing:
        raise ValueError(f"business assertion has missing authoritative source: {missing[0]}")
    digest = canonical_digest(cast(JSONValue, sources.model_dump(mode="json")))
    if plan.assertion_sources_digest != digest:
        raise ValueError("case execution plan assertion source digest does not match")


__all__ = [
    "ACTUAL_BINDING_ADAPTER",
    "ActualBindingV1",
    "BASE_RUNTIME_OBLIGATIONS",
    "BINDING_VERSION",
    "CaseExecutionPlanSetV1",
    "CaseExecutionPlanV1",
    "CasePlanContextV1",
    "CompletionV1",
    "ExecutionBindingsV1",
    "HTTP_ACTION_BINDING_ID",
    "HttpActionBindingV1",
    "HttpActionV1",
    "InitialStateV1",
    "InputKey",
    "ObligationBindingV1",
    "SqliteUserFieldBindingV1",
    "SqliteUserOracleV1",
    "TRACE_OBLIGATIONS",
    "TraceNotRequiredV1",
    "TraceRequirementsV1",
    "USER_SQLITE_BINDING_ID",
    "USER_ASSERTION_SHAPES",
    "USER_INPUT_KEYS",
    "ValidationProfile",
    "required_obligations",
    "validate_case_plan_sources",
    "validate_user_assertions",
]
