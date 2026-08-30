from __future__ import annotations

import json

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    ResourceContribution,
    SchemaContribution,
)

from assurance_quality.operations import quality_handlers
from assurance_quality.resource_loader import resource_bytes
from assurance_quality.validators.issues import IssueValidator, ProblemApplyValidator
from assurance_quality.validators.metrics import CrossArtifactValidator, MetricsValidator
from assurance_quality.validators.report import ReportValidator
from assurance_quality.validators.trace import TraceValidator

QUALITY_SOURCE = ProviderSource(
    distribution="assurance-quality",
    version="0.2.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="quality",
    entrypoint_value="assurance_quality.plugin:QualityPlugin",
    declaration_path="assurance_quality/plugin-declaration.json",
    import_roots=("",),
)

QUALITY_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.2.0"),
    PluginDependency("assurance.generation", "==0.2.0"),
    PluginDependency("assurance.execution", "==0.2.0"),
    PluginDependency("assurance.healing", "==0.2.0"),
)

QUALITY_HANDLER_IDS: tuple[str, ...] = (
    "assurance.quality.aggregate-nightly-metrics",
    "assurance.quality.apply-problem-review",
    "assurance.quality.build-coverage-gap-signals",
    "assurance.quality.collect-adversarial-yield",
    "assurance.quality.collect-diff-coverage",
    "assurance.quality.collect-observations",
    "assurance.quality.collect-pr-metrics-batch",
    "assurance.quality.compute-assertion-strength",
    "assurance.quality.compute-auth-matrix",
    "assurance.quality.compute-baseline-drift",
    "assurance.quality.compute-constraint-coverage",
    "assurance.quality.compute-journey-coverage",
    "assurance.quality.compute-threshold-slack",
    "assurance.quality.dashboard",
    "assurance.quality.derive-plan-layer-applicability",
    "assurance.quality.evaluate-retrospective-shortboards",
    "assurance.quality.fact-baseline.finalize",
    "assurance.quality.fact-baseline.prepare",
    "assurance.quality.generate-report",
    "assurance.quality.inspect",
    "assurance.quality.inspect.finalize",
    "assurance.quality.inspect.prepare",
    "assurance.quality.issue-analysis.finalize",
    "assurance.quality.issue-analysis.prepare",
    "assurance.quality.issue-triage.finalize",
    "assurance.quality.issue-triage.prepare",
    "assurance.quality.load-latest-pr-metrics",
    "assurance.quality.load-problem-review-context",
    "assurance.quality.materialize-c-layer-metrics",
    "assurance.quality.materialize-minimum-coverage",
    "assurance.quality.materialize-pr-metrics",
    "assurance.quality.materialize-quarantine-projection",
    "assurance.quality.materialize-trace-and-coverage-gaps",
    "assurance.quality.materialize-trace-projection",
    "assurance.quality.probe-coverage-repair-need",
    "assurance.quality.reconcile-issues",
    "assurance.quality.record-empty-issue-analysis",
    "assurance.quality.record-issue-analysis-failure",
    "assurance.quality.record-project-sync-pending",
    "assurance.quality.report.finalize",
    "assurance.quality.report.prepare",
    "assurance.quality.run-mutation-sample",
    "assurance.quality.run-nightly-metrics-pipeline",
)

QUALITY_VALIDATOR_IDS: tuple[str, ...] = (
    "assurance.quality.validator.cross-artifact.v1",
    "assurance.quality.validator.issues.v1",
    "assurance.quality.validator.metrics.v1",
    "assurance.quality.validator.problem-apply.v1",
    "assurance.quality.validator.report.v1",
    "assurance.quality.validator.trace.v2",
)

QUALITY_RESOURCE_FILES: dict[str, str] = {
    "assurance.quality.persona.explorer.v1": "personas/explorer.md",
    "assurance.quality.persona.reporter.v1": "personas/reporter.md",
    "assurance.quality.persona.reviewer.v1": "personas/reviewer.md",
    "assurance.quality.result.fact-baseline.v1": "result-contracts/fact-baseline.v1.schema.json",
    "assurance.quality.result.inspection.v1": "result-contracts/inspection.v1.schema.json",
    "assurance.quality.result.issue-analysis.v1": "result-contracts/issue-analysis.v1.schema.json",
    "assurance.quality.result.issue-triage.v1": "result-contracts/issue-triage.v1.schema.json",
    "assurance.quality.result.report.v1": "result-contracts/report.v1.schema.json",
    "assurance.quality.skill.aa-dashboard.v1": "skills/aa-dashboard/SKILL.md",
    "assurance.quality.skill.aa-fact-baseline.v1": "skills/aa-fact-baseline/SKILL.md",
    "assurance.quality.skill.aa-inspect.v1": "skills/aa-inspect/SKILL.md",
    "assurance.quality.skill.aa-issue-analyzer.v1": "skills/aa-issue-analyzer/SKILL.md",
    "assurance.quality.skill.aa-issue-triage-advisor.v1": "skills/aa-issue-triage-advisor/SKILL.md",
    "assurance.quality.skill.aa-report-generator.v1": "skills/aa-report-generator/SKILL.md",
    "assurance.quality.workflow.module.v1": "workflow/module.yaml",
}

QUALITY_RESOURCE_IDS: tuple[str, ...] = tuple(sorted(QUALITY_RESOURCE_FILES))

QUALITY_SCHEMA_IDS: tuple[str, ...] = (
    "assurance.quality.schema.adversarial-yield.v1",
    "assurance.quality.schema.assertion-strength.v1",
    "assurance.quality.schema.auth-matrix.v1",
    "assurance.quality.schema.baseline-drift.v1",
    "assurance.quality.schema.c-layer.v1",
    "assurance.quality.schema.constraint-coverage.v1",
    "assurance.quality.schema.coverage-diff.v1",
    "assurance.quality.schema.coverage-gaps.v1",
    "assurance.quality.schema.fact-baseline.v1",
    "assurance.quality.schema.issue-events.v1",
    "assurance.quality.schema.issues.v1",
    "assurance.quality.schema.journey-coverage.v1",
    "assurance.quality.schema.metrics.v1",
    "assurance.quality.schema.minimum-coverage.v1",
    "assurance.quality.schema.mutation.v1",
    "assurance.quality.schema.perf-slack.v1",
    "assurance.quality.schema.quality-gate.v2",
    "assurance.quality.schema.quarantine.v1",
    "assurance.quality.schema.report.v1",
    "assurance.quality.schema.sufficiency.v2",
    "assurance.quality.schema.trace-sufficiency.v1",
    "assurance.quality.schema.trace.v2",
    "assurance.quality.workflow.assess.input.v1",
    "assurance.quality.workflow.assess.output.v1",
    "assurance.quality.workflow.issue-analyze.input.v1",
    "assurance.quality.workflow.issue-analyze.output.v1",
    "assurance.quality.workflow.issue-reconcile.input.v1",
    "assurance.quality.workflow.issue-reconcile.output.v1",
    "assurance.quality.workflow.issue-review.input.v1",
    "assurance.quality.workflow.issue-review.output.v1",
    "assurance.quality.workflow.report.input.v1",
    "assurance.quality.workflow.report.output.v1",
)

_SCHEMA_FILES: dict[str, str] = {
    "assurance.quality.schema.adversarial-yield.v1": "schemas/adversarial-yield.v1.schema.json",
    "assurance.quality.schema.assertion-strength.v1": "schemas/assertion-strength.v1.schema.json",
    "assurance.quality.schema.auth-matrix.v1": "schemas/auth-matrix.v1.schema.json",
    "assurance.quality.schema.baseline-drift.v1": "schemas/baseline-drift.v1.schema.json",
    "assurance.quality.schema.c-layer.v1": "schemas/c-layer.v1.schema.json",
    "assurance.quality.schema.constraint-coverage.v1": "schemas/constraint-coverage.v1.schema.json",
    "assurance.quality.schema.coverage-diff.v1": "schemas/coverage-diff.v1.schema.json",
    "assurance.quality.schema.coverage-gaps.v1": "schemas/coverage-gaps.v1.schema.json",
    "assurance.quality.schema.fact-baseline.v1": "schemas/fact-baseline.v1.schema.json",
    "assurance.quality.schema.issue-events.v1": "schemas/issue-events.v1.schema.json",
    "assurance.quality.schema.issues.v1": "schemas/issues.v1.schema.json",
    "assurance.quality.schema.journey-coverage.v1": "schemas/journey-coverage.v1.schema.json",
    "assurance.quality.schema.metrics.v1": "schemas/metrics.v1.schema.json",
    "assurance.quality.schema.minimum-coverage.v1": "schemas/minimum-coverage.v1.schema.json",
    "assurance.quality.schema.mutation.v1": "schemas/mutation.v1.schema.json",
    "assurance.quality.schema.perf-slack.v1": "schemas/perf-slack.v1.schema.json",
    "assurance.quality.schema.quality-gate.v2": "schemas/quality-gate.v2.schema.json",
    "assurance.quality.schema.quarantine.v1": "schemas/quarantine.v1.schema.json",
    "assurance.quality.schema.report.v1": "schemas/report.v1.schema.json",
    "assurance.quality.schema.sufficiency.v2": "schemas/sufficiency.v2.schema.json",
    "assurance.quality.schema.trace-sufficiency.v1": "schemas/trace-sufficiency.v1.schema.json",
    "assurance.quality.schema.trace.v2": "schemas/trace.v2.schema.json",
    "assurance.quality.workflow.assess.input.v1": "schemas/workflow/assess-input.v1.schema.json",
    "assurance.quality.workflow.assess.output.v1": "schemas/workflow/assess-output.v1.schema.json",
    "assurance.quality.workflow.issue-analyze.input.v1": (
        "schemas/workflow/issue-analyze-input.v1.schema.json"
    ),
    "assurance.quality.workflow.issue-analyze.output.v1": (
        "schemas/workflow/issue-analyze-output.v1.schema.json"
    ),
    "assurance.quality.workflow.issue-reconcile.input.v1": (
        "schemas/workflow/issue-reconcile-input.v1.schema.json"
    ),
    "assurance.quality.workflow.issue-reconcile.output.v1": (
        "schemas/workflow/issue-reconcile-output.v1.schema.json"
    ),
    "assurance.quality.workflow.issue-review.input.v1": (
        "schemas/workflow/issue-review-input.v1.schema.json"
    ),
    "assurance.quality.workflow.issue-review.output.v1": (
        "schemas/workflow/issue-review-output.v1.schema.json"
    ),
    "assurance.quality.workflow.report.input.v1": "schemas/workflow/report-input.v1.schema.json",
    "assurance.quality.workflow.report.output.v1": "schemas/workflow/report-output.v1.schema.json",
}


_VALIDATORS = {
    "assurance.quality.validator.cross-artifact.v1": CrossArtifactValidator(path_only=True),
    "assurance.quality.validator.issues.v1": IssueValidator(path_only=True),
    "assurance.quality.validator.metrics.v1": MetricsValidator(path_only=True),
    "assurance.quality.validator.problem-apply.v1": ProblemApplyValidator(path_only=True),
    "assurance.quality.validator.report.v1": ReportValidator(path_only=True),
    "assurance.quality.validator.trace.v2": TraceValidator(path_only=True),
}


def _schema_contributions() -> tuple[SchemaContribution, ...]:
    return tuple(
        SchemaContribution(
            schema_id=schema_id,
            media_type="application/schema+json",
            content=canonical_json_bytes(json.loads(resource_bytes(_SCHEMA_FILES[schema_id]))),
        )
        for schema_id in QUALITY_SCHEMA_IDS
    )


_WORKFLOW_MODULE_MIME = "application/vnd.graph-engine.workflow-module+yaml"


def _resource_media_type(path: str) -> str:
    if path.endswith(".schema.json"):
        return "application/schema+json"
    if path.endswith("workflow/module.yaml"):
        return _WORKFLOW_MODULE_MIME
    if path.endswith(".json"):
        return "application/json"
    return "text/plain"


def _resource_contributions() -> tuple[ResourceContribution, ...]:
    return tuple(
        ResourceContribution(
            resource_id=resource_id,
            media_type=_resource_media_type(QUALITY_RESOURCE_FILES[resource_id]),
            content=resource_bytes(QUALITY_RESOURCE_FILES[resource_id]),
        )
        for resource_id in QUALITY_RESOURCE_IDS
    )


class QualityPlugin:
    @staticmethod
    def descriptor() -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=QUALITY_SOURCE,
            plugin_id="assurance.quality",
            plugin_version="0.2.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=QUALITY_HANDLER_IDS,
            commit_validators=QUALITY_VALIDATOR_IDS,
            dependencies=QUALITY_DEPENDENCIES,
            schemas=QUALITY_SCHEMA_IDS,
            resources=QUALITY_RESOURCE_IDS,
            effects=(),
            bindings=(),
        )

    @staticmethod
    def contribute(ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != ENGINE_API_VERSION:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers=dict(quality_handlers()),
            commit_validators=dict(sorted(_VALIDATORS.items())),
            schemas=_schema_contributions(),
            resources=_resource_contributions(),
        )
