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
from assurance_quality.ops import router
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
    **router.resource_files(),
    "assurance.quality.skill.aa-dashboard.v1": "skills/aa-dashboard/SKILL.md",
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
}

_GENERATED_SCHEMAS: dict[str, str] = {
    "assurance.quality.schema.adversarial-yield.v1": "assurance_quality.contracts:AdversarialYieldEvidence",
    "assurance.quality.schema.assertion-strength.v1": (
        "assurance_quality.contracts:AssertionStrengthEvidence"
    ),
    "assurance.quality.schema.auth-matrix.v1": "assurance_quality.contracts:AuthMatrixEvidence",
    "assurance.quality.schema.baseline-drift.v1": "assurance_quality.contracts:BaselineDriftEvidence",
    "assurance.quality.schema.c-layer.v1": "assurance_quality.contracts:CLayerMetricsDocument",
    "assurance.quality.schema.constraint-coverage.v1": (
        "assurance_quality.contracts:ConstraintCoverageEvidence"
    ),
    "assurance.quality.schema.coverage-diff.v1": "assurance_quality.contracts:CoverageDiffEvidence",
    "assurance.quality.schema.coverage-gaps.v1": "assurance_quality.contracts:CoverageGapsDocument",
    "assurance.quality.schema.fact-baseline.v1": "assurance_quality.contracts:FactBaselineAuthoring",
    "assurance.quality.schema.issue-events.v1": (
        "assurance_quality.contracts.issue_events:CHANGE_ISSUE_EVENT_ADAPTER"
    ),
    "assurance.quality.schema.issues.v1": "assurance_quality.contracts:ChangeIssueSnapshot",
    "assurance.quality.schema.journey-coverage.v1": "assurance_quality.contracts:JourneyCoverageEvidence",
    "assurance.quality.schema.metrics.v1": "assurance_quality.contracts:MetricsDocument",
    "assurance.quality.schema.minimum-coverage.v1": "assurance_quality.contracts:MinimumCoverageResult",
    "assurance.quality.schema.mutation.v1": "assurance_quality.contracts:MutationEvidence",
    "assurance.quality.schema.obligation-assessment.v1": (
        "assurance_quality.contracts.obligations:ObligationAssessmentV1"
    ),
    "assurance.quality.schema.perf-slack.v1": "assurance_quality.contracts:PerfSlackEvidence",
    "assurance.quality.schema.quality-gate.v2": "assurance_quality.contracts:QualityGateResultV2",
    "assurance.quality.schema.quarantine.v1": "assurance_quality.contracts:QuarantineProjection",
    "assurance.quality.schema.report.v1": "assurance_quality.contracts:QualityReport",
    "assurance.quality.schema.sufficiency.v2": "assurance_quality.contracts:SufficiencyReportV2",
    "assurance.quality.schema.trace-sufficiency.v1": "assurance_quality.contracts:TraceSufficiencyFacts",
    "assurance.quality.schema.trace.v2": "assurance_quality.contracts:TraceProjectionV2",
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
    generated_schemas = _GENERATED_SCHEMAS
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
