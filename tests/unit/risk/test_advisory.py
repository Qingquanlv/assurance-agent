from assurance_agent.risk.advisory import validate_advisory
from assurance_agent.risk.context import EvidenceEntry, ImpactBlock, RiskContext


def make_context(**overrides: object) -> RiskContext:
    ctx = RiskContext(
        change_id="CH-1",
        generated_at="2026-07-15T00:00:00+00:00",
        aggregation_policy={},
        archive_window={},
        staleness={"max_age_days": 30, "stale": False},
        impact=ImpactBlock(
            diff_base="main", changed_files=[], modules=[],
            affected_case_ids=["TC_MENU_001"], affected_cases_by_module={}, affected_test_files=[],
        ),
        case_signals=[],
        test_health=[],
        historical_issues=[],
        evidence=[EvidenceEntry(id="EV-DIFF-MENUS-HIGH", type="code_change", module="menus", confidence="high", source="git diff")],
        degraded=False,
        degraded_reasons=[],
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def test_valid_minimal_advisory_passes() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-DIFF-MENUS-HIGH"]}],
        "open_questions_for_case_design": [],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=["TC_MENU_001"])
    assert ok is True, errors


def test_unknown_evidence_id_fails() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "low", "evidence_ids": ["EV-DOES-NOT-EXIST"]}],
        "open_questions_for_case_design": [],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert any("unknown id" in e for e in errors)


def test_high_confidence_requires_non_empty_evidence_ids() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": []}],
        "open_questions_for_case_design": [],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert any("confidence high requires non-empty evidence_ids" in e for e in errors)


def test_high_confidence_forbidden_when_stale() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "high", "evidence_ids": ["EV-DIFF-MENUS-HIGH"]}],
        "open_questions_for_case_design": [],
    }
    ctx = make_context(staleness={"max_age_days": 30, "stale": True})
    ok, errors = validate_advisory(ctx, advisory, known_case_ids=[])
    assert ok is False
    assert any("staleness.stale" in e for e in errors)


def test_missing_evidence_must_use_low_confidence() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [{"id": "WL-1", "confidence": "medium", "evidence_ids": []}],
        "open_questions_for_case_design": [],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert any("missing evidence_ids must use confidence low" in e for e in errors)


def test_open_question_answered_requires_intent_and_via() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [],
        "open_questions_for_case_design": [{"id": "OQ-1", "status": "answered"}],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert any("requires assertion_intent" in e for e in errors)
    assert any("requires answered_via" in e for e in errors)


def test_autonomous_mode_forbids_explore_answered_via() -> None:
    advisory = {
        "schema_version": "1.0",
        "watchlist": [],
        "open_questions_for_case_design": [
            {"id": "OQ-1", "status": "answered", "assertion_intent": "assert_ideal", "answered_via": "explore", "answer": "x"}
        ],
    }
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[], interaction_mode="autonomous")
    assert ok is False
    assert any("autonomous run forbids answered_via explore" in e for e in errors)


def test_structural_failure_short_circuits() -> None:
    advisory = {"schema_version": "1.0", "open_questions_for_case_design": []}
    ok, errors = validate_advisory(make_context(), advisory, known_case_ids=[])
    assert ok is False
    assert errors
