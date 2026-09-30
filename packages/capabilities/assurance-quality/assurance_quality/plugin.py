from __future__ import annotations

from dataclasses import replace

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import (
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
)
from graph_engine.plugin_kit import CapabilityPlugin, CapabilitySpec

from assurance_quality.contracts.attempts import attempt_contract_refs
from assurance_quality.operations import quality_handlers
from assurance_quality.resource_loader import resource_bytes
from assurance_quality.validators.issues import IssueValidator, ProblemApplyValidator
from assurance_quality.validators.metrics import CrossArtifactValidator, MetricsValidator
from assurance_quality.validators.report import ReportValidator
from assurance_quality.validators.trace import TraceValidator

QUALITY_SOURCE = ProviderSource(
    distribution="assurance-quality",
    version="0.3.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="quality",
    entrypoint_value="assurance_quality.plugin:QualityPlugin",
    declaration_path="assurance_quality/plugin-declaration.json",
    import_roots=("",),
)

QUALITY_DEPENDENCIES: tuple[PluginDependency, ...] = (
    PluginDependency("assurance.intake", "==0.3.0"),
    PluginDependency("assurance.generation", "==0.3.0"),
    PluginDependency("assurance.execution", "==0.3.0"),
    PluginDependency("assurance.healing", "==0.3.0"),
)

QUALITY_RESOURCE_FILES: dict[str, str] = {
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
}

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
    "assurance.quality.schema.obligation-assessment.v1": "schemas/obligation-assessment.v1.schema.json",
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


_HANDLERS = quality_handlers()

_VALIDATORS = {
    "assurance.quality.validator.cross-artifact.v1": CrossArtifactValidator(path_only=True),
    "assurance.quality.validator.issues.v1": IssueValidator(path_only=True),
    "assurance.quality.validator.metrics.v1": MetricsValidator(path_only=True),
    "assurance.quality.validator.problem-apply.v1": ProblemApplyValidator(path_only=True),
    "assurance.quality.validator.report.v1": ReportValidator(path_only=True),
    "assurance.quality.validator.trace.v2": TraceValidator(path_only=True),
}


class QualityPlugin(CapabilityPlugin):
    spec = CapabilitySpec(
        plugin_id="assurance.quality",
        version="0.3.0",
        engine_api=ENGINE_API_VERSION,
        source=QUALITY_SOURCE,
        resource_bytes=resource_bytes,
        schema_files=_SCHEMA_FILES,
        resource_files=QUALITY_RESOURCE_FILES,
        task_handlers=_HANDLERS,
        commit_validators=_VALIDATORS,
        dependencies=QUALITY_DEPENDENCIES,
    )

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor().model_copy(update={"attempt_contracts": attempt_contract_refs()})

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return replace(cls.spec.contribution(ports), attempt_contracts=attempt_contract_refs())
