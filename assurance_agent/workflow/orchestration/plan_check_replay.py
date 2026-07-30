"""Raw-byte-bound replay of wired plan-review gate policy branches."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

import yaml
from pydantic import BaseModel

from assurance_agent.artifacts.models.data_knowledge import DataKnowledge
from assurance_agent.artifacts.models.plan_checks import PlanCheckDocument
from assurance_agent.artifacts.models.policy import PlanCheckAction, Policy
from assurance_agent.artifacts.models.review import PlanReview
from assurance_agent.artifacts.policy import normalized_policy_bytes, policy_digest
from assurance_agent.knowledge.capabilities import plan_review_route
from assurance_agent.verification.profiles import LayerAssuranceProfile
from assurance_agent.workflow.orchestration.gates import (
    FrozenGateReport,
    GateEvaluationContext,
    check_gate_in_view,
)
from assurance_agent.workflow.orchestration.schema import GateDef

T = TypeVar("T")

PolicyEffect = Literal[
    "applied",
    "no_failed_checks",
    "shadowed_by_capability_precondition",
    "shadowed_by_gate_precondition",
]

_SCENARIO_ACTIONS: tuple[PlanCheckAction, ...] = ("warn", "block", "require_human")


@dataclass(frozen=True, slots=True)
class BoundArtifact(Generic[T]):
    logical_path: str
    raw_bytes: bytes
    sha256: str
    model: T

    def __post_init__(self) -> None:
        digest = hashlib.sha256(self.raw_bytes).hexdigest()
        if digest != self.sha256:
            raise ValueError(
                f"sha256 mismatch for {self.logical_path}: expected {self.sha256}, got {digest}"
            )


@dataclass(frozen=True, slots=True)
class PolicyScenario:
    action: PlanCheckAction
    policy_digest: str
    verdict: str
    route: str
    matched_rule: str | None
    reason: str
    missing_capabilities: list[str]
    policy_effect: PolicyEffect


@dataclass(frozen=True, slots=True)
class LayerPolicyReplay:
    gate_id: str
    baseline: FrozenGateReport
    scenarios: tuple[PolicyScenario, ...]


def bind_json_artifact(
    *,
    logical_path: str,
    model: BaseModel,
    raw_bytes: bytes | None = None,
) -> BoundArtifact[BaseModel]:
    payload = raw_bytes
    if payload is None:
        payload = (json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    else:
        reparsed = type(model).model_validate(json.loads(payload))
        if reparsed != model:
            raise ValueError(f"model does not match raw_bytes for {logical_path}")
    digest = hashlib.sha256(payload).hexdigest()
    return BoundArtifact(logical_path=logical_path, raw_bytes=payload, sha256=digest, model=model)


def bind_yaml_artifact(
    *,
    logical_path: str,
    model: BaseModel,
    raw_bytes: bytes | None = None,
) -> BoundArtifact[BaseModel]:
    payload = raw_bytes
    if payload is None:
        payload = (
            yaml.safe_dump(model.model_dump(mode="json"), sort_keys=False, allow_unicode=True).encode(
                "utf-8"
            )
        )
    else:
        reparsed = type(model).model_validate(yaml.safe_load(payload))
        if reparsed != model:
            raise ValueError(f"model does not match raw_bytes for {logical_path}")
    digest = hashlib.sha256(payload).hexdigest()
    return BoundArtifact(logical_path=logical_path, raw_bytes=payload, sha256=digest, model=model)


def evaluate_bound_plan_gate(
    *,
    gates: Mapping[str, GateDef],
    profile: LayerAssuranceProfile,
    review: BoundArtifact[PlanReview] | None,
    checks: BoundArtifact[PlanCheckDocument],
    data_knowledge: BoundArtifact[DataKnowledge] | None,
    policy: Policy,
    change_id: str,
    params: Mapping[str, object],
) -> FrozenGateReport:
    with _staged_replay_tree(
        profile=profile,
        review=review,
        checks=checks,
        data_knowledge=data_knowledge,
        policy=policy,
        change_id=change_id,
        params=params,
    ) as context:
        return check_gate_in_view(gates, profile.gate_id, context)


def replay_plan_check_policy(
    *,
    gates: Mapping[str, GateDef],
    profile: LayerAssuranceProfile,
    review: BoundArtifact[PlanReview] | None,
    checks: BoundArtifact[PlanCheckDocument],
    data_knowledge: BoundArtifact[DataKnowledge] | None,
    base_policy: Policy,
    change_id: str,
    params: Mapping[str, object],
) -> LayerPolicyReplay:
    baseline = evaluate_bound_plan_gate(
        gates=gates,
        profile=profile,
        review=review,
        checks=checks,
        data_knowledge=data_knowledge,
        policy=base_policy,
        change_id=change_id,
        params=params,
    )
    scenarios: list[PolicyScenario] = []
    for action in _SCENARIO_ACTIONS:
        scenario_policy = base_policy.model_copy(
            update={"plan_checks": dict.fromkeys(base_policy.plan_checks, action)}
        )
        report = evaluate_bound_plan_gate(
            gates=gates,
            profile=profile,
            review=review,
            checks=checks,
            data_knowledge=data_knowledge,
            policy=scenario_policy,
            change_id=change_id,
            params=params,
        )
        scenarios.append(
            _scenario_from_report(
                action=action,
                policy=scenario_policy,
                report=report,
                checks=checks.model,
                profile=profile,
                review=review.model if review is not None else None,
            )
        )
    return LayerPolicyReplay(
        gate_id=profile.gate_id,
        baseline=baseline,
        scenarios=tuple(scenarios),
    )


def _scenario_from_report(
    *,
    action: PlanCheckAction,
    policy: Policy,
    report: FrozenGateReport,
    checks: PlanCheckDocument,
    profile: LayerAssuranceProfile,
    review: PlanReview | None,
) -> PolicyScenario:
    details = report.details if isinstance(report.details, dict) else {}
    missing_raw = details.get("missing_capabilities")
    missing = [str(item) for item in missing_raw] if isinstance(missing_raw, list) else []
    return PolicyScenario(
        action=action,
        policy_digest=policy_digest(policy),
        verdict=report.verdict.value,
        route=_route_from_report(report),
        matched_rule=report.matched_rule,
        reason=report.reason,
        missing_capabilities=missing,
        policy_effect=_classify_policy_effect(
            report=report,
            checks=checks,
            profile=profile,
            action=action,
            review=review,
            policy=policy,
        ),
    )


def _route_from_report(report: FrozenGateReport) -> str:
    return plan_review_route(
        {
            "gate": {
                "verdict": report.verdict.value,
                "matched_rule": report.matched_rule,
                "reason": report.reason,
                "details": report.details,
            }
        }
    )


def _classify_policy_effect(
    *,
    report: FrozenGateReport,
    checks: PlanCheckDocument,
    profile: LayerAssuranceProfile,
    action: PlanCheckAction,
    review: PlanReview | None,
    policy: Policy,
) -> PolicyEffect:
    matched = report.matched_rule or ""
    field = matched.split(":", 1)[0] if matched else ""
    has_failed = _has_failed_applicable_checks(checks, profile)
    details = report.details if isinstance(report.details, dict) else {}
    missing_raw = details.get("missing_capabilities")
    missing_capabilities = (
        [str(item) for item in missing_raw] if isinstance(missing_raw, list) else []
    )

    if field == "needs_human_review_when" and missing_capabilities:
        return "shadowed_by_capability_precondition"

    if field == "needs_fix_when":
        return "shadowed_by_gate_precondition"

    if field == "reject_when":
        if review is not None and review.decision == "reject":
            return "shadowed_by_gate_precondition"
        if review is not None and review.codegen_readiness == "not_ready":
            return "shadowed_by_gate_precondition"
        if has_failed and action == "block":
            return "applied"
        return "shadowed_by_gate_precondition"

    if field == "needs_human_review_when":
        if review is not None and review.decision == "needs_human_review":
            return "shadowed_by_gate_precondition"
        if review is not None and review.human_review_required is True:
            return "shadowed_by_gate_precondition"
        if (
            review is not None
            and review.risk_level is not None
            and review.risk_level in policy.human_review_risk_levels
        ):
            return "shadowed_by_gate_precondition"
        if has_failed and action == "require_human":
            return "applied"
        return "no_failed_checks" if not has_failed else "shadowed_by_gate_precondition"

    if field == "pass_when":
        if has_failed and action == "warn":
            return "applied"
        return "no_failed_checks"

    if not has_failed:
        return "no_failed_checks"

    return "applied"


def _has_failed_applicable_checks(
    checks: PlanCheckDocument,
    profile: LayerAssuranceProfile,
) -> bool:
    if checks.applicability is None or not checks.applicability.applicable:
        return False
    return any(
        check.status == "fail" and check.check_id in profile.applicable_check_ids
        for check in checks.checks
    )


class _StagedReplayTree:
    def __init__(
        self,
        *,
        profile: LayerAssuranceProfile,
        review: BoundArtifact[PlanReview] | None,
        checks: BoundArtifact[PlanCheckDocument],
        data_knowledge: BoundArtifact[DataKnowledge] | None,
        policy: Policy,
        change_id: str,
        params: Mapping[str, object],
    ) -> None:
        self._review = review
        self._checks = checks
        self._data_knowledge = data_knowledge
        self._policy = policy
        self._change_id = change_id
        self._params = dict(params)
        self._temp = tempfile.TemporaryDirectory(prefix="aa-plan-check-replay-")
        self.root = Path(self._temp.name)
        self.audit_dir = self.root / "empty-audit-events"
        self.audit_dir.mkdir()
        self._stage()

    def _stage(self) -> None:
        change_dir = self.root / "qa" / "changes" / self._change_id
        change_dir.mkdir(parents=True, exist_ok=True)

        policy_path = self.root / ".aa" / "policy.yaml"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_bytes(normalized_policy_bytes(self._policy))

        self._write_artifact(self._checks, change_dir=change_dir, project_root=self.root)
        if self._review is not None:
            self._write_artifact(self._review, change_dir=change_dir, project_root=self.root)
        if self._data_knowledge is not None:
            self._write_artifact(self._data_knowledge, change_dir=change_dir, project_root=self.root)

    @staticmethod
    def _write_artifact(
        artifact: BoundArtifact[Any],
        *,
        change_dir: Path,
        project_root: Path,
    ) -> None:
        if artifact.logical_path.startswith("repo:"):
            path = project_root / artifact.logical_path[len("repo:") :]
        elif artifact.logical_path.startswith("qa/"):
            path = project_root / artifact.logical_path
        else:
            path = change_dir / artifact.logical_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(artifact.raw_bytes)

    @property
    def context(self) -> GateEvaluationContext:
        change_dir = self.root / "qa" / "changes" / self._change_id
        return GateEvaluationContext(
            project_root=self.root,
            repo_root=self.root,
            change_dir=change_dir,
            change_id=self._change_id,
            params=self._params,
            state_values={},
            node_results={},
            artifact_overrides={},
            audit_events_dir=self.audit_dir,
        )

    def close(self) -> None:
        self._temp.cleanup()

    def __enter__(self) -> GateEvaluationContext:
        return self.context

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _staged_replay_tree(
    *,
    profile: LayerAssuranceProfile,
    review: BoundArtifact[PlanReview] | None,
    checks: BoundArtifact[PlanCheckDocument],
    data_knowledge: BoundArtifact[DataKnowledge] | None,
    policy: Policy,
    change_id: str,
    params: Mapping[str, object],
) -> _StagedReplayTree:
    return _StagedReplayTree(
        profile=profile,
        review=review,
        checks=checks,
        data_knowledge=data_knowledge,
        policy=policy,
        change_id=change_id,
        params=params,
    )
