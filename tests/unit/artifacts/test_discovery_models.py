"""Phase 1 adversarial discovery artifact schemas (Task A)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.discovery import (
    AdversarialStrategy,
    CampaignResult,
    CampaignSpec,
    Counterexample,
    CounterexampleReplay,
    GeneratedFileEntry,
    GeneratedManifest,
    MinimizationInfo,
    OracleRule,
    OracleSetSnapshot,
    OracleSpec,
    RoundDecision,
    StatusCodeRule,
)
from assurance_agent.artifacts.registry import match_artifact


def _replay(**overrides: Any) -> CounterexampleReplay:
    base: dict[str, Any] = dict(attempts=3, reproduced=3, artifact_refs=("execution/replay-1.json",))
    base.update(overrides)
    return CounterexampleReplay(**base)


def _counterexample(**overrides: Any) -> Counterexample:
    base: dict[str, Any] = dict(
        schema_version="1",
        counterexample_id="CE-auth-tenant-001",
        campaign_id="CAM-001",
        round_id="R0007",
        surface="api",
        technique="stateful_fuzz",
        obligation_ids=("OBL-auth-tenant-read",),
        oracle_id="ORACLE-auth-tenant-isolation",
        oracle_kind="hard_oracle",
        environment_digest="sha256:" + ("a" * 64),
        generated_file_digests={"generated/tests/api/test_tenant.py": "sha256:" + ("b" * 64)},
        setup={"tenant": "A"},
        actions=({"method": "GET", "path": "/api/users"},),
        observed={"status": 200},
        expected={"status": 403},
        seed=12345,
        minimization=MinimizationInfo(status="minimized", parent_counterexample_id=None),
        replay=_replay(),
        finding_status="confirmed",
    )
    base.update(overrides)
    return Counterexample(**base)


def _manifest(**overrides: Any) -> GeneratedManifest:
    base: dict[str, Any] = dict(
        schema_version="1",
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        round_id="R0001",
        parent_round_ids=(),
        base_revision="deadbeef",
        strategy_ids=("api.auth.tenant-boundary",),
        files=(
            GeneratedFileEntry(
                source="generated/tests/api/test_tenant_boundary.py",
                target="tests/api/test_tenant_boundary.py",
                sha256="sha256:" + ("c" * 64),
                role="search_test",
            ),
        ),
        execution_selection=("tests/api/test_tenant_boundary.py",),
        oracle_refs=("ORACLE-auth-tenant-isolation",),
        seed=12345,
    )
    base.update(overrides)
    return GeneratedManifest(**base)


def test_counterexample_happy_path_round_trips() -> None:
    ce = _counterexample()
    restored = Counterexample.model_validate(ce.model_dump(mode="json"))
    assert restored == ce
    assert restored.finding_status == "confirmed"
    assert restored.replay.reproduced == restored.replay.attempts


def test_generated_manifest_happy_path_round_trips() -> None:
    manifest = _manifest()
    restored = GeneratedManifest.model_validate(manifest.model_dump(mode="json"))
    assert restored == manifest
    assert restored.files[0].target.startswith("tests/api/")


def test_generated_manifest_rejects_absolute_target() -> None:
    with pytest.raises(ValidationError, match="path"):
        _manifest(
            files=(
                GeneratedFileEntry(
                    source="generated/tests/api/test_x.py",
                    target="/tmp/tests/api/test_x.py",
                    sha256="sha256:" + ("d" * 64),
                    role="search_test",
                ),
            ),
        )


def test_generated_manifest_rejects_parent_traversal() -> None:
    with pytest.raises(ValidationError, match="path"):
        _manifest(
            files=(
                GeneratedFileEntry(
                    source="generated/tests/api/test_x.py",
                    target="tests/api/../../etc/passwd",
                    sha256="sha256:" + ("d" * 64),
                    role="search_test",
                ),
            ),
        )


def test_generated_manifest_rejects_dotdot_source() -> None:
    with pytest.raises(ValidationError, match="path"):
        _manifest(
            files=(
                GeneratedFileEntry(
                    source="../escape/tests/api/test_x.py",
                    target="tests/api/test_x.py",
                    sha256="sha256:" + ("d" * 64),
                    role="search_test",
                ),
            ),
        )


def test_generated_manifest_rejects_non_api_target() -> None:
    with pytest.raises(ValidationError, match="tests/api"):
        _manifest(
            files=(
                GeneratedFileEntry(
                    source="generated/tests/e2e/test_x.py",
                    target="tests/e2e/test_x.py",
                    sha256="sha256:" + ("d" * 64),
                    role="search_test",
                ),
            ),
        )


def test_generated_manifest_rejects_symlink_escape_pattern() -> None:
    with pytest.raises(ValidationError, match="path"):
        _manifest(
            files=(
                GeneratedFileEntry(
                    source="generated/tests/api/test_x.py",
                    target="tests/api/foo/./../../secret",
                    sha256="sha256:" + ("d" * 64),
                    role="search_test",
                ),
            ),
        )


def test_confirmed_requires_hard_oracle_and_full_replay() -> None:
    with pytest.raises(ValidationError, match="confirmed"):
        _counterexample(oracle_kind="search_heuristic", finding_status="confirmed")

    with pytest.raises(ValidationError, match="confirmed"):
        _counterexample(replay=_replay(attempts=0, reproduced=0), finding_status="confirmed")

    with pytest.raises(ValidationError, match="confirmed"):
        _counterexample(replay=_replay(attempts=3, reproduced=2), finding_status="confirmed")


def test_search_heuristic_may_be_needs_review() -> None:
    ce = _counterexample(
        oracle_kind="search_heuristic",
        finding_status="needs_review",
        replay=_replay(attempts=1, reproduced=0, artifact_refs=()),
    )
    assert ce.finding_status == "needs_review"


def test_finding_status_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        _counterexample(finding_status="probably_bug")  # type: ignore[arg-type]


def test_oracle_set_and_campaign_documents_validate() -> None:
    oracle = OracleSpec(
        oracle_id="ORACLE-auth-tenant-isolation",
        kind="hard_oracle",
        surface="api",
        rule=OracleRule(status_codes=StatusCodeRule(allowed_codes=(403, 404))),
    )
    snapshot = OracleSetSnapshot(
        schema_version="1",
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        oracles=(oracle,),
    )
    assert snapshot.oracles[0].kind == "hard_oracle"

    campaign = CampaignSpec(
        schema_version="1",
        campaign_id="CAM-001",
        change_id="CH-DEMO-001",
        surfaces=("api",),
        strategy_ids=("api.auth.tenant-boundary",),
        cadence_profile="pr",
    )
    strategy = AdversarialStrategy(
        strategy_id="api.auth.tenant-boundary",
        version="1",
        surface="api",
        technique="stateful_fuzz",
        oracle_family_ids=("ORACLE-auth-tenant-isolation",),
    )
    decision = RoundDecision(
        schema_version="1",
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        round_id="R0001",
        strategy_ids=(strategy.strategy_id,),
        seed=1,
    )
    result = CampaignResult(
        schema_version="1",
        campaign_id="CAM-001",
        change_id="CH-DEMO-001",
        status="completed",
        surfaces=("api",),
        rounds_completed=1,
        counterexample_count=1,
        confirmed_count=1,
    )
    assert decision.round_id == "R0001"
    assert result.confirmed_count == 1
    assert campaign.surfaces == ("api",)


def test_discovery_registry_patterns() -> None:
    expected = {
        "discovery/campaign-spec.json": "discovery_campaign_spec",
        "discovery/oracle-set.json": "discovery_oracle_set",
        "discovery/rounds/R0001/decision.json": "discovery_round_decision",
        "discovery/rounds/R0001/generated-manifest.json": "discovery_generated_manifest",
        "discovery/counterexamples/CE-001.json": "discovery_counterexample",
        "discovery/counterexamples/CE-001/replay/attempt-0.json": "discovery_replay_attempt_receipt",
        "discovery/campaign-result.json": "discovery_campaign_result",
    }
    for path, artifact_type in expected.items():
        spec = match_artifact(path)
        assert spec is not None, path
        assert spec.artifact_type == artifact_type
        assert spec.compat == "must_compat"

    assert match_artifact("discovery/rounds/R0001/findings.json") is None
    assert match_artifact("discovery/counterexamples/nested/CE.yaml") is None
