import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import (
    CoverageThreshold,
    QualityGateResultV1,
    QualityGateResultV2,
    SelectedTargets,
)
from assurance_agent.workflow.execution import evidence as evidence_mod
from assurance_agent.workflow.execution.evidence import (
    EvidenceError,
    atomic_write_bytes,
    load_execution_evidence,
    publish_execution_evidence,
    publish_target_results,
)
from assurance_agent.workflow.execution.results import CoverageResult, ResultSource, TargetResult
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from tests.helpers_aa import make_report_v2, sufficient_evidence_coverage
from tests.unit.artifacts.test_models_inspect_report import (
    make_coverage,
    make_functional,
    make_quality_gate_result,
)


def make_api(
    passed: int = 2,
    failed: int = 0,
    *,
    batch_id: str = "20260715-000000",
) -> TargetResult:
    return TargetResult(
        change_id="CH-1",
        batch_id=batch_id,
        target="api",
        status="failed" if failed else "passed",
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=passed + failed,
        passed=passed,
        failed=failed,
        skipped=0,
        cases=[],
        unmapped_tests=[],
    )


def make_cov(
    available: bool = True,
    *,
    batch_id: str = "20260715-000000",
) -> CoverageResult:
    return CoverageResult(
        change_id="CH-1",
        batch_id=batch_id,
        available=available,
        line_coverage=90.0,
        branch_coverage=80.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="PASS" if available else "SKIPPED",
    )


def publish(tmp_path: Path, api: TargetResult, cov: CoverageResult | None):
    execution_dir = tmp_path / "execution"
    api_result = api
    batch_id = api.batch_id
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id=batch_id,
        api=api_result,
        e2e=None,
        coverage=cov,
        evidence_coverage=sufficient_evidence_coverage(),
    )
    manifest = publish_execution_evidence(
        execution_dir=execution_dir,
        change_id="CH-1",
        batch_id=batch_id,
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api_result,
        e2e=None,
        fuzz=None,
        coverage=cov,
        performance=None,
        quality_gate=gate,
        summary="# summary\n",
    )
    return execution_dir, manifest


def test_publish_writes_batch_dir_and_latest_pointers(tmp_path: Path) -> None:
    execution_dir, manifest = publish(tmp_path, make_api(), make_cov())
    batch_dir = execution_dir / "runs" / "20260715-000000"
    assert (batch_dir / "api-result.json").is_file()
    assert (batch_dir / "coverage-result.json").is_file()
    assert (batch_dir / "quality-gate-result.json").is_file()
    assert (batch_dir / "execution-manifest.json").is_file()
    assert (execution_dir / "api-result.json").is_file()
    assert (execution_dir / "execution-manifest.json").is_file()
    assert manifest.final_status == "PASS"
    assert manifest.result_files["api"] == "runs/20260715-000000/api-result.json"


def test_load_evidence_round_trips(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(failed=1), make_cov())
    evidence = load_execution_evidence(execution_dir)
    assert evidence.batch_id == "20260715-000000"
    assert evidence.api is not None
    assert evidence.api.failed == 1
    assert evidence.coverage is not None
    assert evidence.quality_gate is not None
    assert evidence.quality_gate.final_status == "FAIL"
    assert evidence.integrity_issues == []


def test_load_evidence_accepts_generated_nanosecond_batch_id(tmp_path: Path) -> None:
    batch_id = "20260806-222614-768233000"
    execution_dir, _ = publish(
        tmp_path,
        make_api(batch_id=batch_id),
        make_cov(batch_id=batch_id),
    )

    evidence = load_execution_evidence(execution_dir, batch_id=batch_id)

    assert evidence.batch_id == batch_id


def test_load_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError):
        load_execution_evidence(tmp_path / "execution")


def test_selected_target_with_missing_result_is_integrity_issue(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    (execution_dir / "runs" / "20260715-000000" / "api-result.json").unlink()
    evidence = load_execution_evidence(execution_dir)
    assert evidence.api is None
    assert len(evidence.integrity_issues) == 1
    assert evidence.integrity_issues[0].target == "api"


def test_load_rejects_result_path_escape(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    manifest = execution_dir / "execution-manifest.json"
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    doc["result_files"]["api"] = "../../outside.json"
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(EvidenceError, match="escapes execution directory"):
        load_execution_evidence(execution_dir)


def test_explicit_batch_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    manifest = execution_dir / "runs/20260715-000000/execution-manifest.json"
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    doc["batch_id"] = "20260715-999999"
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(EvidenceError, match="batch id mismatch"):
        load_execution_evidence(execution_dir, batch_id="20260715-000000")


def test_result_identity_mismatch_is_integrity_issue(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    result_path = execution_dir / "runs/20260715-000000/api-result.json"
    doc = json.loads(result_path.read_text(encoding="utf-8"))
    doc["batch_id"] = "20260715-999999"
    result_path.write_text(json.dumps(doc), encoding="utf-8")
    evidence = load_execution_evidence(execution_dir)
    assert any("identity mismatch" in issue.reason for issue in evidence.integrity_issues)


# --------------------------------------------------------------------------- #
# the atomic writer
# --------------------------------------------------------------------------- #


def test_the_writer_creates_the_parent_directories_it_needs(tmp_path: Path) -> None:
    """Callers name a path, not a directory tree; the repo's other atomic
    writers all create it, and a writer that does not is a landmine for the
    first caller who writes into a fresh batch dir."""
    target = tmp_path / "runs" / "20260715-000000" / "api-result.json"
    atomic_write_bytes(target, b"{}\n")
    assert target.read_bytes() == b"{}\n"


def test_the_target_still_holds_the_old_bytes_when_the_replace_is_issued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """temp-then-replace, observed instead of assumed: at the only moment the
    two files coexist, the payload is entirely in the temp file and the target
    is entirely the previous publication."""
    target = tmp_path / "doc.json"
    target.write_bytes(b"old")
    observed: dict[str, bytes] = {}
    real_replace = os.replace

    def spy(src, dst, **kwargs):  # noqa: ANN001, ANN202
        observed["target"] = Path(dst).read_bytes()
        observed["temp"] = Path(src).read_bytes()
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(evidence_mod.os, "replace", spy)
    atomic_write_bytes(target, b"new")

    assert observed == {"target": b"old", "temp": b"new"}
    assert target.read_bytes() == b"new"


def test_two_writers_of_one_path_never_share_a_temp_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temp name derived from the target alone collides, and a collision is
    not a lost temp file: the loser's replace finds nothing to rename and the
    write fails after the winner has already published."""
    target = tmp_path / "doc.json"
    seen: list[str] = []
    errors: list[BaseException] = []
    real_replace = os.replace
    both_inside = threading.Barrier(2)

    def spy(src, dst, **kwargs):  # noqa: ANN001, ANN202
        seen.append(Path(src).name)
        both_inside.wait(timeout=10)
        return real_replace(src, dst, **kwargs)

    def write_from_thread() -> None:
        try:
            atomic_write_bytes(target, b"thread")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    monkeypatch.setattr(evidence_mod.os, "replace", spy)
    worker = threading.Thread(target=write_from_thread)
    worker.start()
    try:
        atomic_write_bytes(target, b"main")
    finally:
        worker.join(timeout=10)

    assert errors == []
    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert all(str(os.getpid()) in name for name in seen)
    assert target.read_bytes() in (b"main", b"thread")


def test_a_failed_replace_reraises_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(src, dst, **kwargs):  # noqa: ANN001, ANN202
        raise OSError("replace refused")

    monkeypatch.setattr(evidence_mod.os, "replace", refuse)
    with pytest.raises(OSError, match="replace refused"):
        atomic_write_bytes(tmp_path / "doc.json", b"new")

    assert list(tmp_path.iterdir()) == [], "a half-written temp file is evidence nobody can read"


def test_a_failed_cleanup_does_not_mask_the_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator needs to know the publication failed, not that the tidying
    up afterwards did."""

    def refuse_replace(src, dst, **kwargs):  # noqa: ANN001, ANN202
        raise OSError("replace refused")

    def refuse_unlink(self, missing_ok: bool = False) -> None:  # noqa: ANN001
        raise OSError("unlink refused")

    monkeypatch.setattr(evidence_mod.os, "replace", refuse_replace)
    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    with pytest.raises(OSError, match="replace refused"):
        atomic_write_bytes(tmp_path / "doc.json", b"new")


# --------------------------------------------------------------------------- #
# when the top-level latest copies may move
# --------------------------------------------------------------------------- #

_POINTERS = ("api-result.json", "coverage-result.json", "summary.md", "quality-gate-result.json")
_STALE = b"previous batch"


def _publish_with_write_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    api: TargetResult,
    cov: CoverageResult | None,
) -> tuple[Path, list[tuple[str, dict[str, bytes | None]]]]:
    """Publish one batch over a previous one, snapshotting every write.

    Each log entry pairs the path about to be written with the bytes of every
    top-level latest copy at that instant, so "nothing moved before the
    manifest" is an assertion about observed filesystem states.
    """
    execution_dir = tmp_path / "execution"
    execution_dir.mkdir(parents=True)
    for name in _POINTERS:
        (execution_dir / name).write_bytes(_STALE)

    log: list[tuple[str, dict[str, bytes | None]]] = []
    real_write = evidence_mod.atomic_write_bytes

    def recorder(path: Path, payload: bytes) -> None:
        snapshot: dict[str, bytes | None] = {
            name: (execution_dir / name).read_bytes() if (execution_dir / name).is_file() else None
            for name in _POINTERS
        }
        log.append((path.relative_to(execution_dir).as_posix(), snapshot))
        real_write(path, payload)

    monkeypatch.setattr(evidence_mod, "atomic_write_bytes", recorder)
    publish(tmp_path, api, cov)
    return execution_dir, log


def test_phase_one_publishes_batch_scoped_results_only(tmp_path: Path) -> None:
    """Phase 1 runs before the gate exists, so anything it wrote at the top
    level would advertise a batch no manifest yet names."""
    execution_dir = tmp_path / "execution"
    execution_dir.mkdir(parents=True)
    (execution_dir / "api-result.json").write_bytes(_STALE)

    result_files = publish_target_results(
        execution_dir=execution_dir,
        batch_id="20260715-000000",
        api=make_api(),
        e2e=None,
        fuzz=None,
        coverage=make_cov(),
        performance=None,
    )

    assert result_files["api"] == "runs/20260715-000000/api-result.json"
    assert (execution_dir / "runs/20260715-000000/api-result.json").is_file()
    assert (execution_dir / "api-result.json").read_bytes() == _STALE
    assert not (execution_dir / "coverage-result.json").exists()


def test_no_latest_copy_moves_before_the_manifest_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The top-level manifest is the authoritative marker. Until it names this
    batch, every latest copy beside it must still describe the previous one."""
    _, log = _publish_with_write_log(tmp_path, monkeypatch, api=make_api(), cov=make_cov())

    paths = [rel for rel, _ in log]
    manifest_at = paths.index("execution-manifest.json")
    for rel, snapshot in log[: manifest_at + 1]:
        assert snapshot == dict.fromkeys(_POINTERS, _STALE), f"a latest copy moved while writing {rel}"


def test_the_latest_copies_are_byte_copies_of_the_batch_files_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copied rather than re-serialized, so the pointer and the archive it
    points at cannot drift apart."""
    execution_dir, log = _publish_with_write_log(tmp_path, monkeypatch, api=make_api(), cov=make_cov())

    batch_dir = execution_dir / "runs" / "20260715-000000"
    for name in _POINTERS:
        assert (execution_dir / name).read_bytes() == (batch_dir / name).read_bytes()

    paths = [rel for rel, _ in log]
    assert paths.index("execution-manifest.json") < min(paths.index(name) for name in _POINTERS)


def test_an_unselected_targets_latest_copy_is_removed_after_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that ran no coverage must not leave the previous batch's
    coverage standing as current — but it may only stop doing so once its own
    manifest is on disk."""
    execution_dir, log = _publish_with_write_log(tmp_path, monkeypatch, api=make_api(), cov=None)

    paths = [rel for rel, _ in log]
    manifest_at = paths.index("execution-manifest.json")
    assert all(snapshot["coverage-result.json"] == _STALE for _, snapshot in log[: manifest_at + 1])
    assert not (execution_dir / "coverage-result.json").exists()
    assert not (execution_dir / "runs/20260715-000000/coverage-result.json").exists()


def test_a_reused_batch_does_not_promote_a_result_the_manifest_omits(tmp_path: Path) -> None:
    """The pointer set comes from `result_files`, not from whatever is lying in
    the batch directory.

    `generate_batch_id` is second-resolution, so two runs of one change can share
    a batch id, and a rerun that selects fewer targets then finds the previous
    run's result files still sitting in its own batch directory. Promoting those
    would present evidence as current that the manifest does not even list.
    """
    _, first = publish(tmp_path, make_api(), make_cov())
    execution_dir = tmp_path / "execution"
    batch_dir = execution_dir / "runs" / "20260715-000000"
    assert "coverage" in first.result_files
    assert (execution_dir / "coverage-result.json").is_file()

    _, second = publish(tmp_path, make_api(passed=3), None)

    assert "coverage" not in second.result_files
    assert (batch_dir / "coverage-result.json").is_file(), "the stale batch file is the premise here"
    assert not (execution_dir / "coverage-result.json").exists()
    assert (execution_dir / "api-result.json").read_bytes() == (batch_dir / "api-result.json").read_bytes()
    assert json.loads((execution_dir / "api-result.json").read_text(encoding="utf-8"))["passed"] == 3


def test_no_batch_document_is_written_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two writes of one artifact in a run are two byte states a reader could
    observe; the split publisher exists to avoid the second."""
    _, log = _publish_with_write_log(tmp_path, monkeypatch, api=make_api(), cov=make_cov())

    batch_writes = [rel for rel, _ in log if rel.startswith("runs/")]
    assert sorted(batch_writes) == sorted(set(batch_writes))


def _quality_doc(version: str) -> dict:
    if version == "1.0":
        return make_quality_gate_result(
            change_id="CH-1",
            batch_id="20260715-000000",
            dimensions={"functional": make_functional(), "coverage": make_coverage()},
            final_status="PASS",
        )
    coverage = {
        **make_coverage(),
        "evidence": {"kind": "sufficiency", "report": make_report_v2(verdicts=[])},
    }
    return {
        "schema_version": "2.0",
        "change_id": "CH-1",
        "batch_id": "20260715-000000",
        "dimensions": {"functional": make_functional(), "coverage": coverage},
        "final_status": "PASS",
    }


def write_execution_fixture(tmp_path: Path, *, quality_version: str) -> Path:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    gate_path = execution_dir / "runs" / "20260715-000000" / "quality-gate-result.json"
    gate_path.write_text(json.dumps(_quality_doc(quality_version)), encoding="utf-8")
    return execution_dir


@pytest.mark.parametrize("version", ["1.0", "2.0"])
def test_execution_evidence_loads_concrete_quality_variant(
    tmp_path: Path,
    version: str,
) -> None:
    write_execution_fixture(tmp_path, quality_version=version)
    loaded = load_execution_evidence(tmp_path / "execution")
    expected = QualityGateResultV1 if version == "1.0" else QualityGateResultV2
    assert isinstance(loaded.quality_gate, expected)


def test_execution_evidence_rejects_unknown_quality_version(tmp_path: Path) -> None:
    execution_dir = write_execution_fixture(tmp_path, quality_version="1.0")
    gate_path = execution_dir / "runs" / "20260715-000000" / "quality-gate-result.json"
    doc = json.loads(gate_path.read_text(encoding="utf-8"))
    doc["schema_version"] = "99"
    gate_path.write_text(json.dumps(doc), encoding="utf-8")
    loaded = load_execution_evidence(execution_dir)
    assert loaded.quality_gate is None


def test_execution_evidence_rejects_invalid_utf8_quality(tmp_path: Path) -> None:
    execution_dir = write_execution_fixture(tmp_path, quality_version="1.0")
    gate_path = execution_dir / "runs" / "20260715-000000" / "quality-gate-result.json"
    gate_path.write_bytes(b"\xff\xfe{not-utf8")
    loaded = load_execution_evidence(execution_dir)
    assert loaded.quality_gate is None
