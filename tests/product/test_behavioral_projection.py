from __future__ import annotations

import ast
import hashlib
import importlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.product.conformance import EXPECTED_25_CASE_IDS

HARNESS_ROOT = Path(__file__).resolve().parents[2] / "benchmark" / "assurance-product"
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

eval_mod = importlib.import_module("eval")
projection = importlib.import_module("projection")

BehavioralProjectionV1 = projection.BehavioralProjectionV1
evaluate_complete_run = eval_mod.evaluate_complete_run
project_legacy_export = projection.project_legacy_export
project_new_export = projection.project_new_export
FORBIDDEN_HARNESS_IMPORTS = ("assurance_agent", "graph_engine", "assurance_product")

CASE_ID = "full-api-only-success"
INPUT_DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
CHANGED_DIGEST = "b" * 64
REPORT_BODY = '{"schema_version":"1","decision":"pass","sections":["summary","coverage"]}'
REPORT_DIGEST = hashlib.sha256(REPORT_BODY.encode("utf-8")).hexdigest()


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


def imported_top_level_modules(root: Path) -> set[str]:
    names: set[str] = set()
    if not root.is_dir():
        return names
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or relative.parts[0] == "results":
            continue
        if relative.as_posix() == "run_item.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".", 1)[0])
    return names


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
    assert (
        project_legacy_export(legacy_export).runtime_identity
        != project_new_export(new_export).runtime_identity
    )


def test_harness_modules_do_not_import_runtime_packages() -> None:
    assert (HARNESS_ROOT / "projection.py").is_file()
    assert (HARNESS_ROOT / "eval.py").is_file()
    imports = imported_top_level_modules(HARNESS_ROOT)
    assert "assurance_agent" not in imports
    assert "graph_engine" not in imports
    assert "assurance_product" not in imports
    for name in FORBIDDEN_HARNESS_IMPORTS:
        assert name not in imports


def test_evaluate_complete_runs_emits_external_eval(tmp_path: Path) -> None:
    opencode = make_export(
        tmp_path / "opencode",
        runtime_identity="assurance-opencode",
        noise={"session_id": "opencode-session", "token_count": 11},
    )
    evaluated = evaluate_complete_run(opencode)
    assert evaluated.schema_version == "1"
    assert evaluated.export_digest.startswith("sha256:")
    assert evaluated.retro_input_digest.startswith("sha256:")
    assert evaluated.findings
    assert all(finding.outcome == "pass" for finding in evaluated.findings)
    assert "session_id" not in evaluated.model_dump_json()


def test_evaluate_complete_runs_rejects_incomplete_export(tmp_path: Path) -> None:
    failed = make_export(
        tmp_path / "failed",
        runtime_identity="assurance-opencode",
        terminal_class="failed",
    )
    with pytest.raises(ValueError, match="completed"):
        evaluate_complete_run(failed)


@pytest.mark.parametrize("field", ("artifact_contract", "gate_decisions"))
def test_omitted_required_projection_array_refuses_to_project(tmp_path: Path, field: str) -> None:
    source = default_source(runtime_identity="legacy-aa")
    del source[field]
    export = write_export(tmp_path / f"omit-{field}", source)
    with pytest.raises(projection.ProjectionError, match=field):
        project_legacy_export(export)


def test_explicit_empty_required_arrays_still_project(tmp_path: Path) -> None:
    export = make_export(
        tmp_path / "empty-arrays",
        runtime_identity="legacy-aa",
        artifact_contract=[],
        gate_decisions=[],
    )
    projected = project_legacy_export(export)
    assert projected.artifact_contract == ()
    assert projected.gate_decisions == ()
