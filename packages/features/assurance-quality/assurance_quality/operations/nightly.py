"""Nightly aggregation, shortboards, and pipeline composition."""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, ConfigDict, ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.metrics import MetricShortboard, MetricsDocument
from assurance_quality.operations.common import InputError, failed_input, validate_input
from assurance_quality.operations.metrics import (
    AdversarialYieldInput,
    AssertionStrengthInput,
    BaselineDriftInput,
    ClosedMutationHost,
    MutationSampleHost,
    MutationSampleInput,
    PrEvidenceBundle,
    build_metrics_document,
    collect_adversarial_yield,
    compute_assertion_strength,
    compute_baseline_drift,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class NightlyAggregateInput(PrEvidenceBundle):
    pass


class ShortboardInput(BaseModel):
    model_config = _FROZEN

    metrics: MetricsDocument


class NightlyPipelineInput(PrEvidenceBundle):
    mutation_sample: MutationSampleInput | None = None
    assertion_input: AssertionStrengthInput | None = None
    drift_input: BaselineDriftInput | None = None
    adversarial_input: AdversarialYieldInput | None = None


def evaluate_shortboards(document: MetricsDocument) -> tuple[MetricShortboard, ...]:
    boards = list(document.shortboards)
    for key, entry in document.metrics.items():
        if entry.status == "not_evaluated" and not any(
            board.metric == key and board.code == "pending_nightly" for board in boards
        ):
            boards.append(MetricShortboard(code="pending_nightly", metric=key))
        if (
            entry.status == "evaluated"
            and entry.value is not None
            and entry.value < 1.0
            and not any(board.metric == key and board.code == "below_floor" for board in boards)
        ):
            boards.append(MetricShortboard(code="below_floor", metric=key, detail=f"{entry.value}"))
    return tuple(boards)


def run_nightly_pipeline(
    payload: NightlyPipelineInput,
    *,
    mutation_host: MutationSampleHost | None = None,
) -> dict[str, object]:
    host = mutation_host or ClosedMutationHost()
    mutation = payload.mutation or (
        host.sample(payload.mutation_sample) if payload.mutation_sample is not None else None
    )
    assertion = payload.assertion or (
        compute_assertion_strength(payload.assertion_input) if payload.assertion_input is not None else None
    )
    drift = payload.drift or (
        compute_baseline_drift(payload.drift_input) if payload.drift_input is not None else None
    )
    adversarial = payload.adversarial or (
        collect_adversarial_yield(payload.adversarial_input)
        if payload.adversarial_input is not None
        else None
    )
    bundle = PrEvidenceBundle(
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        computed_at=payload.computed_at,
        policy_digest=payload.policy_digest,
        risk=payload.risk,
        cadence="nightly",
        diff=payload.diff,
        constraint=payload.constraint,
        auth=payload.auth,
        journey=payload.journey,
        slack=payload.slack,
        mutation=mutation,
        assertion=assertion,
        drift=drift,
        adversarial=adversarial,
    )
    document = build_metrics_document(bundle)
    return {
        "metrics": document.model_dump(mode="json"),
        "mutation": None if mutation is None else mutation.model_dump(mode="json"),
        "assertion": None if assertion is None else assertion.model_dump(mode="json"),
        "drift": None if drift is None else drift.model_dump(mode="json"),
        "adversarial": None if adversarial is None else adversarial.model_dump(mode="json"),
        "shortboards": [board.model_dump(mode="json") for board in evaluate_shortboards(document)],
    }


class AggregateNightlyMetricsHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(NightlyAggregateInput, request.input)
            rebuilt = payload.model_copy(update={"cadence": "nightly"})
            document = build_metrics_document(rebuilt)
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except ValidationError as error:
            return failed_input(error)


class EvaluateRetrospectiveShortboardsHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(ShortboardInput, request.input)
            boards = evaluate_shortboards(payload.metrics)
            return TaskOutcome.succeeded(
                cast(JSONValue, {"shortboards": [board.model_dump(mode="json") for board in boards]})
            )
        except InputError as error:
            return failed_input(error)
        except ValidationError as error:
            return failed_input(error)


class RunNightlyMetricsPipelineHandler:
    def __init__(self, mutation_host: MutationSampleHost | None = None) -> None:
        self._host = mutation_host or ClosedMutationHost()

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(NightlyPipelineInput, request.input)
            return TaskOutcome.succeeded(
                cast(JSONValue, run_nightly_pipeline(payload, mutation_host=self._host))
            )
        except InputError as error:
            return failed_input(error)
        except ValidationError as error:
            return failed_input(error)
