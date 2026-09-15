from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from typing import cast

import pytest
from assurance_execution.contracts.agent import RunTestsInputV1
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_execution.operations.common import InputError
from assurance_execution.operations.normalize import normalize_evidence
from assurance_execution.operations.runner import (
    ProcessReceipt,
    build_pytest_argv,
    run_closed_mapping,
    runner_environment,
    write_canonical_evidence,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_product.execution_view import build_execution_view, discard_execution_view
from assurance_product.generated_merge import (
    GeneratedFileV2,
    GeneratedOperation,
    TestFamily,
    merge_generated,
)

CHANGE_ID = "CH-DEMO-001"
BATCH_ID = "20260822T000000Z"
CANDIDATE_TARGET = "qa/tests/api/test_users.py"
UNSELECTED_TARGET = "qa/tests/api/test_legacy.py"


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _write(project: Path, relative: str, content: bytes) -> Path:
    path = project.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _promote(project: Path, target: str, content: bytes) -> GeneratedFileV2:
    staged = target if target.startswith("qa/tests/") else f"qa/tests/{target}"
    path = _write(project, staged, content)
    digest = _digest(content)
    _write(
        project,
        "qa/results/codegen/api-generated-files.json",
        json.dumps(
            {
                "schema_version": "1",
                "change_id": CHANGE_ID,
                "layer": "api",
                "files": [
                    {
                        "target_path": target,
                        "disposition": "generated",
                        "role": "test_entry",
                        "case_ids": ["TC_A"],
                        "content_sha256": digest,
                    }
                ],
            }
        ).encode("utf-8"),
    )
    return GeneratedFileV2(
        target_path=target,
        staged_path=staged,
        sha256=digest,
        mode=stat.S_IMODE(path.stat().st_mode),
        operation=cast(GeneratedOperation, "generated"),
        family=cast(TestFamily, "api"),
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "qa").mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    (project / "app").mkdir(parents=True)
    _write(project, "app/main.py", b"APP = 1\n")
    return project


def _run_payload(selected: list[str]) -> dict[str, object]:
    return {
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "plan_digest": "d" * 64,
        "plan_ref": {
            "path": f"qa/results/plan/{'d' * 64}/resolved-assurance-plan.json",
            "digest": "e" * 64,
        },
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "mapping": {
            "selected": selected,
            "mappings": [
                {
                    "test": item,
                    "case_id": "TC_A",
                    "capability": "entities.item.create",
                    "layer": "api",
                }
                for item in selected
            ],
        },
        "capability_leafs": ["auth.session.create", "entities.item.create"],
        "case_ids": ["TC_A", "TC_B"],
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
    }


class RecordingHost:
    def __init__(self, *, crash: bool = False) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.cwds: list[Path] = []
        self.crash = crash

    def spawn(self, argv: tuple[str, ...], cwd: Path) -> ProcessReceipt:
        self.commands.append(argv)
        self.cwds.append(cwd)
        if self.crash:
            raise InputError("runner crashed before writing a report")
        selected = tuple(
            _canonical_selector(item) for item in argv[1:] if not item.startswith("-") and "::" in item
        )
        tests = [
            {
                "nodeid": item if "::" in item else f"{item}::test_ok",
                "outcome": "passed",
                "call": {"outcome": "passed", "duration": 0.001, "longrepr": ""},
            }
            for item in selected
        ]
        return ProcessReceipt(
            command=argv,
            exit_code=0,
            stdout="",
            stderr="",
            report={
                "tests": tests,
                "exitcode": 0,
                "summary": {
                    "collected": len(selected),
                    "passed": len(selected),
                    "failed": 0,
                    "skipped": 0,
                },
            },
        )


def test_unselected_closed_mapping_tests_are_not_executed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, UNSELECTED_TARGET, b"def test_legacy():\n    assert True\n")
    _promote(project, CANDIDATE_TARGET, b"def test_ok():\n    assert True\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    selected = (f"{CANDIDATE_TARGET}::test_ok",)
    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=selected,
    )
    host = RecordingHost()

    output = run_closed_mapping(
        RunTestsInputV1.model_validate(_run_payload(list(selected))),
        project,
        host,
        include_pr_metrics=False,
    )

    assert output["executed"] == list(selected)
    assert UNSELECTED_TARGET not in " ".join(host.commands[0])
    view_unselected = "tests/" + UNSELECTED_TARGET[len("qa/tests/") :]
    assert not project.joinpath(*view.root.split("/"), *view_unselected.split("/")).exists()


def test_pytest_cache_and_hypothesis_storage_stay_outside_the_sut(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote(project, CANDIDATE_TARGET, b"def test_ok():\n    assert True\n")
    merge_generated(project, CHANGE_ID, ("api",))
    selected = (f"{CANDIDATE_TARGET}::test_ok",)
    host = RecordingHost()

    run_closed_mapping(
        RunTestsInputV1.model_validate(_run_payload(list(selected))),
        project,
        host,
        include_pr_metrics=False,
    )

    argv = host.commands[0]
    joined = " ".join(argv)
    env = runner_environment(BATCH_ID)
    assert any(item.startswith("-o=pythonpath=") and item.endswith("/qa") for item in argv)
    assert not any("--rootdir=" in item and ".staging/execution" in item for item in argv)
    assert CANDIDATE_TARGET in argv or f"{CANDIDATE_TARGET}::test_ok" in argv
    assert "-p" in argv and "no:cacheprovider" in argv
    assert str(project) in joined
    assert env["HYPOTHESIS_STORAGE_DIRECTORY"] == f"/tmp/aa-hypothesis-{BATCH_ID}"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert ".pytest_cache" not in joined
    assert str(project / ".hypothesis") not in joined
    assert not (project / ".pytest_cache").exists()
    assert not (project / ".hypothesis").exists()


def test_runner_crash_leaves_view_disposable_and_writes_no_canonical_evidence(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote(project, CANDIDATE_TARGET, b"def test_ok():\n    assert True\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    selected = (f"{CANDIDATE_TARGET}::test_ok",)
    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=selected,
    )
    host = RecordingHost(crash=True)

    with pytest.raises(InputError, match="crashed"):
        run_closed_mapping(
            RunTestsInputV1.model_validate(_run_payload(list(selected))),
            project,
            host,
            include_pr_metrics=False,
        )

    assert project.joinpath(*view.root.split("/")).is_dir()
    assert not (project / "qa" / "results/execution").exists()
    discard_execution_view(project, view)
    assert not project.joinpath(*view.root.split("/")).exists()


def test_application_imports_stay_on_the_original_sut(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote(project, CANDIDATE_TARGET, b"def test_ok():\n    assert True\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    selected = (f"{CANDIDATE_TARGET}::test_ok",)
    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=selected,
    )

    argv = build_pytest_argv(
        selected,
        rootdir=project.joinpath(*view.root.split("/")),
        project_root=project,
        batch_id=BATCH_ID,
    )

    joined = " ".join(argv)
    assert (
        f"--rootdir={project.joinpath(*view.root.split('/'))}" in joined
        or str(project.joinpath(*view.root.split("/"))) in argv
    )
    assert str(project) in joined
    assert "app/main.py" not in joined
    assert not project.joinpath(*view.root.split("/"), "app").exists()


def test_canonical_evidence_is_written_only_after_validated_output(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote(project, CANDIDATE_TARGET, b"def test_ok():\n    assert True\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    selected = (f"{CANDIDATE_TARGET}::test_ok",)
    build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=selected,
    )
    host = RecordingHost()

    output = run_closed_mapping(
        RunTestsInputV1.model_validate(_run_payload(list(selected))),
        project,
        host,
        include_pr_metrics=False,
    )

    evidence = project / "qa" / "results/execution" / "execute-result.json"
    assert evidence.is_file()
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["batch_id"] == BATCH_ID
    assert payload["mapping_digest"] == output["mapping_digest"]
    assert (project / "app" / "main.py").read_bytes() == b"APP = 1\n"


def _canonical_selector(item: str) -> str:
    marker = "/.staging/execution/"
    if marker not in item:
        return item
    remainder = item.split(marker, 1)[1]
    parts = remainder.split("/", 1)
    return parts[1] if len(parts) == 2 else item


class LivePytestHost:
    def spawn(self, argv: tuple[str, ...], cwd: Path) -> ProcessReceipt:
        public = tuple(
            item
            for item in argv
            if not item.startswith("--assurance-batch-id=")
            and item != "--json-report"
            and not item.startswith("--json-report-file=")
        )
        completed = subprocess.run(  # noqa: S603
            list(public),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        selected = tuple(
            _canonical_selector(item)
            for item in public[1:]
            if not item.startswith("-") and ("::" in item or item.endswith(".py"))
        )
        failed = completed.returncode != 0
        outcome = "failed" if failed else "passed"
        tests = [
            {
                "nodeid": item if "::" in item else f"{item}::test_ok",
                "outcome": outcome,
                "call": {
                    "outcome": outcome,
                    "duration": 0.001,
                    "longrepr": (completed.stdout + completed.stderr) if failed else "",
                },
            }
            for item in selected
        ]
        return ProcessReceipt(
            command=argv,
            exit_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            report={
                "tests": tests,
                "exitcode": int(completed.returncode),
                "summary": {
                    "collected": len(selected),
                    "passed": 0 if failed else len(selected),
                    "failed": len(selected) if failed else 0,
                    "skipped": 0,
                },
            },
        )


def test_real_pytest_runs_shadowed_view_file_not_failing_sut(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sut = _write(project, "tests/api/test_users.py", b'def test_ok():\n    assert False, "SUT_RAN"\n')
    _promote(project, CANDIDATE_TARGET, b"def test_ok():\n    assert True\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    selected = (f"{CANDIDATE_TARGET}::test_ok",)
    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=selected,
    )

    output = run_closed_mapping(
        RunTestsInputV1.model_validate(_run_payload(list(selected))),
        project,
        LivePytestHost(),
        include_pr_metrics=False,
    )

    evidence = output["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["status"] == "passed"
    assert "SUT_RAN" not in json.dumps(output)
    assert sut.read_text(encoding="utf-8") == 'def test_ok():\n    assert False, "SUT_RAN"\n'
    shadowed = project.joinpath(*view.root.split("/"), "tests", "api", "test_users.py")
    assert shadowed.read_text(encoding="utf-8") == "def test_ok():\n    assert True\n"


def _closed_evidence(change_id: str) -> ExecutionEvidenceV1:
    selected = [f"{CANDIDATE_TARGET}::test_ok"]
    leafs = frozenset({"auth.session.create", "entities.item.create"})
    cases = frozenset({"TC_A", "TC_B"})
    mapping = ClosedMappingV1.model_validate(
        {
            "selected": selected,
            "mappings": [
                {
                    "test": selected[0],
                    "case_id": "TC_A",
                    "capability": "entities.item.create",
                    "layer": "api",
                }
            ],
        },
        context={"capability_leafs": leafs, "case_ids": cases},
    )
    return normalize_evidence(
        change_id=change_id,
        batch_id=BATCH_ID,
        plan_digest="d" * 64,
        plan_ref=EvidenceArtifactRefV1(
            path=f"qa/results/plan/{'d' * 64}/resolved-assurance-plan.json",
            digest="e" * 64,
        ),
        selected_targets={"api": True, "e2e": False, "fuzz": False, "performance": False},
        mapping=mapping,
        capability_leafs=leafs,
        case_ids=cases,
        baseline_tree_id="b" * 64,
        runner_profile_digest="c" * 64,
        command=("pytest",),
        exit_code=0,
        report={
            "tests": [
                {
                    "nodeid": selected[0],
                    "outcome": "passed",
                    "call": {"outcome": "passed", "duration": 0.001, "longrepr": ""},
                }
            ],
            "exitcode": 0,
            "summary": {"collected": 1, "passed": 1, "failed": 0, "skipped": 0},
        },
    )


def test_canonical_evidence_change_id_cannot_escape_the_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "qa").mkdir(parents=True)
    outside = tmp_path / "outside"

    with pytest.raises((InputError, ValueError)):
        write_canonical_evidence(project, _closed_evidence("../../outside"))
    with pytest.raises((InputError, ValueError)):
        write_canonical_evidence(project, _closed_evidence(".."))

    assert not outside.exists()
    assert list(project.rglob("execute-result.json")) == []
    assert list(tmp_path.rglob("execute-result.json")) == []
