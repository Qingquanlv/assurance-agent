"""PR-cadence metrics, mutation, assertion, drift, and adversarial handlers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts import RiskTier
from assurance_quality.contracts.metrics import (
    METRIC_KEYS,
    MetricEntry,
    MetricKey,
    MetricShortboard,
    MetricsDocument,
    RiskDeclarationLowered,
)
from assurance_quality.contracts.pr_metrics import (
    AdversarialYieldEvidence,
    AssertionStrengthEvidence,
    AssertionStrengthSurfaceSlice,
    AuthMatrixEvidence,
    BaselineDriftEvidence,
    BaselineDriftScenario,
    ConstraintCoverageEvidence,
    CoverageDiffEvidence,
    JourneyCoverageEvidence,
    MutationEvidence,
    MutationSurvivor,
    PerfSlackEvidence,
)
from assurance_quality.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    json_digest,
    scope_of,
    succeeded,
    validate_input,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class BoundRisk:
    def __init__(
        self,
        *,
        tier: RiskTier = "medium",
        lower_bound: RiskTier = "medium",
        declared: RiskTier | None = None,
        lowered: tuple[RiskDeclarationLowered, ...] = (),
    ) -> None:
        resolved: RiskTier
        if declared is None or _rank(declared) <= _rank(lower_bound):
            resolved = lower_bound
        else:
            resolved = declared
        del tier
        self.tier: RiskTier = resolved
        self.lower_bound: RiskTier = lower_bound
        self.declared: RiskTier | None = declared
        self.declaration_lowered = bool(lowered)
        self.lowered_declarations = lowered


def _rank(tier: RiskTier) -> int:
    return ("low", "medium", "high", "critical").index(tier)


class RiskInput(BaseModel):
    model_config = _FROZEN

    tier: RiskTier = "medium"
    lower_bound: RiskTier = "medium"
    declared: RiskTier | None = None


class PrEvidenceBundle(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    computed_at: datetime = Field(default_factory=lambda: datetime(2026, 8, 22, tzinfo=UTC))
    policy_digest: str
    risk: RiskInput = RiskInput()
    cadence: Literal["pr", "nightly"] = "pr"
    diff: CoverageDiffEvidence | None = None
    constraint: ConstraintCoverageEvidence | None = None
    auth: AuthMatrixEvidence | None = None
    journey: JourneyCoverageEvidence | None = None
    slack: PerfSlackEvidence | None = None
    mutation: MutationEvidence | None = None
    assertion: AssertionStrengthEvidence | None = None
    drift: BaselineDriftEvidence | None = None
    adversarial: AdversarialYieldEvidence | None = None


class MutationSampleInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    killed: int = 0
    survived: int = 0
    equivalent: int = 0
    selected: int
    budget_seconds: int = 60
    elapsed_seconds: float = 0.0
    seed: int = 0
    survivors: tuple[MutationSurvivor, ...] = ()
    source: dict[str, str] = Field(default_factory=dict)


class AssertionStrengthInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    surfaces: tuple[AssertionStrengthSurfaceSlice, ...]
    source: dict[str, str] = Field(default_factory=dict)


class BaselineDriftInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    scenarios: tuple[BaselineDriftScenario, ...]
    drift_band: float = 0.2
    window: int = 5
    source: dict[str, str] = Field(default_factory=dict)


class AdversarialYieldInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    property: str
    layer: Literal["api", "e2e", "fuzz"] = "api"
    sample_count: int
    counterexample_ids: tuple[str, ...] = ()
    unclosed_count: int = 0
    seed: int = 0
    source: dict[str, str] = Field(default_factory=dict)


class LatestMetricsInput(BaseModel):
    model_config = _FROZEN

    documents: tuple[MetricsDocument, ...]
    change_id: str | None = None


class MutationSampleHost(Protocol):
    def sample(self, payload: MutationSampleInput) -> MutationEvidence: ...


class ClosedMutationHost:
    def sample(self, payload: MutationSampleInput) -> MutationEvidence:
        tested = payload.killed + payload.survived + payload.equivalent
        denom = payload.killed + payload.survived
        return MutationEvidence(
            schema_version="1",
            change_id=payload.change_id,
            batch_id=payload.batch_id,
            status="evaluated" if denom else "not_evaluated",
            value=None if denom == 0 else payload.killed / denom,
            killed=payload.killed,
            survived=payload.survived,
            equivalent=payload.equivalent,
            tested=tested,
            selected=payload.selected,
            budget_seconds=payload.budget_seconds,
            elapsed_seconds=payload.elapsed_seconds,
            budget_exceeded=payload.elapsed_seconds > payload.budget_seconds,
            seed=payload.seed,
            survivors=payload.survivors,
            shortboards=(
                (MetricShortboard(code="mutation_budget_exceeded", metric="mutation_score"),)
                if payload.elapsed_seconds > payload.budget_seconds
                else ()
            ),
            source=dict(payload.source),
        )


def _entry(
    *,
    layer: Literal["api", "e2e", "fuzz", "performance", "backend", "cross"],
    status: Literal["evaluated", "not_evaluated", "skipped", "collection_failed"] = "skipped",
    value: float | None = None,
    holds: bool | None = None,
    declared: object = None,
    evidence: str = "",
    surfaces: tuple[object, ...] = (),
) -> MetricEntry:
    return MetricEntry(
        layer=layer,
        status=status,
        value=value,
        holds=holds,
        declared=declared,  # type: ignore[arg-type]
        evidence=evidence,
        surfaces=surfaces,  # type: ignore[arg-type]
    )


def _skipped(layer: Literal["api", "e2e", "fuzz", "performance", "backend", "cross"]) -> MetricEntry:
    return _entry(layer=layer, status="skipped")


def build_metrics_document(bundle: PrEvidenceBundle) -> MetricsDocument:
    metrics: dict[MetricKey, MetricEntry] = {
        "diff_coverage": _skipped("backend"),
        "constraint_coverage": _skipped("api"),
        "auth_matrix_coverage": _skipped("api"),
        "journey_coverage": _skipped("e2e"),
        "baseline_drift": _skipped("performance"),
        "mutation_score": _skipped("backend"),
        "assertion_strength": _skipped("cross"),
        "threshold_slack": _skipped("performance"),
        "adversarial_yield": _skipped("cross") if False else _skipped("api"),
        "adversarial_clean": _skipped("cross"),
    }
    shortboards: list[MetricShortboard] = []
    if bundle.diff is not None:
        metrics["diff_coverage"] = _entry(
            layer="backend",
            status="evaluated" if bundle.diff.value is not None else "not_evaluated",
            value=bundle.diff.value,
            evidence=json_digest(bundle.diff.model_dump(mode="json")),
        )
        shortboards.extend(bundle.diff.shortboards)
    if bundle.constraint is not None and bundle.constraint.declared is not None:
        metrics["constraint_coverage"] = _entry(
            layer="api",
            status="evaluated",
            value=bundle.constraint.value,
            declared=bundle.constraint.declared,
            evidence=json_digest(bundle.constraint.model_dump(mode="json")),
        )
    if bundle.auth is not None and bundle.auth.declared is not None:
        metrics["auth_matrix_coverage"] = _entry(
            layer="api",
            status="evaluated",
            value=bundle.auth.value,
            declared=bundle.auth.declared,
            evidence=json_digest(bundle.auth.model_dump(mode="json")),
        )
    if bundle.journey is not None and bundle.journey.declared is not None:
        metrics["journey_coverage"] = _entry(
            layer="e2e",
            status="evaluated",
            value=bundle.journey.value,
            declared=bundle.journey.declared,
            evidence=json_digest(bundle.journey.model_dump(mode="json")),
        )
    if bundle.slack is not None:
        metrics["threshold_slack"] = _entry(
            layer="performance",
            status="evaluated" if bundle.slack.value is not None else "not_evaluated",
            value=bundle.slack.value,
            evidence=json_digest(bundle.slack.model_dump(mode="json")),
        )
        shortboards.extend(bundle.slack.shortboards)
    if bundle.mutation is not None:
        metrics["mutation_score"] = _entry(
            layer="backend",
            status=bundle.mutation.status
            if bundle.mutation.status != "collection_failed"
            else "collection_failed",
            value=bundle.mutation.value,
            evidence=json_digest(bundle.mutation.model_dump(mode="json")),
        )
        shortboards.extend(bundle.mutation.shortboards)
    if (
        bundle.assertion is not None
        and bundle.assertion.status == "evaluated"
        and bundle.assertion.declared is not None
    ):
        metrics["assertion_strength"] = MetricEntry(
            layer="cross",
            status="evaluated",
            value=bundle.assertion.value,
            declared=bundle.assertion.declared,
            evidence=json_digest(bundle.assertion.model_dump(mode="json")),
            surfaces=tuple(
                {
                    "layer": surface.layer,
                    "value": surface.value if surface.value is not None else 0.0,
                    "declared": surface.declared.model_dump(mode="json"),
                    "evidence": json_digest(surface.model_dump(mode="json")),
                }
                for surface in bundle.assertion.surfaces
            ),  # type: ignore[arg-type]
        )
    if bundle.drift is not None:
        metrics["baseline_drift"] = _entry(
            layer="performance",
            status=bundle.drift.status if bundle.drift.status != "collection_failed" else "collection_failed",
            value=bundle.drift.value,
            evidence=json_digest(bundle.drift.model_dump(mode="json")),
        )
        shortboards.extend(bundle.drift.shortboards)
    if bundle.adversarial is not None:
        metrics["adversarial_yield"] = _entry(
            layer=bundle.adversarial.layer,
            status=bundle.adversarial.status
            if bundle.adversarial.status != "collection_failed"
            else "collection_failed",
            value=bundle.adversarial.value,
            evidence=json_digest(bundle.adversarial.model_dump(mode="json")),
        )
        metrics["adversarial_clean"] = _entry(
            layer="cross",
            status="evaluated",
            holds=bundle.adversarial.unclosed_count == 0,
            evidence=json_digest(bundle.adversarial.model_dump(mode="json")),
        )
        shortboards.extend(bundle.adversarial.shortboards)
    numeric = [
        entry.value
        for key, entry in metrics.items()
        if entry.status == "evaluated" and entry.value is not None
    ]
    risk = BoundRisk(
        tier=bundle.risk.tier, lower_bound=bundle.risk.lower_bound, declared=bundle.risk.declared
    )
    return MetricsDocument.of(
        risk=risk,
        change_id=bundle.change_id,
        cadence=bundle.cadence,
        computed_at=bundle.computed_at,
        metrics=metrics,
        policy_digest=bundle.policy_digest,
        shortboards=tuple(shortboards),
        floor_ratio=min(1.0, min(numeric)) if numeric else None,
    )


def compute_assertion_strength(payload: AssertionStrengthInput) -> AssertionStrengthEvidence:
    if {surface.layer for surface in payload.surfaces} != {"api", "e2e"}:
        return AssertionStrengthEvidence(
            schema_version="1",
            change_id=payload.change_id,
            batch_id=payload.batch_id,
            status="not_evaluated",
            shortboards=(MetricShortboard(code="pending_nightly", metric="assertion_strength"),),
            source=dict(payload.source),
        )
    total = sum(surface.declared.total for surface in payload.surfaces)
    covered = sum(surface.declared.covered for surface in payload.surfaces)
    return AssertionStrengthEvidence(
        schema_version="1",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        status="evaluated",
        value=covered / total,
        declared=scope_of(total=total, covered=covered),
        surfaces=payload.surfaces,
        source=dict(payload.source),
    )


def compute_baseline_drift(payload: BaselineDriftInput) -> BaselineDriftEvidence:
    if not payload.scenarios:
        return BaselineDriftEvidence(
            schema_version="1",
            change_id=payload.change_id,
            batch_id=payload.batch_id,
            status="not_evaluated",
            drift_band=payload.drift_band,
            window=payload.window,
            shortboards=(MetricShortboard(code="sample_insufficient", metric="baseline_drift"),),
            source=dict(payload.source),
        )
    return BaselineDriftEvidence(
        schema_version="1",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        status="evaluated",
        value=max(item.drift for item in payload.scenarios),
        drift_band=payload.drift_band,
        window=payload.window,
        scenarios=payload.scenarios,
        source=dict(payload.source),
    )


def collect_adversarial_yield(payload: AdversarialYieldInput) -> AdversarialYieldEvidence:
    ids = tuple(sorted(set(payload.counterexample_ids)))
    return AdversarialYieldEvidence(
        schema_version="1",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        property=payload.property,
        layer=payload.layer,
        sample_count=payload.sample_count,
        counterexample_ids=ids,
        unclosed_count=payload.unclosed_count,
        seed=payload.seed,
        status="evaluated",
        value=float(len(ids)),
        source=dict(payload.source),
    )


def load_latest_pr_metrics(payload: LatestMetricsInput) -> MetricsDocument:
    documents = [
        item
        for item in payload.documents
        if item.cadence == "pr" and (payload.change_id is None or item.change_id == payload.change_id)
    ]
    if not documents:
        raise InputError("no PR metrics documents in the supplied catalog")
    return max(documents, key=lambda item: item.computed_at)


class _ModelHandler:
    input_model: type[BaseModel]
    builder: object

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(self.input_model, request.input)
            result = self.builder(payload)  # type: ignore[operator]
            if hasattr(result, "model_dump"):
                return TaskOutcome.succeeded(cast(JSONValue, result.model_dump(mode="json")))
            return succeeded(cast(Mapping[str, object], result))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
        except ValidationError as error:
            return failed_input(error)


class CollectPrMetricsBatchHandler(_ModelHandler):
    input_model = PrEvidenceBundle

    @staticmethod
    def builder(payload: PrEvidenceBundle) -> dict[str, object]:
        parts = {
            name: getattr(payload, name).model_dump(mode="json")
            for name in ("diff", "constraint", "auth", "journey", "slack")
            if getattr(payload, name) is not None
        }
        return {
            "change_id": payload.change_id,
            "batch_id": payload.batch_id,
            "source_digests": {name: json_digest(value) for name, value in parts.items()},
            "evidence": parts,
        }

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class MaterializePrMetricsHandler(_ModelHandler):
    input_model = PrEvidenceBundle
    builder = staticmethod(build_metrics_document)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class LoadLatestPrMetricsHandler(_ModelHandler):
    input_model = LatestMetricsInput
    builder = staticmethod(load_latest_pr_metrics)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class RunMutationSampleHandler:
    def __init__(self, host: MutationSampleHost | None = None) -> None:
        self._host = host or ClosedMutationHost()

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(MutationSampleInput, request.input)
            evidence = self._host.sample(payload)
            return TaskOutcome.succeeded(cast(JSONValue, evidence.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except ValidationError as error:
            return failed_input(error)


class ComputeAssertionStrengthHandler(_ModelHandler):
    input_model = AssertionStrengthInput
    builder = staticmethod(compute_assertion_strength)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class ComputeBaselineDriftHandler(_ModelHandler):
    input_model = BaselineDriftInput
    builder = staticmethod(compute_baseline_drift)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class CollectAdversarialYieldHandler(_ModelHandler):
    input_model = AdversarialYieldInput
    builder = staticmethod(collect_adversarial_yield)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


def metric_keys() -> tuple[MetricKey, ...]:
    return METRIC_KEYS
