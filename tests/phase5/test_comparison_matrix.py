from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.phase5.conformance import EVIDENCE_ROOT, EXPECTED_25_CASE_IDS, load_yaml
from tests.phase5.test_comparison_isolation import imported_top_level_modules

REPO = Path(__file__).resolve().parents[2]
HARNESS_ROOT = REPO / "benchmark" / "assurance-product-phase5"
PHASE5_TESTS = Path(__file__).resolve().parent
MANIFEST_PATH = HARNESS_ROOT / "comparison-manifest.json"
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

compare = importlib.import_module("compare")
projection = importlib.import_module("projection")

COMPARED_FIELDS = compare.COMPARED_FIELDS
ComparisonManifestV1 = compare.ComparisonManifestV1
DispositionV1 = compare.DispositionV1
compare_case = compare.compare_case
digest_directory = projection.digest_directory
project_legacy_export = projection.project_legacy_export
project_new_export = projection.project_new_export

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUIRED_ARRAYS = (
    "selected_families",
    "activated_families",
    "completed_families",
    "skipped_families",
    "gate_decisions",
    "artifact_contract",
    "changed_files",
    "issue_healing_decisions",
    "durable_effects",
    "diagnostics",
)
FAMILY_EVIDENCE_ROLES = ("plan", "plan-review", "codegen", "execution")
FOUR_FAMILIES = ("api", "e2e", "fuzz", "performance")


@pytest.fixture
def comparison_manifest() -> Any:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return ComparisonManifestV1.model_validate(raw)


def resolve_export(relative: str) -> Path:
    return PHASE5_TESTS / relative


def export_manifest(export_root: Path) -> dict[str, Any]:
    return json.loads((export_root / "manifest.json").read_text(encoding="utf-8"))


def difference_fields(result: Any) -> set[str]:
    fields: set[str] = set()
    for bucket in (result.governed_differences, result.undisposed_differences):
        for item in bucket:
            if isinstance(item, Mapping) and isinstance(item.get("field"), str):
                fields.add(item["field"])
    return fields


def load_disposition_ledger() -> dict[str, object]:
    return load_yaml(EVIDENCE_ROOT / "comparison-dispositions.yaml")


def iter_disposition_rows(ledger: Mapping[str, object]) -> tuple[dict[str, Any], ...]:
    cases = ledger.get("cases")
    if not isinstance(cases, list):
        raise AssertionError("comparison-dispositions.yaml must list cases")
    rows: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise AssertionError("each comparison case must be a mapping")
        case_id = case.get("id")
        if not isinstance(case_id, str):
            raise AssertionError("each comparison case must have a string id")
        fields = case.get("fields")
        if fields is None:
            fields = []
        if not isinstance(fields, list):
            raise AssertionError(f"{case_id} fields must be an array")
        for item in fields:
            if not isinstance(item, Mapping):
                raise AssertionError(f"{case_id} field row must be a mapping")
            rows.append({"case_id": case_id, **dict(item)})
    return tuple(rows)


def dispositions_for_case(case_id: str) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for row in iter_disposition_rows(load_disposition_ledger()):
        if row["case_id"] != case_id:
            continue
        indexed[str(row["field"])] = DispositionV1.model_validate(row)
    return indexed


def validate_disposition_ledger(
    ledger: Mapping[str, object],
    actual_differences: Mapping[tuple[str, str], bool],
) -> None:
    for row in iter_disposition_rows(ledger):
        case_id = str(row["case_id"])
        field = str(row.get("field", ""))
        if case_id not in EXPECTED_25_CASE_IDS:
            raise AssertionError(f"wildcard or unknown case disposition: {case_id}")
        if field not in COMPARED_FIELDS:
            raise AssertionError(f"wildcard or unknown field disposition: {case_id}/{field}")
        if any(token in case_id or token in field for token in ("*", "?", "[")):
            raise AssertionError(f"wildcard disposition: {case_id}/{field}")
        if not actual_differences.get((case_id, field), False):
            raise AssertionError(f"stale disposition: {case_id}/{field}")


def mutate_projection_field(payload: dict[str, Any], field: str) -> None:
    if field == "input_digest":
        payload[field] = "f" * 64
    elif field == "runtime_identity":
        payload[field] = "mutated-runtime"
    elif field == "terminal_class":
        payload[field] = "failed"
    elif field == "terminal_reason_category":
        payload[field] = "mutated_reason"
    elif field.endswith("_families"):
        payload[field] = ["mutated-family"]
    elif field == "gate_decisions":
        payload[field] = [{"semantic_role": "mutated.gate", "decision": "fail", "input_digest": "a" * 64}]
    elif field == "artifact_contract":
        payload[field] = [
            {
                "artifact_id": "qa/mutated.json",
                "media_type": "application/json",
                "sha256": "c" * 64,
                "semantic_projection": {"mutated": True},
            }
        ]
    elif field == "changed_files":
        payload[field] = [{"path": "mutated.py", "sha256": "d" * 64}]
    elif field == "execution_evidence":
        payload[field] = {"summary": {"passed": 0, "failed": 1}, "digest": "e" * 64}
    elif field == "quality_metrics":
        payload[field] = {
            "coverage": {"outcome": "fail", "ratio": 0},
            "trace": {"complete": False},
            "quality": {"decision": "fail"},
        }
    elif field == "issue_healing_decisions":
        payload[field] = [{"issue_class": "locator", "decision": "heal", "evidence_digest": "b" * 64}]
    elif field == "durable_effects":
        payload[field] = [
            {
                "effect_id": "fx-mutated",
                "idempotency_key": "key-mutated",
                "status": "applied",
                "receipt_digest": "a" * 64,
                "observed_external_projection": {"mutated": True},
            }
        ]
    elif field in {"report", "retro", "improvement", "archive"}:
        payload[field] = {"present": True, "digest": "a" * 64, "semantic_fields": {"mutated": True}}
    elif field == "semantic_counts":
        payload[field] = {"retries": {"mutated": 9}, "interrupts": {}, "stops": {}}
    elif field == "diagnostics":
        payload[field] = [{"category": "error", "message": "mutated"}]
    else:
        raise AssertionError(f"no mutation defined for {field}")


def test_comparison_manifest_is_the_exact_spec_matrix(comparison_manifest: Any) -> None:
    expected = EXPECTED_25_CASE_IDS
    assert expected == (
        "full-api-only-success",
        "full-e2e-only-success",
        "full-fuzz-only-success",
        "full-performance-only-success",
        "full-all-four-family-success",
        "intake-review-needs-fix-then-pass",
        "plan-review-invalid-output-bounded-retry",
        "codegen-validation-failure-bounded-fix",
        "execution-closed-mapping-no-stale-test",
        "coverage-insufficient-repair-reexecution-pass",
        "coverage-repair-no-progress-exhausted",
        "healing-disallowed-business-stop",
        "report-generation-required-outputs",
        "issue-analysis-reconcile-path",
        "archive-durable-effect-replay",
        "retro-collect-analyze-propose-reconcile",
        "improvement-review-evaluate-export-apply",
        "improvement-rollback",
        "human-interrupt-exact-resume",
        "transient-local-retry",
        "opencode-ambiguous-create-recovery",
        "cursor-unknown-process-indeterminate",
        "engine-crash-after-provider-terminal-receipt",
        "config-model-graph-source-drift-rejection",
        "replay-after-provider-state-removal",
    )
    assert tuple(item.id for item in comparison_manifest.cases) == expected
    assert len(comparison_manifest.cases) == 25
    assert len(set(item.id for item in comparison_manifest.cases)) == 25
    assert all(item.legacy_export_digest.startswith("sha256:") for item in comparison_manifest.cases)
    assert all(item.current_export_digest.startswith("sha256:") for item in comparison_manifest.cases)


def test_manifest_schema_version_is_string_one(comparison_manifest: Any) -> None:
    assert comparison_manifest.schema_version == "1"
    assert isinstance(comparison_manifest.schema_version, str)


def test_committed_export_digests_are_authenticated(comparison_manifest: Any) -> None:
    for item in comparison_manifest.cases:
        legacy = resolve_export(item.legacy_export)
        current = resolve_export(item.current_export)
        assert legacy.is_dir()
        assert current.is_dir()
        assert DIGEST_RE.fullmatch(item.legacy_export_digest)
        assert DIGEST_RE.fullmatch(item.current_export_digest)
        assert digest_directory(legacy) == item.legacy_export_digest
        assert digest_directory(current) == item.current_export_digest


def test_every_fixture_declares_required_arrays(comparison_manifest: Any) -> None:
    for item in comparison_manifest.cases:
        for relative in (item.legacy_export, item.current_export):
            payload = export_manifest(resolve_export(relative))
            for field in REQUIRED_ARRAYS:
                assert field in payload, f"{item.id} {relative} omitted {field}"
                assert isinstance(payload[field], list), f"{item.id} {relative} {field} must be an array"


def governed_signature_without_identity_or_diagnostics(projected: Any) -> dict[str, Any]:
    return {
        field: projection.jsonable(getattr(projected, field))
        for field in COMPARED_FIELDS
        if field not in {"runtime_identity", "diagnostics"}
    }


def test_current_projections_are_pairwise_distinct_without_identity_or_diagnostics(
    comparison_manifest: Any,
) -> None:
    signatures: dict[str, dict[str, Any]] = {}
    for item in comparison_manifest.cases:
        projected = project_new_export(resolve_export(item.current_export))
        signatures[item.id] = governed_signature_without_identity_or_diagnostics(projected)
    ids = list(signatures)
    collisions: list[str] = []
    for index, left_id in enumerate(ids):
        for right_id in ids[index + 1 :]:
            if signatures[left_id] == signatures[right_id]:
                collisions.append(f"{left_id} == {right_id}")
    assert collisions == [], (
        "current projections collide after dropping runtime_identity and diagnostics: "
        + "; ".join(collisions)
    )


def test_all_twenty_five_cases_are_governed_passes(comparison_manifest: Any) -> None:
    executed: list[str] = []
    extra = 0
    missing = 0
    undisposed = 0
    governed_passes = 0
    seen = {item.id for item in comparison_manifest.cases}
    for case_id in EXPECTED_25_CASE_IDS:
        if case_id not in seen:
            missing += 1
            continue
        item = next(entry for entry in comparison_manifest.cases if entry.id == case_id)
        result = compare_case(
            case_id,
            resolve_export(item.legacy_export),
            resolve_export(item.current_export),
            dispositions_for_case(case_id),
        )
        executed.append(case_id)
        if result.undisposed_differences:
            undisposed += len(result.undisposed_differences)
        if result.passed:
            governed_passes += 1
    extra = len(seen - set(EXPECTED_25_CASE_IDS))
    assert tuple(executed) == EXPECTED_25_CASE_IDS
    assert len(executed) == 25
    assert governed_passes == 25
    assert missing == 0
    assert extra == 0
    assert undisposed == 0


def test_all_four_family_case_has_planning_review_codegen_execution(
    comparison_manifest: Any,
) -> None:
    item = next(entry for entry in comparison_manifest.cases if entry.id == "full-all-four-family-success")
    for relative in (item.legacy_export, item.current_export):
        projected = project_new_export(resolve_export(relative))
        assert projected.completed_families == frozenset(FOUR_FAMILIES)
        roles = {gate.semantic_role for gate in projected.gate_decisions}
        for family in FOUR_FAMILIES:
            for role in FAMILY_EVIDENCE_ROLES:
                assert f"generation.{family}.{role}" in roles or (
                    role == "execution" and f"execution.{family}" in roles
                )


def test_report_case_keeps_required_outputs_in_the_result_tree(comparison_manifest: Any) -> None:
    item = next(
        entry for entry in comparison_manifest.cases if entry.id == "report-generation-required-outputs"
    )
    for relative in (item.legacy_export, item.current_export):
        root = resolve_export(relative)
        projected = project_new_export(root)
        assert projected.report.present is True
        assert projected.report.semantic_fields is not None
        report = root / "result-tree" / "qa" / "report.json"
        assert report.is_file()


@pytest.mark.parametrize("field", COMPARED_FIELDS)
def test_changing_governed_projection_field_fails_unless_disposed(
    comparison_manifest: Any, tmp_path: Path, field: str
) -> None:
    item = comparison_manifest.cases[0]
    assert item.id == "full-api-only-success"
    mutated = tmp_path / "mutated"
    shutil.copytree(resolve_export(item.current_export), mutated)
    payload = export_manifest(mutated)
    mutate_projection_field(payload, field)
    (mutated / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    remaining = {name: row for name, row in dispositions_for_case(item.id).items() if name != field}
    result = compare_case(item.id, resolve_export(item.legacy_export), mutated, remaining)
    assert result.passed is False
    assert field in difference_fields(result)
    assert any(
        isinstance(entry, Mapping) and entry.get("field") == field for entry in result.undisposed_differences
    )


def test_committed_dispositions_are_exact_and_not_stale(comparison_manifest: Any) -> None:
    actual: dict[tuple[str, str], bool] = {}
    for item in comparison_manifest.cases:
        result = compare_case(
            item.id,
            resolve_export(item.legacy_export),
            resolve_export(item.current_export),
            {},
        )
        diffs = difference_fields(result)
        for field in COMPARED_FIELDS:
            actual[(item.id, field)] = field in diffs
    validate_disposition_ledger(load_disposition_ledger(), actual)


def test_wildcard_disposition_is_rejected() -> None:
    with pytest.raises(AssertionError, match="wildcard"):
        validate_disposition_ledger(
            {
                "cases": [
                    {
                        "id": "*",
                        "fields": [
                            {
                                "field": "runtime_identity",
                                "mode": "intentionally-different",
                                "classification": "required",
                                "predicate": None,
                                "governing_contract": "spec §17.4 lock and runtime identity",
                            }
                        ],
                    }
                ]
            },
            {("*", "runtime_identity"): True},
        )


def test_stale_disposition_is_rejected() -> None:
    with pytest.raises(AssertionError, match="stale"):
        validate_disposition_ledger(
            {
                "cases": [
                    {
                        "id": "full-api-only-success",
                        "fields": [
                            {
                                "field": "input_digest",
                                "mode": "exact",
                                "classification": "required",
                                "predicate": None,
                                "governing_contract": "spec §17.5 baseline authority",
                            }
                        ],
                    }
                ]
            },
            {("full-api-only-success", "input_digest"): False},
        )


def test_run_comparison_script_reports_closed_matrix() -> None:
    completed = subprocess.run(
        ["bash", str(HARNESS_ROOT / "run-comparison.sh")],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "1"
    assert payload["executed"] == 25
    assert payload["governed_passes"] == 25
    assert payload["missing"] == 0
    assert payload["extra"] == 0
    assert payload["undisposed_mismatches"] == 0
    assert tuple(payload["case_ids"]) == EXPECTED_25_CASE_IDS


def test_run_comparison_reads_only_export_roots() -> None:
    script = (HARNESS_ROOT / "run-comparison.sh").read_text(encoding="utf-8")
    runner = HARNESS_ROOT / "run_comparison.py"
    source = script
    if runner.is_file():
        source += "\n" + runner.read_text(encoding="utf-8")
    for forbidden in (
        "invocations/",
        "workflow-state.yaml",
        "assurance_agent",
        "graph_engine",
        "assurance_product",
    ):
        assert forbidden not in source


def test_matrix_harness_stays_isolated() -> None:
    imports = imported_top_level_modules(HARNESS_ROOT)
    assert "assurance_agent" not in imports
    assert "graph_engine" not in imports
    assert "assurance_product" not in imports
