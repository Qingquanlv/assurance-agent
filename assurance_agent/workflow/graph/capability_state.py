"""Process-global capability view: operation names, validator ids, and digest.

Operation *callables* live in ``workflow.driver.capability_catalog``. This module
is readable from ``workflow.graph`` without importing driver.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from assurance_agent.artifacts.registry import ArtifactSpec
from assurance_agent.exceptions import AaError

DEFAULT_OPERATION_NAMES: frozenset[str] = frozenset(
    {
        "operation:no-op",
        "operation:skill-registry-check",
        "operation:derive-plan-layer-applicability",
        "operation:run-tests",
        "operation:run-tests-and-collect-pr-metrics",
        "operation:allocate-healing-attempt",
        "operation:fixer-authority-ready",
        "operation:record-fixer-approval",
        "operation:fixer-dispatch",
        "operation:record-codegen-fix-apply",
        "operation:combine-fixer-safety",
        "operation:record-healing-status",
        "operation:probe-coverage-repair-need",
        "operation:compute-coverage-repair-safety",
        "operation:allocate-coverage-repair-attempt",
        "operation:record-coverage-repair-status",
        "operation:inspect",
        "operation:generate-report",
        "operation:stop",
        "operation:retro-collect-v3",
        "operation:assemble-retro-context-v3",
        "operation:drain-improvement-outbox",
        "operation:finalize-retro-status",
        "operation:record-retro-pipeline-failure",
        "operation:retro-evidence-gap-fallback",
        "operation:record-analysis-failed",
        "operation:materialize-empty-retro-analysis",
        "operation:reconcile-improvements",
        "operation:load-review-subject",
        "operation:validate-improvement-review-assessment",
        "operation:apply-improvement-auto-review",
        "operation:record-improvement-auto-review-error",
        "operation:record-auto-review-orchestration-error",
        "operation:select-current-retro-auto-review-items",
        "operation:summarize-auto-review-batch",
        "operation:retro-accept",
        "operation:collect-observations",
        "operation:record-empty-issue-analysis",
        "operation:record-issue-analysis-failure",
        "operation:record-project-sync-pending",
        "operation:reconcile-issues",
        "operation:materialize-trace-projection",
        "operation:load-problem-review-context",
        "operation:apply-problem-review",
        "operation:load-improvement-review-context",
        "operation:apply-improvement-review",
        "operation:load-improvement-delivery",
        "operation:evaluate-memory-improvement",
        "operation:apply-memory-improvement",
        "operation:rollback-memory-improvement",
        "operation:export-change-improvement",
        "operation:record-change-improvement-applied",
        "operation:export-knowledge-improvement",
        "operation:record-knowledge-improvement-applied",
        "operation:materialize-minimum-coverage",
        "operation:collect-diff-coverage",
        "operation:compute-constraint-coverage",
        "operation:compute-auth-matrix",
        "operation:compute-journey-coverage",
        "operation:compute-threshold-slack",
        "operation:collect-pr-metrics-batch",
        "operation:materialize-pr-metrics",
        "operation:load-latest-pr-metrics",
        "operation:run-mutation-sample",
        "operation:compute-assertion-strength",
        "operation:compute-baseline-drift",
        "operation:aggregate-nightly-metrics",
        "operation:evaluate-retrospective-shortboards",
        "operation:run-nightly-metrics-pipeline",
        "operation:collect-adversarial-yield",
        "operation:materialize-quarantine-projection",
        "operation:build-coverage-gap-signals",
        "operation:materialize-trace-and-coverage-gaps",
        "operation:materialize-c-layer-metrics",
    }
)


class CapabilityCatalogError(AaError):
    """Capability catalog registration or lookup failed."""


@dataclass(frozen=True, slots=True)
class CapabilityView:
    operation_names: frozenset[str]
    validator_ids: frozenset[str]
    digest: str


def compute_capability_catalog_digest(
    *,
    operations: Iterable[str],
    validators: Iterable[str],
    artifacts: Sequence[ArtifactSpec],
) -> str:
    payload = {
        "artifacts": [
            {
                "model": f"{spec.model.__module__}.{spec.model.__qualname__}",
                "pattern": spec.pattern,
            }
            for spec in sorted(artifacts, key=lambda item: item.pattern)
        ],
        "operations": sorted(operations),
        "validators": sorted(validators),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CapabilityCatalog:
    def __init__(self) -> None:
        self._operations: dict[str, None] = {}
        self._validators: dict[str, None] = {}
        self._artifacts: dict[str, ArtifactSpec] = {}
        self._frozen = False

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise CapabilityCatalogError("capability catalog is frozen")

    def register_operation(self, name: str) -> None:
        self._ensure_mutable()
        if name in self._operations:
            raise CapabilityCatalogError(f"duplicate operation: {name}")
        self._operations[name] = None

    def register_validator(self, validator_id: str) -> None:
        self._ensure_mutable()
        if validator_id in self._validators:
            raise CapabilityCatalogError(f"duplicate validator: {validator_id}")
        self._validators[validator_id] = None

    def register_artifact(self, spec: ArtifactSpec) -> None:
        self._ensure_mutable()
        if spec.pattern in self._artifacts:
            raise CapabilityCatalogError(f"duplicate artifact: {spec.pattern}")
        self._artifacts[spec.pattern] = spec

    def freeze(self) -> CapabilityView:
        self._frozen = True
        operations = frozenset(self._operations)
        validators = frozenset(self._validators)
        artifacts = tuple(self._artifacts[key] for key in sorted(self._artifacts))
        return CapabilityView(
            operation_names=operations,
            validator_ids=validators,
            digest=compute_capability_catalog_digest(
                operations=operations,
                validators=validators,
                artifacts=artifacts,
            ),
        )


_installed_view: CapabilityView | None = None


def install_capability_view(view: CapabilityView) -> None:
    global _installed_view
    _installed_view = view


def reset_capability_view() -> None:
    global _installed_view
    _installed_view = None


def current_capability_view() -> CapabilityView | None:
    return _installed_view


def current_validator_ids() -> frozenset[str]:
    if _installed_view is None:
        from assurance_agent.workflow.graph.precommit import KNOWN_PRECOMMIT_VALIDATORS

        return KNOWN_PRECOMMIT_VALIDATORS
    return _installed_view.validator_ids


def default_capability_catalog_digest() -> str:
    from assurance_agent.artifacts.registry import REGISTRY
    from assurance_agent.workflow.graph.precommit import KNOWN_PRECOMMIT_VALIDATORS

    return compute_capability_catalog_digest(
        operations=DEFAULT_OPERATION_NAMES,
        validators=KNOWN_PRECOMMIT_VALIDATORS,
        artifacts=tuple(REGISTRY),
    )


def assert_capability_catalog_compatible(pinned_digest: str) -> None:
    from assurance_agent.workflow.graph.runtime import CapabilityCatalogDrift

    current = current_capability_view()
    current_digest = current.digest if current is not None else default_capability_catalog_digest()
    if not pinned_digest:
        if current_digest != default_capability_catalog_digest():
            raise CapabilityCatalogDrift(
                "capability_catalog_digest drifted: ledger has an empty pin but the "
                "installed catalog is not the default"
            )
        return
    if pinned_digest != current_digest:
        raise CapabilityCatalogDrift(
            f"capability_catalog_digest drifted: pinned {pinned_digest!r} != installed {current_digest!r}"
        )
