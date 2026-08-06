"""Task 8: the runner's injected trace fold, `executed_at`, and shadow sufficiency.

The invariant these tests exist for is P0-1: the projection the runner folds
*before* the manifest exists must be byte-identical to the projection anyone
folds *after* it is published. That only holds if the runner publishes the
current batch's result JSONs and computes its per-file test digests before it
folds, and then publishes exactly the injected `executed_at` /
`test_files_sha256` into the manifest — so the ordering is asserted directly,
not inferred from the parity check alone.

Everything the shadow evaluation *concludes* is written to
`QualityGateResult.diagnostics["evidence_sufficiency"]`; a failure to conclude
anything also appends one warning. No test here may see either move
`final_status`, and the failure diagnostics must be reproducible — this gate
document is copied verbatim into `inspect/` and into eval fixtures, so an
absolute path recorded here travels with it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models import ExecutionManifest, QualityGateResult
from assurance_agent.artifacts.models.policy import EvidenceSufficiency, Policy
from assurance_agent.artifacts.policy import PolicyError
from assurance_agent.config import AaConfig, ConfigNotFoundError
from assurance_agent.exceptions import AaError
from assurance_agent.evidence.sufficiency import EvidenceCoverageErrorCode
from assurance_agent.evidence.trace import fold_trace
from assurance_agent.workflow.execution import runner as runner_mod
from assurance_agent.workflow.execution import runners as runners_mod
from assurance_agent.workflow.execution.runner import run_change
from assurance_agent.workflow.execution.tree_hash import hash_test_tree
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-RUN-TRACE-001"
BATCH_ID = "20260805-010203"
# Microseconds are deliberate: they are what a naive ISO round-trip through YAML
# silently drops. The offset here is zero; a non-zero one is exercised
# separately by `test_a_non_utc_offset_survives_publication_unshifted`.
EXECUTED_AT = datetime(2026, 8, 5, 1, 2, 3, 456789, tzinfo=UTC)
SHANGHAI = timezone(timedelta(hours=8))

PROJECTION_REL = f"runs/{BATCH_ID}/trace-projection.json"


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #


def make_config() -> AaConfig:
    return AaConfig.model_validate(
        {
            "version": 1,
            "sources": {"frontend": "./frontend", "backend": "./backend"},
            "qa": {"cases": "./qa/cases", "changes": "./qa/changes"},
            "tests": {"root": "./tests", "api": "./tests/api", "e2e": "./tests/e2e"},
            "frameworks": {
                "api": {"enabled": True, "name": "pytest"},
                "e2e": {"enabled": True, "name": "playwright"},
            },
            "generation": {"prd_input_mode": "prompt", "e2e": {"default_pom": False}},
            "execution": {"entry": "cli", "self_healing": {"mode": "proposal-only"}},
            "coverage": {"enabled": False, "gate_mode": "warn", "threshold": {"line": 70, "branch": 60}},
            "performance": {"enabled": False},
        }
    )


def stub_pytest_run(outcome_by_target: dict[str, str]):
    """subprocess.run stub keyed by which target dir appears in argv."""

    def fake_run(args, **kwargs):
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        target = "api" if "tests/api" in args else "e2e" if "tests/e2e" in args else "fuzz"
        outcome = outcome_by_target.get(target, "passed")
        tests = [
            {
                "nodeid": f"tests/{target}/t.py::test_tc_{target}_001__x",
                "outcome": outcome,
                "call": {
                    "outcome": outcome,
                    "duration": 0.0,
                    "longrepr": "" if outcome == "passed" else "AssertionError: boom",
                },
            }
        ]
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": tests}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return fake_run


def stub_unmapped_run(outcome_by_target: dict[str, str]):
    """As `stub_pytest_run`, plus one test whose name carries no case id.

    That is what makes `build_quality_gate` raise a warning of its own, so the
    shadow warning has something to be appended after.
    """
    inner = stub_pytest_run(outcome_by_target)

    def fake_run(args, **kwargs):
        completed = inner(args, **kwargs)
        report_file = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file=")))
        document = json.loads(report_file.read_text(encoding="utf-8"))
        document["tests"].append(
            {
                "nodeid": "tests/api/t.py::test_orphan",
                "outcome": "passed",
                "call": {"outcome": "passed", "duration": 0.0, "longrepr": ""},
            }
        )
        report_file.write_text(json.dumps(document), encoding="utf-8")
        return completed

    return fake_run


def _case(case_id: str, case_type: str, *, required: bool = True) -> dict[str, object]:
    return {
        "case_id": case_id,
        "module": "system.core",
        "type": case_type,
        "assertions": ["an assertion"],
        "automation": {"required": required},
    }


def _project(tmp_path: Path, *, extra_cases: list[dict[str, object]] | None = None) -> Path:
    """A minimal SUT: config, two mapped test files, one change with two cases."""
    write_aa_config(tmp_path)
    for target in ("api", "e2e"):
        target_dir = tmp_path / "tests" / target
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "t.py").write_text(
            f"def test_tc_{target}_001__x():\n    assert True\n", encoding="utf-8"
        )

    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: true\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    cases = [_case("TC_API_001", "API"), _case("TC_E2E_001", "E2E"), *(extra_cases or [])]
    case_path = change_dir / "cases" / "system" / "core" / "case.yaml"
    case_path.parent.mkdir(parents=True)
    case_path.write_text(
        yaml.safe_dump(
            {"schema_version": "1.0", "added": cases, "modified": [], "removed": []}, sort_keys=False
        ),
        encoding="utf-8",
    )
    return change_dir


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: BATCH_ID)
    monkeypatch.setattr(runner_mod, "generate_executed_at", lambda: EXECUTED_AT)
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "passed", "e2e": "passed"}))


def _gate_on_disk(change_dir: Path) -> QualityGateResult:
    path = change_dir / "execution" / "quality-gate-result.json"
    return QualityGateResult.model_validate_json(path.read_text(encoding="utf-8"))


def _shadow(change_dir: Path) -> dict[str, object]:
    diagnostics = _gate_on_disk(change_dir).diagnostics
    assert diagnostics is not None, "the gate must carry diagnostics"
    shadow = diagnostics["evidence_sufficiency"]
    assert isinstance(shadow, dict)
    return shadow


# --------------------------------------------------------------------------- #
# P0-1: injected fold == on-disk fold, byte for byte
# --------------------------------------------------------------------------- #


def test_the_published_projection_is_byte_identical_to_a_later_on_disk_fold(
    tmp_path: Path, pinned: None
) -> None:
    """P0-1, proved against the real manifest writer.

    The bytes on the left were folded with `current=ExecutionFoldInput(...)`
    before any manifest existed; the bytes on the right come from
    `fold_trace(current=None)` reading the manifest the runner actually
    published. Anything the writer drops — a null `executed_at`, an omitted
    `test_files_sha256` — changes the logical view's digest and fails here.
    """
    change_dir = _project(tmp_path)
    run_change(tmp_path, change_dir, make_config())

    published = (change_dir / "execution" / PROJECTION_REL).read_bytes()
    on_disk = canonical_json_bytes(fold_trace(tmp_path, CHANGE_ID, phase="execution", current=None))
    assert published == on_disk


def test_the_on_disk_fold_reports_no_gaps_and_complete_integrity(tmp_path: Path, pinned: None) -> None:
    """Parity would also hold if both folds were equally broken; this pins that
    the shared projection is the *correct* one — results present, digests
    agreeing with the tree, manifest readable."""
    change_dir = _project(tmp_path)
    run_change(tmp_path, change_dir, make_config())

    projection = fold_trace(tmp_path, CHANGE_ID, phase="execution", current=None)
    assert projection.gaps == ()
    assert projection.integrity == "complete"
    assert projection.authoritative_batch_id == BATCH_ID
    assert [row.presence_in_current_batch for row in projection.rows] == ["executed", "executed"]


def test_the_projection_is_published_once_under_the_batch_dir(tmp_path: Path, pinned: None) -> None:
    change_dir = _project(tmp_path)
    run_change(tmp_path, change_dir, make_config())

    path = change_dir / "execution" / PROJECTION_REL
    assert path.is_file()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == "1"
    assert document["change_id"] == CHANGE_ID
    assert document["phase"] == "execution"


# --------------------------------------------------------------------------- #
# writer ordering (what makes P0-1 reachable at all)
# --------------------------------------------------------------------------- #


def test_the_current_batch_results_are_published_before_the_fold(
    tmp_path: Path, pinned: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fold must see the batch's result JSONs and *not* the manifest.

    Publishing results after the fold would make the injected projection carry
    `result_missing` gaps the on-disk fold does not have; publishing the manifest
    before the fold would make the injected mode pointless.
    """
    change_dir = _project(tmp_path)
    real_fold = runner_mod.fold_trace
    seen: list[list[str]] = []

    def spy(*args, **kwargs):
        batch_dir = change_dir / "execution" / "runs" / BATCH_ID
        seen.append(sorted(p.name for p in batch_dir.iterdir()) if batch_dir.is_dir() else [])
        return real_fold(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "fold_trace", spy)
    run_change(tmp_path, change_dir, make_config())

    assert len(seen) == 1, "the runner folds exactly once per batch"
    at_fold_time = seen[0]
    assert "api-result.json" in at_fold_time
    assert "e2e-result.json" in at_fold_time
    assert "execution-manifest.yaml" not in at_fold_time
    assert "quality-gate-result.json" not in at_fold_time


def test_publication_is_split_into_results_then_manifest(
    tmp_path: Path, pinned: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One publisher call per phase, in this order.

    The pre-Task-8 publisher wrote results *and* the manifest in a single call
    after the gate; folding before that would have needed a second write of the
    same result JSONs. Splitting it is what removes the duplicate write, so the
    split is asserted rather than assumed.
    """
    change_dir = _project(tmp_path)
    calls: list[str] = []
    real_results = runner_mod.publish_target_results
    real_manifest = runner_mod.publish_execution_manifest

    def results_spy(**kwargs):
        calls.append("results")
        return real_results(**kwargs)

    def manifest_spy(**kwargs):
        calls.append("manifest")
        return real_manifest(**kwargs)

    monkeypatch.setattr(runner_mod, "publish_target_results", results_spy)
    monkeypatch.setattr(runner_mod, "publish_execution_manifest", manifest_spy)
    run_change(tmp_path, change_dir, make_config())

    assert calls == ["results", "manifest"]


# --------------------------------------------------------------------------- #
# one aware executed_at, shared by injection, shadow and manifest
# --------------------------------------------------------------------------- #


def test_the_manifest_publishes_the_aware_executed_at(tmp_path: Path, pinned: None) -> None:
    change_dir = _project(tmp_path)
    manifest = run_change(tmp_path, change_dir, make_config())

    assert manifest.executed_at == EXECUTED_AT
    assert manifest.executed_at is not None
    assert manifest.executed_at.utcoffset() is not None

    document = yaml.safe_load(
        (change_dir / "execution" / "execution-manifest.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(document["executed_at"], str)
    reloaded = ExecutionManifest.model_validate(document)
    assert reloaded.executed_at == EXECUTED_AT


def test_one_executed_at_value_reaches_the_manifest_and_the_shadow(tmp_path: Path, pinned: None) -> None:
    """`ExecutionFoldInput.executed_at`, the sufficiency `as_of` and the
    manifest field are one value, not three clock reads."""
    change_dir = _project(tmp_path)
    manifest = run_change(tmp_path, change_dir, make_config())

    report = _shadow(change_dir)["report"]
    assert isinstance(report, dict)
    assert datetime.fromisoformat(str(report["as_of"])) == EXECUTED_AT == manifest.executed_at


def test_the_published_test_file_digests_are_the_ones_folded(tmp_path: Path, pinned: None) -> None:
    change_dir = _project(tmp_path)
    manifest = run_change(tmp_path, change_dir, make_config())

    assert manifest.test_files_sha256 == hash_test_tree(tmp_path).files
    assert manifest.test_files_sha256, "a non-null mapping is a P0-1 precondition"
    projection = fold_trace(tmp_path, CHANGE_ID, phase="execution", current=None)
    assert [gap.code for gap in projection.gaps if gap.code == "tests_tree_digest_mismatch"] == []


def test_the_runner_reads_the_clock_once_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    change_dir = _project(tmp_path)
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: BATCH_ID)
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "passed", "e2e": "passed"}))
    calls: list[datetime] = []

    def counting() -> datetime:
        calls.append(EXECUTED_AT)
        return EXECUTED_AT

    monkeypatch.setattr(runner_mod, "generate_executed_at", counting)
    run_change(tmp_path, change_dir, make_config())
    assert len(calls) == 1


def test_generate_executed_at_is_timezone_aware() -> None:
    assert runner_mod.generate_executed_at().utcoffset() is not None


# --------------------------------------------------------------------------- #
# model surface
# --------------------------------------------------------------------------- #


def test_the_manifest_rejects_a_naive_executed_at() -> None:
    with pytest.raises(ValueError, match="executed_at"):
        ExecutionManifest.model_validate(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": BATCH_ID,
                "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
                "result_files": {},
                "executed_at": "2026-08-05T01:02:03",
            }
        )


def test_a_legacy_manifest_without_executed_at_still_loads() -> None:
    """Task 3's batch-id UTC approximation stays reachable for old batches."""
    manifest = ExecutionManifest.model_validate(
        {
            "schema_version": "1.0",
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
            "result_files": {},
        }
    )
    assert manifest.executed_at is None


def test_quality_gate_diagnostics_default_to_absent() -> None:
    gate = QualityGateResult.model_validate_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": BATCH_ID,
                "dimensions": {
                    "functional": {
                        "status": "PASS",
                        "api": {"total": 1, "passed": 1, "failed": 0},
                        "e2e": {"total": 0, "passed": 0, "failed": 0},
                    },
                    "coverage": {
                        "status": "SKIPPED",
                        "available": False,
                        "line_coverage": 0.0,
                        "branch_coverage": 0.0,
                        "threshold": {"line": 70, "branch": 60},
                    },
                },
                "final_status": "PASS",
            }
        )
    )
    assert gate.diagnostics is None


# --------------------------------------------------------------------------- #
# shadow only
# --------------------------------------------------------------------------- #


def test_the_diagnostics_carry_the_whole_sufficiency_report(tmp_path: Path, pinned: None) -> None:
    change_dir = _project(tmp_path)
    run_change(tmp_path, change_dir, make_config())

    shadow = _shadow(change_dir)
    assert shadow["error_code"] is None
    report = shadow["report"]
    assert isinstance(report, dict)
    assert report["change_id"] == CHANGE_ID
    assert report["recency_hours"] == 72
    # Task 7: consumers must route integrity before `sufficient`, so it must be
    # persisted with the verdicts rather than left behind in the projection.
    assert report["integrity"] == "complete"
    assert [row["case_id"] for row in report["rows"]] == ["TC_API_001", "TC_E2E_001"]
    assert all(row["sufficient"] for row in report["rows"])
    assert shadow["projection"] == PROJECTION_REL


def test_an_insufficient_row_does_not_move_the_final_status(tmp_path: Path, pinned: None) -> None:
    """An uncovered, never-run API case is insufficient under the packaged
    policy (`require_human`). The gate must still report the functional truth."""
    change_dir = _project(tmp_path, extra_cases=[_case("TC_API_002", "API")])
    manifest = run_change(tmp_path, change_dir, make_config())

    report = _shadow(change_dir)["report"]
    assert isinstance(report, dict)
    verdict = next(row for row in report["rows"] if row["case_id"] == "TC_API_002")
    assert verdict["sufficient"] is False
    assert verdict["reason_codes"] == ["not_covered", "never_run"]

    assert manifest.final_status == "PASS"
    assert _gate_on_disk(change_dir).final_status == "PASS"
    assert _gate_on_disk(change_dir).dimensions.coverage.status == "SKIPPED"


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ({"api": "passed", "e2e": "passed"}, "PASS"),
        ({"api": "failed", "e2e": "passed"}, "FAIL"),
        ({"api": "passed", "e2e": "failed"}, "FAIL"),
    ],
)
def test_the_final_status_is_the_pre_shadow_verdict_case_by_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[str, str],
    expected: str,
) -> None:
    change_dir = _project(tmp_path, extra_cases=[_case("TC_API_002", "API")])
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: BATCH_ID)
    monkeypatch.setattr(runner_mod, "generate_executed_at", lambda: EXECUTED_AT)
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run(outcomes))

    manifest = run_change(tmp_path, change_dir, make_config())
    assert manifest.final_status == expected
    assert _gate_on_disk(change_dir).final_status == expected
    # The shadow ran (so this is not a vacuous pass) and still changed nothing.
    assert _shadow(change_dir)["error_code"] is None


def test_a_broken_policy_is_recorded_as_policy_error_without_failing_the_run(
    tmp_path: Path, pinned: None
) -> None:
    change_dir = _project(tmp_path)
    policy = tmp_path / ".aa" / "policy.yaml"
    policy.write_text("version: 1\nevidence_sufficiency:\n  recency_hours: -5\n", encoding="utf-8")

    manifest = run_change(tmp_path, change_dir, make_config())

    shadow = _shadow(change_dir)
    assert shadow["error_code"] == "policy_error"
    assert shadow["report"] is None
    assert manifest.final_status == "PASS"
    # The projection is a fact and is still published; only adjudication stops.
    assert (change_dir / "execution" / PROJECTION_REL).is_file()


def test_an_unfoldable_change_is_recorded_without_failing_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `.aa/config.yaml` means the fold cannot resolve the change directory.
    The run must still publish its evidence; the shadow just says why it is
    absent."""
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: false\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "api").mkdir(parents=True)
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: BATCH_ID)
    monkeypatch.setattr(runner_mod, "generate_executed_at", lambda: EXECUTED_AT)
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "passed"}))

    manifest = run_change(tmp_path, change_dir, make_config())

    assert manifest.executed_at == EXECUTED_AT
    shadow = _shadow(change_dir)
    assert shadow["error_code"] == "evidence_projection_missing"
    assert shadow["report"] is None
    assert not (change_dir / "execution" / PROJECTION_REL).exists()


# --------------------------------------------------------------------------- #
# shadow failures: visible, reproducible, and still not the verdict
# --------------------------------------------------------------------------- #

BROKEN_POLICY = "version: 1\nevidence_sufficiency:\n  recency_hours: -5\n"
# Two widenings over the Task 8 wording, both about what an operator would
# otherwise conclude wrongly:
#
# - "*quality* gate verdict is unaffected" — since M1 there is a second gate, the
#   independent `trace-sufficiency-gate`, and it *does* route on these facts at
#   the change level. "The gate verdict is unaffected" now reads as a claim about
#   both, which would be false.
# - "or the projection was published and then refused" —
#   `evidence_projection_missing` also covers a projection that exists on disk and
#   was rejected by the evaluation (contradictory row facts, a naive timestamp).
#   An operator told only "could not be projected" goes looking for a missing file
#   that is right there.
SHADOW_WARNING = {
    "policy_error": (
        "EVIDENCE-SUFFICIENCY-NOT-EVALUATED: the evidence sufficiency policy could not be "
        "loaded or applied (error_code=policy_error). The quality gate verdict is unaffected; "
        "the independent trace-sufficiency gate routes on the change-level facts; see "
        "diagnostics.evidence_sufficiency."
    ),
    "evidence_projection_missing": (
        "EVIDENCE-SUFFICIENCY-NOT-EVALUATED: this change's evidence could not be projected, or "
        "the projection was published and then refused (error_code=evidence_projection_missing). "
        "The quality gate verdict is unaffected; the independent trace-sufficiency gate routes on "
        "the change-level facts; see diagnostics.evidence_sufficiency."
    ),
}


def _unfoldable_project(tmp_path: Path) -> Path:
    """A change with no `.aa/config.yaml`, so the fold cannot resolve it."""
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: false\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "api").mkdir(parents=True)
    return change_dir


def _run_with_shadow_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str, *, unmapped: bool = False
) -> Path:
    """Run one batch whose shadow evaluation fails with `error_code`."""
    if error_code == "policy_error":
        change_dir = _project(tmp_path)
        (tmp_path / ".aa" / "policy.yaml").write_text(BROKEN_POLICY, encoding="utf-8")
        targets = {"api": "passed", "e2e": "passed"}
    else:
        change_dir = _unfoldable_project(tmp_path)
        targets = {"api": "passed"}

    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: BATCH_ID)
    monkeypatch.setattr(runner_mod, "generate_executed_at", lambda: EXECUTED_AT)
    stub = stub_unmapped_run(targets) if unmapped else stub_pytest_run(targets)
    monkeypatch.setattr(runners_mod.subprocess, "run", stub)
    run_change(tmp_path, change_dir, make_config())
    return change_dir


@pytest.mark.parametrize("error_code", ["policy_error", "evidence_projection_missing"])
def test_a_shadow_failure_appends_one_warning_the_operator_can_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """Diagnostics are for machines. A shadow evaluation that never ran is
    invisible unless the gate says so where humans already look."""
    change_dir = _run_with_shadow_error(tmp_path, monkeypatch, error_code)

    gate = _gate_on_disk(change_dir)
    assert _shadow(change_dir)["error_code"] == error_code
    assert gate.warnings == [SHADOW_WARNING[error_code]]


@pytest.mark.parametrize("error_code", ["policy_error", "evidence_projection_missing"])
def test_the_shadow_warning_does_not_make_the_verdict_pass_with_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """`warnings` is a free-text list, not an input to the status ladder.

    `PASS_WITH_WARNINGS` is a verdict about the *tests* — a dimension that
    degraded — and the shadow evaluation is not a dimension. A warning here says
    "this was not judged", which is a gap in the observation rather than a fault
    in the change, so the verdict stays exactly what the functional and coverage
    dimensions made it.
    """
    change_dir = _run_with_shadow_error(tmp_path, monkeypatch, error_code)

    gate = _gate_on_disk(change_dir)
    assert gate.warnings, "the premise of this test is that a warning was raised"
    assert gate.final_status == "PASS"
    assert gate.dimensions.functional.status == "PASS"


@pytest.mark.parametrize("error_code", ["policy_error", "evidence_projection_missing"])
def test_the_shadow_warning_names_nothing_machine_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """The warning is keyed on the error code alone: exception text carries
    absolute paths, which turn a shared artifact into a machine's diary."""
    change_dir = _run_with_shadow_error(tmp_path, monkeypatch, error_code)

    warnings = _gate_on_disk(change_dir).warnings or []
    assert len(warnings) == 1
    assert str(tmp_path) not in warnings[0]


def test_a_shadow_failure_leaves_the_verdict_byte_for_byte_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical runs, one with a policy that cannot be applied. Everything
    the gate says apart from its evidence channels must be the same document.

    Task 9 widened those channels from one to two: the evaluation is reported in
    `diagnostics` and again in `dimensions.coverage.evidence`, so both are
    removed here. `warnings` goes too, because a failed evaluation appends one.
    """
    broken = _run_with_shadow_error(tmp_path, monkeypatch, "policy_error")
    healthy_root = tmp_path / "healthy"
    healthy_root.mkdir()
    healthy = _project(healthy_root)
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "passed", "e2e": "passed"}))
    run_change(healthy_root, healthy, make_config())

    def without_shadow(change_dir: Path) -> dict[str, object]:
        document = json.loads((change_dir / "execution" / "quality-gate-result.json").read_text("utf-8"))
        document.pop("diagnostics")
        document.pop("warnings")
        document["dimensions"]["coverage"].pop("evidence")
        return document

    assert _shadow(broken)["error_code"] == "policy_error"
    assert _shadow(healthy)["error_code"] is None
    assert without_shadow(broken) == without_shadow(healthy)
    assert _gate_on_disk(healthy).warnings is None


def test_the_shadow_warning_is_appended_after_the_gates_own_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is one more warning, not a replacement for the ones the gate raised."""
    change_dir = _run_with_shadow_error(tmp_path, monkeypatch, "policy_error", unmapped=True)

    warnings = _gate_on_disk(change_dir).warnings or []
    assert len(warnings) == 2
    assert warnings[0].startswith("TRACEABILITY-BROKEN:")
    assert warnings[1] == SHADOW_WARNING["policy_error"]


@pytest.mark.parametrize("error_code", ["policy_error", "evidence_projection_missing"])
def test_error_diagnostics_name_nothing_machine_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """`str(exc)` embeds the absolute path of whatever failed to load. The gate
    is copied verbatim into `inspect/` and into eval fixtures, so that path
    travels; the stable shape is the error code plus the exception's type."""
    change_dir = _run_with_shadow_error(tmp_path, monkeypatch, error_code)

    shadow = _shadow(change_dir)
    assert shadow["error_code"] == error_code
    assert "detail" not in shadow
    assert isinstance(shadow["error_type"], str)
    assert shadow["error_type"].isidentifier(), "a bare exception class name, not a message"
    assert str(tmp_path) not in json.dumps(shadow)


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [("policy_error", PolicyError), ("evidence_projection_missing", ConfigNotFoundError)],
)
def test_each_failure_records_the_domain_class_that_raised_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str, expected: type[Exception]
) -> None:
    """The recorded type is pinned to the class, not merely to "some identifier".

    Both are `AaError` subclasses, and that is the reason the value is worth
    recording: `load_policy` catches pydantic's `ValidationError` and re-raises
    `PolicyError`, so what reaches the runner is a domain class this repo owns
    and can keep stable — not a library class whose name and shape belong to a
    dependency's release cycle.
    """
    change_dir = _run_with_shadow_error(tmp_path, monkeypatch, error_code)

    assert issubclass(expected, AaError)
    assert _shadow(change_dir)["error_type"] == expected.__name__


def test_the_same_failure_diagnoses_identically_from_a_different_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproducibility is the point: two checkouts of the same change failing
    the same way must produce the same diagnostics bytes, or no reviewer can
    compare them."""
    first = _run_with_shadow_error(tmp_path / "one", monkeypatch, "policy_error")
    second = _run_with_shadow_error(tmp_path / "two", monkeypatch, "policy_error")

    assert canonical_json_bytes(_shadow(first)) == canonical_json_bytes(_shadow(second))


# --------------------------------------------------------------------------- #
# offsets other than UTC
# --------------------------------------------------------------------------- #


def test_a_non_utc_offset_survives_publication_unshifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An aware value is only worth having if the offset survives the writer.
    A round-trip through `datetime.isoformat()` on a naive local value, or a
    normalising `astimezone(UTC)`, both pass a UTC-only test and fail here."""
    executed_at = datetime(2026, 8, 5, 9, 2, 3, 456789, tzinfo=SHANGHAI)
    change_dir = _project(tmp_path)
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: BATCH_ID)
    monkeypatch.setattr(runner_mod, "generate_executed_at", lambda: executed_at)
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "passed", "e2e": "passed"}))

    manifest = run_change(tmp_path, change_dir, make_config())

    assert manifest.executed_at == executed_at
    text = (change_dir / "execution" / "execution-manifest.yaml").read_text(encoding="utf-8")
    assert "+08:00" in text, "the offset is published as written, not normalised to Z"
    reloaded = ExecutionManifest.model_validate(yaml.safe_load(text))
    assert reloaded.executed_at is not None
    assert reloaded.executed_at.utcoffset() == timedelta(hours=8)
    assert reloaded.executed_at == executed_at

    published = (change_dir / "execution" / PROJECTION_REL).read_bytes()
    on_disk = canonical_json_bytes(fold_trace(tmp_path, CHANGE_ID, phase="execution", current=None))
    assert published == on_disk


# --------------------------------------------------------------------------- #
# Task 9 (revised): one typed evaluation, reported through two channels
#
# The runner no longer assembles a diagnostics dict by hand. It builds an
# `EvidenceCoverageEvaluation` — the object Task 11's `trace-sufficiency-gate`
# will consume — and serializes that one object into both places the gate
# document reports it: `diagnostics.evidence_sufficiency` and
# `dimensions.coverage.evidence`. `action` is the policy's `on_insufficient` as
# written, because the runner concludes nothing from it.
# --------------------------------------------------------------------------- #

EVALUATION_KEYS = ("report", "action", "error_code")


def _policy_text(on_insufficient: str) -> str:
    return "\n".join(
        [
            "version: 1",
            "human_review_risk_levels: [high, critical]",
            "force_continue_allowed: true",
            "plan_checks:",
            "  l1_path: warn",
            "  shared_factory: warn",
            "  assert_ideal: warn",
            "  capability_keys: warn",
            "coverage_floor:",
            "  risk_high: 0.9",
            "  risk_medium: 0.7",
            "fuzz:",
            "  required_when_endpoint_has_auth: true",
            "healing:",
            "  auth_module: require_human",
            "evidence_sufficiency:",
            "  recency_hours: 72",
            "  required_kinds:",
            "    API: [covered, execution_recent]",
            "    E2E: [covered, execution_recent]",
            "    Fuzz: [covered, fuzz_run]",
            "    Performance: [covered, perf_run]",
            f"  on_insufficient: {on_insufficient}",
            "",
        ]
    )


def _coverage_evidence(change_dir: Path) -> dict[str, object]:
    evidence = _gate_on_disk(change_dir).dimensions.coverage.evidence
    assert evidence is not None, "the coverage dimension must report the evaluation"
    return evidence


def test_every_error_code_has_exactly_one_operator_warning() -> None:
    """The warning table is closed over the error vocabulary, both ways.

    A code with no warning is silent: the diagnostics would record that nothing
    was judged and no operator would ever see it. A warning keyed on a code that
    no longer exists is dead text that outlives the failure it described. Pinning
    the two sets equal makes a third `EvidenceCoverageErrorCode` red here rather
    than shipping quietly.
    """
    assert set(runner_mod._SHADOW_WARNINGS) == set(get_args(EvidenceCoverageErrorCode))
    assert set(SHADOW_WARNING) == set(get_args(EvidenceCoverageErrorCode))
    assert runner_mod._SHADOW_WARNINGS == SHADOW_WARNING


# --------------------------------------------------------------------------- #
# which input was unusable: the two codes are not interchangeable
#
# `policy_error` and `evidence_projection_missing` are the vocabulary a consumer
# dispatches on, so they have to name the input that failed rather than the line
# that happened to raise. Before this split, every exception after a successful
# fold was labelled `policy_error` — including a projection whose own facts
# contradict each other, which `evaluate_sufficiency` is the first thing to
# notice. An operator reading `policy_error` would then go and audit a policy
# that was fine.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        # The projection's own facts are unusable: `_reject_contradictory_facts`
        # (a row whose two automation facts disagree) and a naive timestamp
        # inside a row both surface here, and neither is the policy's fault.
        (
            ValueError("TC_API_001: coverage_state='not_required' contradicts automation_required=True"),
            "evidence_projection_missing",
        ),
        (TypeError("TC_API_001.latest_execution.ts must be timezone-aware"), "evidence_projection_missing"),
        # The policy cannot say what this row's case type requires. Reachable only
        # for a policy that bypassed `load_policy`'s validators, which is exactly
        # when fail-closed labelling matters.
        (
            KeyError("policy.evidence_sufficiency.required_kinds has no entry for case type 'API'"),
            "policy_error",
        ),
    ],
    ids=("contradictory-row", "naive-row-timestamp", "policy-missing-case-type"),
)
def test_the_evaluator_failure_is_attributed_to_the_input_that_was_unusable(
    tmp_path: Path, pinned: None, monkeypatch: pytest.MonkeyPatch, raised: Exception, expected: str
) -> None:
    change_dir = _project(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(runner_mod, "evaluate_sufficiency", boom)
    manifest = run_change(tmp_path, change_dir, make_config())

    shadow = _shadow(change_dir)
    assert shadow["error_code"] == expected
    assert shadow["report"] is None and shadow["action"] is None
    assert manifest.final_status == "PASS", "a shadow failure may not fail the run"


def test_a_projection_the_evaluator_rejects_still_records_where_it_was_published(
    tmp_path: Path, pinned: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`evidence_projection_missing` here does not mean "no projection exists" —
    the fold succeeded and published one. The path is recorded so a reviewer can
    read the very bytes the evaluator refused."""
    change_dir = _project(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("contradictory row")

    monkeypatch.setattr(runner_mod, "evaluate_sufficiency", boom)
    run_change(tmp_path, change_dir, make_config())

    shadow = _shadow(change_dir)
    assert shadow["error_code"] == "evidence_projection_missing"
    assert shadow["projection"] == PROJECTION_REL
    assert (change_dir / "execution" / PROJECTION_REL).is_file()


def test_an_unfoldable_change_records_no_projection_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same code: nothing was folded, so there is no path.
    The two `evidence_projection_missing` cases stay distinguishable."""
    change_dir = _run_with_shadow_error(tmp_path, monkeypatch, "evidence_projection_missing")

    shadow = _shadow(change_dir)
    assert shadow["error_code"] == "evidence_projection_missing"
    assert shadow["projection"] is None


def test_a_policy_action_outside_the_vocabulary_is_a_policy_error(
    tmp_path: Path, pinned: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building the evaluation is inside the guarded region, not after it.

    `EvidenceCoverageEvaluation` validates `action` fail-closed, so a policy that
    bypassed pydantic's literal — `model_construct`, an imported document, a
    future loader bug — raises at construction. Outside the guard that exception
    would propagate and fail a whole test run over an observation.
    """
    change_dir = _project(tmp_path)
    real = runner_mod.load_policy

    def bypassed(project_root: Path) -> Policy:
        policy = real(project_root)
        return policy.model_copy(
            update={
                "evidence_sufficiency": EvidenceSufficiency.model_construct(
                    **{
                        **policy.evidence_sufficiency.model_dump(),
                        "on_insufficient": "maybe",
                    }
                )
            }
        )

    monkeypatch.setattr(runner_mod, "load_policy", bypassed)
    manifest = run_change(tmp_path, change_dir, make_config())

    shadow = _shadow(change_dir)
    assert shadow["error_code"] == "policy_error"
    assert shadow["error_type"] == "ValueError"
    assert shadow["report"] is None and shadow["action"] is None
    assert manifest.final_status == "PASS"
    assert _gate_on_disk(change_dir).warnings == [SHADOW_WARNING["policy_error"]]


def test_an_unloadable_policy_is_still_a_policy_error(tmp_path: Path, pinned: None) -> None:
    """The classification that already held, kept explicit next to the split so a
    future edit cannot collapse the two paths back together."""
    change_dir = _project(tmp_path)
    (tmp_path / ".aa" / "policy.yaml").write_text(BROKEN_POLICY, encoding="utf-8")

    run_change(tmp_path, change_dir, make_config())

    shadow = _shadow(change_dir)
    assert shadow["error_code"] == "policy_error"
    assert shadow["error_type"] == "PolicyError"


def test_the_diagnostics_carry_the_policy_action_beside_the_report(tmp_path: Path, pinned: None) -> None:
    """The packaged default is `require_human`, and it is recorded verbatim.

    A stored report is uninterpretable without it: the same verdicts call for a
    different disposition under `warn`, and a consumer reading the gate document
    months later cannot recover which policy produced them.
    """
    change_dir = _project(tmp_path)
    run_change(tmp_path, change_dir, make_config())

    assert _shadow(change_dir)["action"] == "require_human"


@pytest.mark.parametrize("on_insufficient", ["warn", "block", "require_human"])
def test_the_recorded_action_is_the_projects_own_policy_value(
    tmp_path: Path, pinned: None, on_insufficient: str
) -> None:
    change_dir = _project(tmp_path)
    (tmp_path / ".aa" / "policy.yaml").write_text(_policy_text(on_insufficient), encoding="utf-8")

    run_change(tmp_path, change_dir, make_config())

    shadow = _shadow(change_dir)
    assert shadow["action"] == on_insufficient
    assert shadow["error_code"] is None


@pytest.mark.parametrize("error_code", ["policy_error", "evidence_projection_missing"])
def test_a_shadow_failure_records_no_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """An evaluation that never ran has no policy disposition to report; a
    defaulted one would let a consumer route on a value nobody chose."""
    change_dir = _run_with_shadow_error(tmp_path, monkeypatch, error_code)

    shadow = _shadow(change_dir)
    assert shadow["action"] is None
    assert shadow["report"] is None
    assert shadow["error_code"] == error_code


def test_the_diagnostics_always_carry_the_evaluations_three_keys(tmp_path: Path, pinned: None) -> None:
    """Absent-vs-null must not be a distinction a reader has to make; the
    observation-channel facts (`projection`, `error_type`) are additional."""
    change_dir = _project(tmp_path)
    run_change(tmp_path, change_dir, make_config())

    shadow = _shadow(change_dir)
    assert set(EVALUATION_KEYS) <= set(shadow)
    assert shadow["projection"] == PROJECTION_REL


def test_the_coverage_dimension_reports_the_same_evaluation_as_the_diagnostics(
    tmp_path: Path, pinned: None
) -> None:
    """Two copies of one object in one document, from one serializer, so they
    cannot drift. The diagnostics copy adds where the projection was written and
    what stopped the evaluation — facts about this run, not about the verdict."""
    change_dir = _project(tmp_path)
    run_change(tmp_path, change_dir, make_config())

    shadow = _shadow(change_dir)
    assert _coverage_evidence(change_dir) == {key: shadow[key] for key in EVALUATION_KEYS}
    assert "projection" not in _coverage_evidence(change_dir)


@pytest.mark.parametrize("error_code", ["policy_error", "evidence_projection_missing"])
def test_the_coverage_dimension_reports_a_failed_evaluation_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    change_dir = _run_with_shadow_error(tmp_path, monkeypatch, error_code)

    assert _coverage_evidence(change_dir) == {
        "report": None,
        "action": None,
        "error_code": error_code,
    }


@pytest.mark.parametrize("on_insufficient", ["warn", "block", "require_human"])
def test_a_reported_insufficiency_moves_no_verdict_under_any_policy(
    tmp_path: Path, pinned: None, on_insufficient: str
) -> None:
    """The revised architecture, end to end through the real writer.

    `TC_API_002` is uncovered and never run, so it is insufficient under every
    action. The gate still reports the execution's own truth, and the coverage
    dimension keeps the status its line/branch inputs gave it — `SKIPPED` here,
    because coverage is disabled in this config.
    """
    change_dir = _project(tmp_path, extra_cases=[_case("TC_API_002", "API")])
    (tmp_path / ".aa" / "policy.yaml").write_text(_policy_text(on_insufficient), encoding="utf-8")

    manifest = run_change(tmp_path, change_dir, make_config())

    evidence = _coverage_evidence(change_dir)
    report = evidence["report"]
    assert isinstance(report, dict)
    assert [row["case_id"] for row in report["rows"] if not row["sufficient"]] == ["TC_API_002"]
    assert evidence["action"] == on_insufficient

    gate = _gate_on_disk(change_dir)
    assert manifest.final_status == "PASS"
    assert gate.final_status == "PASS"
    assert gate.dimensions.coverage.status == "SKIPPED"
    assert gate.warnings is None


def test_reported_evidence_changes_no_other_byte_of_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runs of the same batch inputs, one whose change carries an
    insufficient case. Everything outside the two evidence channels — including
    every status — must be the same document."""
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: BATCH_ID)
    monkeypatch.setattr(runner_mod, "generate_executed_at", lambda: EXECUTED_AT)
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "passed", "e2e": "passed"}))

    sufficient_root = tmp_path / "sufficient"
    sufficient_root.mkdir()
    sufficient = _project(sufficient_root)
    insufficient_root = tmp_path / "insufficient"
    insufficient_root.mkdir()
    insufficient = _project(insufficient_root, extra_cases=[_case("TC_API_002", "API")])
    run_change(sufficient_root, sufficient, make_config())
    run_change(insufficient_root, insufficient, make_config())

    def without_evidence(change_dir: Path) -> bytes:
        document = json.loads((change_dir / "execution" / "quality-gate-result.json").read_text("utf-8"))
        document.pop("diagnostics")
        document["dimensions"]["coverage"].pop("evidence")
        return canonical_json_bytes(document)

    def insufficient_case_ids(change_dir: Path) -> list[str]:
        report = _shadow(change_dir)["report"]
        assert isinstance(report, dict)
        return [row["case_id"] for row in report["rows"] if not row["sufficient"]]

    assert insufficient_case_ids(sufficient) == []
    assert insufficient_case_ids(insufficient) == ["TC_API_002"], "the premise: the evidence differs"
    assert without_evidence(sufficient) == without_evidence(insufficient)


def test_the_diagnostics_survive_a_json_round_trip(tmp_path: Path, pinned: None) -> None:
    """Diagnostics are persisted artifact content, so they must reload through
    the model rather than only exist in memory."""
    change_dir = _project(tmp_path)
    run_change(tmp_path, change_dir, make_config())

    batch_copy = (change_dir / "execution" / "runs" / BATCH_ID / "quality-gate-result.json").read_text(
        encoding="utf-8"
    )
    latest_copy = (change_dir / "execution" / "quality-gate-result.json").read_text(encoding="utf-8")
    assert batch_copy == latest_copy
    assert QualityGateResult.model_validate_json(batch_copy).diagnostics is not None
