from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.phase5.conformance import EXPECTED_25_CASE_IDS

HARNESS_ROOT = Path(__file__).resolve().parents[2] / "benchmark" / "assurance-product-phase5"
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

compare = importlib.import_module("compare")
eval_mod = importlib.import_module("eval")
projection = importlib.import_module("projection")

BehavioralProjectionV1 = projection.BehavioralProjectionV1
ComparisonResultV1 = compare.ComparisonResultV1
DispositionV1 = compare.DispositionV1
compare_case = compare.compare_case
evaluate_complete_runs = eval_mod.evaluate_complete_runs
project_legacy_export = projection.project_legacy_export
project_new_export = projection.project_new_export

CASE_ID = "full-api-only-success"
INPUT_DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
CHANGED_DIGEST = "b" * 64
REPORT_BODY = '{"schema_version":"1","decision":"pass","sections":["summary","coverage"]}'
REPORT_DIGEST = hashlib.sha256(REPORT_BODY.encode("utf-8")).hexdigest()
GOVERNING_CONTRACT = "spec §17.5 baseline authority"

GOVERNED_FIELDS = (
    "terminal_class",
    "selected_families",
    "activated_families",
    "completed_families",
    "skipped_families",
    "artifact_contract",
    "issue_healing_decisions",
    "quality_metrics",
    "report",
    "retro",
    "improvement",
)


def default_source(*, runtime_identity: str, **overrides: Any) -> dict[str, Any]:
    source: dict[str, Any] = {
        "schema_version": "1",
        "case_id": CASE_ID,
        "input_digest": INPUT_DIGEST,
        "runtime_identity": runtime_identity,
        "terminal_class": "completed",
        "terminal_reason_category": None,
        "selected_families": ["api"],
        "activated_families": ["api"],
        "completed_families": ["api"],
        "skipped_families": ["e2e", "fuzz", "performance"],
        "gate_decisions": [
            {"semantic_role": "intake.review", "decision": "pass", "input_digest": INPUT_DIGEST},
            {
                "semantic_role": "generation.api.plan-review",
                "decision": "pass",
                "input_digest": INPUT_DIGEST,
            },
        ],
        "artifact_contract": [
            {
                "artifact_id": "qa/report.json",
                "media_type": "application/json",
                "sha256": REPORT_DIGEST,
                "semantic_projection": {"decision": "pass"},
            }
        ],
        "changed_files": [{"path": "tests/api/test_item.py", "sha256": CHANGED_DIGEST}],
        "execution_evidence": {"summary": {"passed": 1, "failed": 0}, "digest": CHANGED_DIGEST},
        "quality_metrics": {
            "coverage": {"outcome": "pass", "ratio": 1},
            "trace": {"complete": True},
            "quality": {"decision": "pass"},
        },
        "issue_healing_decisions": [],
        "durable_effects": [],
        "report": {
            "present": True,
            "digest": REPORT_DIGEST,
            "semantic_fields": {"decision": "pass"},
        },
        "retro": None,
        "improvement": None,
        "archive": None,
        "semantic_counts": {
            "retries": {"generation.api.plan-review": 0},
            "interrupts": {},
            "stops": {},
        },
        "diagnostics": [{"category": "info", "message": "completed"}],
    }
    source.update(overrides)
    return source


def write_export(root: Path, source: Mapping[str, Any]) -> Path:
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
    report = root / "result-tree" / "qa" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text(REPORT_BODY, encoding="utf-8")
    (root / "result-tree" / "tests" / "api").mkdir(parents=True)
    (root / "result-tree" / "tests" / "api" / "test_item.py").write_text(
        "def test_item() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    return root


def make_export(
    root: Path, *, runtime_identity: str, noise: Mapping[str, Any] | None = None, **overrides: Any
) -> Path:
    source = default_source(runtime_identity=runtime_identity, **overrides)
    if noise:
        source.update(noise)
    return write_export(root, source)


def load_manifest(export_root: Path) -> dict[str, Any]:
    return json.loads((export_root / "manifest.json").read_text(encoding="utf-8"))


def rewrite_manifest(export_root: Path, **updates: Any) -> None:
    payload = load_manifest(export_root)
    payload.update(updates)
    (export_root / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def copy_export(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def identity_disposition(case_id: str = CASE_ID) -> DispositionV1:
    return DispositionV1(
        case_id=case_id,
        field="runtime_identity",
        mode="intentionally-different",
        classification="required",
        predicate=None,
        governing_contract="spec §17.4 lock and runtime identity",
    )


@pytest.fixture
def legacy_export(tmp_path: Path) -> Path:
    return make_export(
        tmp_path / "legacy",
        runtime_identity="legacy-aa",
        noise={
            "session_id": "ses_legacy_abc",
            "conversation": [{"role": "assistant", "content": "thinking aloud"}],
            "timestamp": "2026-08-22T00:00:00Z",
            "event_seq": 41,
            "token_count": 1800,
            "log": "legacy verbose log line",
            "selected_families": ["api"],
            "skipped_families": ["performance", "fuzz", "e2e"],
            "gate_decisions": [
                {
                    "semantic_role": "generation.api.plan-review",
                    "decision": "pass",
                    "input_digest": INPUT_DIGEST,
                    "timestamp": "2026-08-22T00:00:01Z",
                    "event_seq": 12,
                },
                {
                    "semantic_role": "intake.review",
                    "decision": "pass",
                    "input_digest": INPUT_DIGEST,
                    "session_id": "ses_legacy_abc",
                },
            ],
        },
    )


@pytest.fixture
def new_export(tmp_path: Path) -> Path:
    return make_export(
        tmp_path / "new",
        runtime_identity="assurance-product",
        noise={
            "session_id": "ses_new_xyz",
            "conversation": [{"role": "user", "content": "continue"}],
            "timestamp": "2026-08-23T12:00:00Z",
            "event_seq": 7,
            "token_count": 42,
            "log": "new runtime debug prose",
            "selected_families": ["api"],
            "skipped_families": ["e2e", "performance", "fuzz"],
            "gate_decisions": [
                {
                    "semantic_role": "intake.review",
                    "decision": "pass",
                    "input_digest": INPUT_DIGEST,
                    "token_count": 9,
                },
                {
                    "semantic_role": "generation.api.plan-review",
                    "decision": "pass",
                    "input_digest": INPUT_DIGEST,
                    "conversation": ["ignored"],
                },
            ],
        },
    )


def test_default_fixture_uses_task1_case_id() -> None:
    assert CASE_ID in EXPECTED_25_CASE_IDS
    assert len(EXPECTED_25_CASE_IDS) == 25


def test_projections_ignore_provider_conversation_noise(legacy_export: Path, new_export: Path) -> None:
    legacy = project_legacy_export(legacy_export)
    new = project_new_export(new_export)
    assert legacy.artifact_contract == new.artifact_contract
    assert "session_id" not in legacy.model_dump_json()
    assert "conversation" not in new.model_dump_json()


def test_projection_schema_version_is_string_one(legacy_export: Path, new_export: Path) -> None:
    legacy = project_legacy_export(legacy_export)
    new = project_new_export(new_export)
    assert legacy.schema_version == "1"
    assert new.schema_version == "1"
    assert isinstance(legacy.schema_version, str)


def test_projection_is_frozen_and_forbids_extras(legacy_export: Path) -> None:
    projection = project_legacy_export(legacy_export)
    with pytest.raises((TypeError, ValidationError)):
        projection.terminal_class = "stopped"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        BehavioralProjectionV1.model_validate({**projection.model_dump(mode="json"), "session_id": "x"})


def test_equivalent_exports_share_governed_fields(legacy_export: Path, new_export: Path) -> None:
    legacy = project_legacy_export(legacy_export)
    new = project_new_export(new_export)
    assert legacy.case_id == new.case_id == CASE_ID
    assert legacy.input_digest == new.input_digest
    assert legacy.terminal_class == new.terminal_class == "completed"
    assert legacy.selected_families == new.selected_families == frozenset({"api"})
    assert legacy.skipped_families == new.skipped_families == frozenset({"e2e", "fuzz", "performance"})
    assert legacy.gate_decisions == new.gate_decisions
    assert legacy.quality_metrics == new.quality_metrics
    assert legacy.report == new.report
    assert legacy.runtime_identity != new.runtime_identity


def test_projection_reads_only_files_under_export_root(legacy_export: Path) -> None:
    outside = legacy_export.parent / "invocations" / "inv-secret" / "ledger.json"
    outside.parent.mkdir(parents=True)
    outside.write_text(
        json.dumps({"terminal_class": "failed", "session_id": "engine-session", "conversation": []}),
        encoding="utf-8",
    )
    rewrite_manifest(
        legacy_export,
        engine_state=str(outside),
        invocation_root=str(outside.parent),
    )
    projection = project_legacy_export(legacy_export)
    dumped = projection.model_dump_json()
    assert projection.terminal_class == "completed"
    assert "engine-session" not in dumped
    assert "session_id" not in dumped
    assert "invocations" not in dumped


def test_changing_governed_field_fails_comparison(legacy_export: Path, tmp_path: Path) -> None:
    mutated = copy_export(legacy_export, tmp_path / "mutated-terminal")
    rewrite_manifest(mutated, terminal_class="stopped", terminal_reason_category="healing_disallowed")
    result = compare_case(CASE_ID, legacy_export, mutated, {})
    assert result.passed is False
    assert result.undisposed_differences
    assert any(
        isinstance(item, Mapping) and item.get("field") == "terminal_class"
        for item in result.undisposed_differences
    )


@pytest.mark.parametrize("field", GOVERNED_FIELDS)
def test_each_governed_field_mutation_fails(legacy_export: Path, tmp_path: Path, field: str) -> None:
    mutated = copy_export(legacy_export, tmp_path / f"mutated-{field}")
    payload = load_manifest(mutated)
    if field.endswith("_families"):
        payload[field] = ["fuzz"]
    elif field == "artifact_contract":
        payload[field] = [
            {
                "artifact_id": "qa/report.json",
                "media_type": "application/json",
                "sha256": "c" * 64,
                "semantic_projection": {"decision": "pass"},
            }
        ]
    elif field == "issue_healing_decisions":
        payload[field] = [{"issue_class": "locator", "decision": "heal", "evidence_digest": CHANGED_DIGEST}]
    elif field == "quality_metrics":
        payload[field] = {
            "coverage": {"outcome": "fail", "ratio": 0},
            "trace": {"complete": False},
            "quality": {"decision": "fail"},
        }
    elif field == "report":
        payload[field] = {"present": False, "digest": None, "semantic_fields": None}
    elif field in {"retro", "improvement"}:
        payload[field] = {"present": True, "digest": CHANGED_DIGEST, "semantic_fields": {"ok": True}}
    else:
        payload[field] = "stopped"
    (mutated / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    result = compare_case(CASE_ID, legacy_export, mutated, {})
    assert result.passed is False
    assert any(
        isinstance(item, Mapping) and item.get("field") == field for item in result.undisposed_differences
    )


def test_enumerated_observational_noise_normalizes_away(legacy_export: Path, new_export: Path) -> None:
    legacy = project_legacy_export(legacy_export)
    new = project_new_export(new_export)
    dumped = legacy.model_dump_json() + new.model_dump_json()
    for noise in (
        "session_id",
        "conversation",
        "timestamp",
        "event_seq",
        "token_count",
        "ses_legacy_abc",
        "ses_new_xyz",
        "thinking aloud",
        "legacy verbose log line",
        "new runtime debug prose",
    ):
        assert noise not in dumped
    result = compare_case(CASE_ID, legacy_export, new_export, {"runtime_identity": identity_disposition()})
    assert result.passed is True
    assert result.undisposed_differences == ()


def test_evaluate_complete_runs_emits_external_eval(tmp_path: Path) -> None:
    opencode = make_export(
        tmp_path / "opencode",
        runtime_identity="assurance-opencode",
        noise={"session_id": "opencode-session", "token_count": 11},
    )
    cursor = make_export(
        tmp_path / "cursor",
        runtime_identity="assurance-cursor",
        noise={"conversation": ["cursor chat"], "event_seq": 99},
    )
    evaluated = evaluate_complete_runs(opencode, cursor)
    assert evaluated.schema_version == "1"
    assert evaluated.opencode_export_digest.startswith("sha256:")
    assert evaluated.cursor_export_digest.startswith("sha256:")
    assert evaluated.opencode_export_digest != evaluated.cursor_export_digest
    assert evaluated.retro_input_digest.startswith("sha256:")
    assert evaluated.findings
    assert all(finding.outcome in {"pass", "intentionally-different"} for finding in evaluated.findings)
    assert "session_id" not in evaluated.model_dump_json()


def test_evaluate_complete_runs_rejects_incomplete_export(tmp_path: Path) -> None:
    opencode = make_export(tmp_path / "opencode", runtime_identity="assurance-opencode")
    cursor = make_export(
        tmp_path / "cursor",
        runtime_identity="assurance-cursor",
        terminal_class="failed",
    )
    with pytest.raises(ValueError, match="completed"):
        evaluate_complete_runs(opencode, cursor)
