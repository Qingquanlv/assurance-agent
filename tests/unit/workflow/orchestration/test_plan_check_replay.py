"""Raw-byte-bound plan-check policy replay against the real packaged gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.data_knowledge import DataKnowledge
from assurance_agent.artifacts.models.plan_checks import CheckEvidence, PlanCheckDocument
from assurance_agent.artifacts.models.policy import Policy
from assurance_agent.artifacts.models.review import PlanReview
from assurance_agent.artifacts.policy import load_policy, policy_digest
from assurance_agent.knowledge.capabilities import plan_review_route
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.profiles import get_layer_assurance_profile
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.gates import Verdict
from assurance_agent.workflow.orchestration.plan_check_replay import (
    BoundArtifact,
    bind_json_artifact,
    bind_yaml_artifact,
    evaluate_bound_plan_gate,
    replay_plan_check_policy,
)

_CHANGE_ID = "CH-REPLAY-001"
_EMPTY_DK: dict[str, object] = {
    "version": 1,
    "capabilities": {"domain_factories": {}},
}
_GATES = load_workflow_v2(Path.cwd()).gates


def _data_knowledge() -> DataKnowledge:
    return DataKnowledge.model_validate(
        {
            "version": 1,
            "auth": {"api_admin_token": {"method": "token", "symbol": "API_ADMIN_TOKEN"}},
            "capabilities": {
                "domain_factories": {},
                "adapters": {"api": {}, "e2e": {}, "fuzz": {}, "performance": {}},
                "cleanup": {},
            },
        }
    )


def _review(layer: str, **overrides: object) -> PlanReview:
    payload = {
        "schema_version": "1.0",
        "decision": "pass",
        "review_type": f"{layer}-plan",
        "change_id": _CHANGE_ID,
        "codegen_readiness": "ready",
        "required_capabilities": ["auth.api_admin_token"],
        "auto_fix_allowed": False,
        "human_review_required": False,
        "risk_level": "low",
        "findings": [],
        "auto_fix_plan": [],
        "next_action": "continue",
    }
    payload.update(overrides)
    return PlanReview.model_validate(payload)


def _applicable_checks(layer: str) -> PlanCheckDocument:
    profile = get_layer_assurance_profile(layer)
    case_id = f"TC_GATE_{profile.case_type.upper()}_001"
    cases = (
        {
            "added": [
                {
                    "case_id": case_id,
                    "title": "gate state",
                    "type": profile.case_type,
                    "automation": {"required": True},
                    "assertions": ["HTTP 200"],
                }
            ],
            "modified": [],
        },
    )
    main_plan = profile.plan_artifacts[0]
    case_table = f"| Case ID | Scenario | Expected |\n|---|---|---|\n| {case_id} | gate | HTTP 200 |\n"
    plan_texts = {path: (case_table if path == main_plan else "# Plan\n") for path in profile.plan_artifacts}
    return run_plan_checks(
        CheckContext(
            plan_texts=plan_texts,
            cases=cases,
            data_knowledge=_EMPTY_DK,
            layer=layer,  # type: ignore[arg-type]
        )
    )


def _inapplicable_checks(layer: str) -> PlanCheckDocument:
    return run_plan_checks(
        CheckContext(plan_texts={}, cases=(), data_knowledge=_EMPTY_DK, layer=layer)  # type: ignore[arg-type]
    )


def _failed_checks(layer: str, *check_ids: str) -> PlanCheckDocument:
    from assurance_agent.artifacts.models.plan_checks import Finding

    document = _applicable_checks(layer)
    checks = list(document.checks)
    for index, check in enumerate(checks):
        if check.check_id in check_ids:
            checks[index] = CheckEvidence(
                check_id=check.check_id,
                status="fail",
                findings=(Finding(locator=check.check_id, actual="bad", expected="good"),),
            )
    return PlanCheckDocument.from_checks(
        layer=document.layer,  # type: ignore[arg-type]
        applicability=document.applicability,  # type: ignore[arg-type]
        checks=checks,
    )


def _base_policy() -> Policy:
    return load_policy(Path.cwd())


def _replay(
    layer: str,
    *,
    checks: PlanCheckDocument,
    review: PlanReview | None = None,
    data_knowledge: DataKnowledge | None = None,
    base_policy: Policy | None = None,
    params: dict[str, object] | None = None,
):
    profile = get_layer_assurance_profile(layer)
    return replay_plan_check_policy(
        gates=_GATES,
        profile=profile,
        review=(
            bind_json_artifact(logical_path=profile.review_artifact, model=review)
            if review is not None
            else None
        ),
        checks=bind_json_artifact(logical_path=profile.checks_artifact, model=checks),
        data_knowledge=(
            bind_yaml_artifact(logical_path="repo:.aa/data-knowledge.yaml", model=data_knowledge)
            if data_knowledge is not None
            else None
        ),
        base_policy=base_policy or _base_policy(),
        change_id=_CHANGE_ID,
        params=params or {"force_continue": False},
    )


def _scenario(replay, action: str):
    return next(item for item in replay.scenarios if item.action == action)


@pytest.mark.parametrize("layer", ["api", "e2e"])
@pytest.mark.parametrize(
    ("action", "expected_verdict"),
    [
        ("warn", Verdict.PASS),
        ("block", Verdict.REJECT),
        ("require_human", Verdict.NEEDS_HUMAN_REVIEW),
    ],
)
def test_failing_check_routes_by_policy_action(layer: str, action: str, expected_verdict: Verdict) -> None:
    replay = _replay(
        layer,
        checks=_failed_checks(layer, "assert_ideal"),
        review=_review(layer),
        data_knowledge=_data_knowledge(),
    )
    scenario = _scenario(replay, action)
    assert scenario.verdict == expected_verdict.value
    assert scenario.policy_effect == "applied"


@pytest.mark.parametrize("layer", ["api", "e2e"])
@pytest.mark.parametrize("action", ["warn", "block", "require_human"])
def test_passing_checks_are_policy_inert(layer: str, action: str) -> None:
    replay = _replay(
        layer,
        checks=_applicable_checks(layer),
        review=_review(layer),
        data_knowledge=_data_knowledge(),
    )
    scenario = _scenario(replay, action)
    assert scenario.verdict == Verdict.PASS.value
    assert scenario.policy_effect == "no_failed_checks"


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_inapplicable_evidence_skips_all_scenarios(layer: str) -> None:
    replay = _replay(layer, checks=_inapplicable_checks(layer))
    assert len(replay.scenarios) == 3
    assert [item.action for item in replay.scenarios] == ["warn", "block", "require_human"]
    assert all(item.verdict == Verdict.SKIP.value for item in replay.scenarios)
    assert all(item.route == "skip" for item in replay.scenarios)


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_missing_capability_shadows_policy(layer: str) -> None:
    replay = _replay(
        layer,
        checks=_applicable_checks(layer),
        review=_review(layer, required_capabilities=["capabilities.missing.leaf"]),
        data_knowledge=_data_knowledge(),
    )
    scenario = _scenario(replay, "warn")
    assert scenario.verdict == Verdict.NEEDS_HUMAN_REVIEW.value
    assert scenario.route == "knowledge_remediation"
    assert scenario.policy_effect == "shadowed_by_capability_precondition"
    assert scenario.missing_capabilities == ["capabilities.missing.leaf"]


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_needs_fix_shadows_blocking_policy(layer: str) -> None:
    replay = _replay(
        layer,
        checks=_failed_checks(layer, "assert_ideal"),
        review=_review(layer, decision="needs_fix", auto_fix_allowed=True),
        data_knowledge=_data_knowledge(),
    )
    scenario = _scenario(replay, "block")
    assert scenario.verdict == Verdict.NEEDS_FIX.value
    assert scenario.policy_effect == "shadowed_by_gate_precondition"


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_explicit_reject_shadows_require_human_policy(layer: str) -> None:
    replay = _replay(
        layer,
        checks=_failed_checks(layer, "assert_ideal"),
        review=_review(
            layer,
            decision="reject",
            codegen_readiness="not_ready",
            human_review_required=True,
            risk_level="critical",
        ),
        data_knowledge=_data_knowledge(),
    )
    scenario = _scenario(replay, "require_human")
    assert scenario.verdict == Verdict.REJECT.value
    assert scenario.policy_effect == "shadowed_by_gate_precondition"


@pytest.mark.parametrize("layer", ["api", "e2e"])
def test_high_risk_shadows_policy_when_force_continue_false(layer: str) -> None:
    replay = _replay(
        layer,
        checks=_failed_checks(layer, "assert_ideal"),
        review=_review(layer, risk_level="high"),
        data_knowledge=_data_knowledge(),
        params={"force_continue": False},
    )
    scenario = _scenario(replay, "block")
    assert scenario.verdict == Verdict.NEEDS_HUMAN_REVIEW.value
    assert scenario.policy_effect == "shadowed_by_gate_precondition"


def test_force_continue_frozen_true_can_bypass_human_review_risk() -> None:
    replay = _replay(
        "api",
        checks=_failed_checks("api", "assert_ideal"),
        review=_review("api", decision="pass", risk_level="high", codegen_readiness="ready"),
        data_knowledge=_data_knowledge(),
        params={"force_continue": True},
    )
    scenario = _scenario(replay, "warn")
    assert scenario.verdict == Verdict.PASS.value
    assert scenario.policy_effect == "applied"


def test_bound_artifact_rejects_digest_mismatch() -> None:
    raw = b'{"schema_version":"1.0"}\n'
    with pytest.raises(ValueError, match="sha256 mismatch"):
        BoundArtifact(
            logical_path="review/api-plan-review.json",
            raw_bytes=raw,
            sha256="0" * 64,
            model=_review("api"),
        )


def test_noncanonical_bytes_preserve_bound_digests() -> None:
    profile = get_layer_assurance_profile("api")
    review = _review("api")
    payload = review.model_dump(mode="json")
    noncanonical = (json.dumps(payload, indent=4, sort_keys=False, ensure_ascii=False) + "  \n").encode(
        "utf-8"
    )
    assert noncanonical != json.dumps(payload, sort_keys=True).encode("utf-8")

    bound_review = bind_json_artifact(
        logical_path=profile.review_artifact,
        model=review,
        raw_bytes=noncanonical,
    )
    bound_checks = bind_json_artifact(
        logical_path=profile.checks_artifact,
        model=_applicable_checks("api"),
    )
    bound_dk = bind_yaml_artifact(
        logical_path="repo:.aa/data-knowledge.yaml",
        model=_data_knowledge(),
        raw_bytes=b"version: 1\nauth:\n  api_admin_token:\n    method: token\n    symbol: API_ADMIN_TOKEN\ncapabilities:\n  domain_factories: {}\n  adapters:\n    api: {}\n    e2e: {}\n    fuzz: {}\n    performance: {}\n  cleanup: {}\n",
    )

    report = evaluate_bound_plan_gate(
        gates=_GATES,
        profile=profile,
        review=bound_review,
        checks=bound_checks,
        data_knowledge=bound_dk,
        policy=_base_policy(),
        change_id=_CHANGE_ID,
        params={"force_continue": False},
    )
    assert report.reads_sha256[profile.review_artifact] == bound_review.sha256
    assert report.reads_sha256[profile.checks_artifact] == bound_checks.sha256
    assert report.reads_sha256["repo:.aa/data-knowledge.yaml"] == bound_dk.sha256


def test_historical_accept_risk_outside_temp_audit_cannot_mask_replay(tmp_path: Path) -> None:
    profile = get_layer_assurance_profile("api")
    historical_audit = tmp_path / "qa" / "changes" / _CHANGE_ID / "audit-events"
    historical_audit.mkdir(parents=True)
    historical_audit.joinpath("events.jsonl").write_text(
        json.dumps(
            {
                "type": "human_decision",
                "source": "decide",
                "checkpoint": profile.gate_id,
                "action": "accept_risk",
                "reason": "accepted",
                "who": "tester",
                "review_file": profile.review_artifact,
                "review_sha256": "deadbeef",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    replay = _replay(
        "api",
        checks=_failed_checks("api", "assert_ideal"),
        review=_review("api", human_review_required=True, risk_level="critical"),
        data_knowledge=_data_knowledge(),
    )
    scenario = _scenario(replay, "require_human")
    assert scenario.verdict == Verdict.NEEDS_HUMAN_REVIEW.value
    assert scenario.verdict != Verdict.PASS.value


def _project_tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_replay_leaves_real_project_tree_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = get_layer_assurance_profile("api")
    change_dir = tmp_path / "qa" / "changes" / _CHANGE_ID
    decoy_marker = b"# DECOY - replay must not mutate caller cwd artifacts\n"

    decoy_files = {
        tmp_path / ".aa" / "policy.yaml": decoy_marker + b"version: 1\n",
        tmp_path / ".aa" / "data-knowledge.yaml": decoy_marker,
        change_dir / profile.review_artifact: decoy_marker + b'{"decoy": true}\n',
        change_dir / profile.checks_artifact: decoy_marker + b'{"decoy": true}\n',
    }
    for path, content in decoy_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    base_policy = _base_policy()
    before = _project_tree_hashes(tmp_path)

    monkeypatch.chdir(tmp_path)
    _replay(
        "api",
        checks=_applicable_checks("api"),
        review=_review("api"),
        data_knowledge=_data_knowledge(),
        base_policy=base_policy,
    )

    assert _project_tree_hashes(tmp_path) == before


def test_bind_json_artifact_rejects_model_raw_bytes_mismatch() -> None:
    profile = get_layer_assurance_profile("api")
    review = _review("api")
    mismatched = _review("api", decision="reject")
    raw = (json.dumps(mismatched.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode("utf-8")
    with pytest.raises(ValueError, match="model does not match raw_bytes"):
        bind_json_artifact(logical_path=profile.review_artifact, model=review, raw_bytes=raw)


def test_replay_scenario_order_and_digests() -> None:
    replay = _replay(
        "api",
        checks=_failed_checks("api", "assert_ideal"),
        review=_review("api"),
        data_knowledge=_data_knowledge(),
    )
    assert [item.action for item in replay.scenarios] == ["warn", "block", "require_human"]
    for scenario in replay.scenarios:
        policy = _base_policy().model_copy(
            update={"plan_checks": dict.fromkeys(_base_policy().plan_checks, scenario.action)}
        )
        assert scenario.policy_digest == policy_digest(policy)


def test_baseline_gate_matches_route_derivation() -> None:
    profile = get_layer_assurance_profile("api")
    checks = _applicable_checks("api")
    review = _review("api")
    dk = _data_knowledge()
    report = evaluate_bound_plan_gate(
        gates=_GATES,
        profile=profile,
        review=bind_json_artifact(logical_path=profile.review_artifact, model=review),
        checks=bind_json_artifact(logical_path=profile.checks_artifact, model=checks),
        data_knowledge=bind_yaml_artifact(logical_path="repo:.aa/data-knowledge.yaml", model=dk),
        policy=_base_policy(),
        change_id=_CHANGE_ID,
        params={"force_continue": False},
    )
    route = plan_review_route(
        {
            "gate": {
                "verdict": report.verdict.value,
                "matched_rule": report.matched_rule,
                "reason": report.reason,
                "details": report.details,
            }
        }
    )
    replay = replay_plan_check_policy(
        gates=_GATES,
        profile=profile,
        review=bind_json_artifact(logical_path=profile.review_artifact, model=review),
        checks=bind_json_artifact(logical_path=profile.checks_artifact, model=checks),
        data_knowledge=bind_yaml_artifact(logical_path="repo:.aa/data-knowledge.yaml", model=dk),
        base_policy=_base_policy(),
        change_id=_CHANGE_ID,
        params={"force_continue": False},
    )
    assert replay.baseline.verdict == report.verdict
    assert replay.baseline.matched_rule == report.matched_rule
    assert _scenario(replay, "warn").route == route
