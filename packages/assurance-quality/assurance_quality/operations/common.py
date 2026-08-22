"""Shared handler errors, catalog helpers, and outcome builders."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, cast

from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import TaskOutcome

from assurance_quality.contracts.metrics import MetricScope


class InputError(ValueError):
    """Malformed caller input or missing locked configuration."""


class OutputError(ValueError):
    """Model-authored semantic invalidity."""


def leafs_of(values: Iterable[str]) -> frozenset[str]:
    return frozenset(values)


def validate_input(model: type[Any], data: object) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def failed_input(error: Exception) -> TaskOutcome:
    return TaskOutcome.failed("invalid_input", str(error), retryable=False)


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)


def json_digest(value: Mapping[str, object] | JSONValue) -> str:
    return canonical_digest(cast(JSONValue, value))


def succeeded(payload: Mapping[str, object]) -> TaskOutcome:
    return TaskOutcome.succeeded(cast(JSONValue, dict(payload)))


def catalog_context(
    *,
    capability_leafs: Iterable[str],
    case_ids: Iterable[str] = (),
    plan_ids: Iterable[str] = (),
    test_ids: Iterable[str] = (),
    schema_ids: Iterable[str] = (),
    issue_ids: Iterable[str] = (),
    evidence_refs: Iterable[str] = (),
) -> dict[str, frozenset[str]]:
    context: dict[str, frozenset[str]] = {"capability_leafs": leafs_of(capability_leafs)}
    optional = {
        "case_ids": case_ids,
        "plan_ids": plan_ids,
        "test_ids": test_ids,
        "schema_ids": schema_ids,
        "issue_ids": issue_ids,
        "evidence_refs": evidence_refs,
    }
    for key, values in optional.items():
        frozen = leafs_of(values)
        if frozen:
            context[key] = frozen
    return context


def scope_of(*, total: int, covered: int, uncovered: tuple[str, ...] = ()) -> MetricScope:
    return MetricScope.of(total=total, covered=covered, uncovered=uncovered)
