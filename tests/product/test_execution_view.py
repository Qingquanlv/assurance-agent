from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
from typing import cast
from pydantic import ValidationError

from assurance_product.generated_merge import (
    GeneratedFileV2,
    GeneratedOperation,
    TestFamily,
    merge_generated,
)
from assurance_product.execution_view import ExecutionView, build_execution_view, discard_execution_view
from assurance_execution.generated_merge import ExecutionViewInputV1

CHANGE_ID = "CH-DEMO-001"
BATCH_ID = "20260822T000000Z"
CANDIDATE_TARGET = "tests/api/test_users.py"
EXISTING_TARGET = "tests/api/test_existing.py"
SUPPORT_TARGET = "tests/conftest.py"
APP_SOURCE = "app/main.py"


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _staged_path(family: str, target: str) -> str:
    return f"qa/changes/{CHANGE_ID}/generated/{family}/files/{target}"


def _write(project: Path, relative: str, content: bytes) -> Path:
    path = project.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _promote(project: Path, family: str, target: str, content: bytes) -> GeneratedFileV2:
    path = _write(project, _staged_path(family, target), content)
    digest = _digest(content)
    _write(
        project,
        f"qa/changes/{CHANGE_ID}/codegen/{family}-generated-files.json",
        json.dumps(
            {
                "schema_version": "1",
                "change_id": CHANGE_ID,
                "layer": family,
                "files": [
                    {
                        "target_path": target,
                        "disposition": "generated",
                        "role": "test_entry",
                        "case_ids": [f"TC_{family.upper()}_001"],
                        "content_sha256": digest,
                    }
                ],
            }
        ).encode("utf-8"),
    )
    return GeneratedFileV2(
        target_path=target,
        staged_path=_staged_path(family, target),
        sha256=digest,
        mode=stat.S_IMODE(path.stat().st_mode),
        operation=cast(GeneratedOperation, "generated"),
        family=cast(TestFamily, family),
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "qa" / "changes" / CHANGE_ID).mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    (project / "app").mkdir(parents=True)
    return project


def _selected(*targets: str) -> tuple[str, ...]:
    return tuple(f"{target}::test_ok" if "::" not in target else target for target in targets)


def test_candidate_files_shadow_existing_tests_in_the_disposable_view(tmp_path: Path) -> None:
    project = _project(tmp_path)
    original = _write(project, CANDIDATE_TARGET, b"original-sut\n")
    _write(project, APP_SOURCE, b"APP = 1\n")
    _promote(project, "api", CANDIDATE_TARGET, b"generated-candidate\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))

    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=_selected(CANDIDATE_TARGET),
    )

    assert isinstance(view, ExecutionView)
    assert view.batch_id == BATCH_ID
    assert view.root == f"qa/changes/{CHANGE_ID}/.staging/execution/{BATCH_ID}"
    assert view.selected_targets == _selected(CANDIDATE_TARGET)
    shadowed = project.joinpath(*view.root.split("/"), *CANDIDATE_TARGET.split("/"))
    assert shadowed.read_bytes() == b"generated-candidate\n"
    assert original.read_bytes() == b"original-sut\n"
    assert not (project.joinpath(*view.root.split("/")) / "app").exists()


def test_unchanged_existing_tests_remain_selected(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, EXISTING_TARGET, b"def test_ok():\n    assert True\n")
    _write(project, SUPPORT_TARGET, b"import pytest\n")
    _promote(project, "api", CANDIDATE_TARGET, b"generated-candidate\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    selected = _selected(CANDIDATE_TARGET, EXISTING_TARGET)

    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=selected,
    )

    assert view.selected_targets == selected
    existing = project.joinpath(*view.root.split("/"), *EXISTING_TARGET.split("/"))
    support = project.joinpath(*view.root.split("/"), *SUPPORT_TARGET.split("/"))
    assert existing.read_bytes() == b"def test_ok():\n    assert True\n"
    assert support.read_bytes() == b"import pytest\n"


def test_authenticated_python_support_is_projected_without_unselected_tests(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    expected_support = {
        "tests/__init__.py": b"",
        "tests/config.py": b"BASE_URL = 'http://example.test'\n",
        "tests/schema_validation.py": b"def validate(value): return value\n",
        "tests/testdata/domain/dept.py": b"DEPT_ID = 1\n",
        "tests/api/adapters/dept.py": b"def fetch(): return 1\n",
        "tests/api/conftest.py": b"import pytest\n",
    }
    for relative, payload in expected_support.items():
        _write(project, relative, payload)
    _write(project, "tests/api/test_unselected.py", b"raise AssertionError('must not collect')\n")
    _write(project, "tests/perf/locustfile_unselected.py", b"raise AssertionError('must not run')\n")
    _write(project, "tests/__pycache__/config.cpython-311.pyc", b"runtime noise\n")
    _promote(project, "api", CANDIDATE_TARGET, b"generated-candidate\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))

    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=_selected(CANDIDATE_TARGET),
    )

    root = project.joinpath(*view.root.split("/"))
    for relative, payload in expected_support.items():
        assert root.joinpath(*relative.split("/")).read_bytes() == payload
    assert not (root / "tests/api/test_unselected.py").exists()
    assert not (root / "tests/perf/locustfile_unselected.py").exists()
    assert not (root / "tests/__pycache__").exists()


def test_execution_view_digest_authenticates_python_support(tmp_path: Path) -> None:
    project = _project(tmp_path)
    support = _write(project, "tests/api/adapters/dept.py", b"DEPT_ID = 1\n")
    _promote(project, "api", CANDIDATE_TARGET, b"generated-candidate\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))

    first = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id="first",
        merged=merged,
        selected=_selected(CANDIDATE_TARGET),
    )
    support.write_bytes(b"DEPT_ID = 2\n")
    second = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id="second",
        merged=merged,
        selected=_selected(CANDIDATE_TARGET),
    )

    assert first.digest != second.digest


def test_application_source_is_not_copied_into_the_view(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, APP_SOURCE, b"SECRET = 1\n")
    _write(project, "src/pkg/__init__.py", b"VALUE = 2\n")
    _promote(project, "api", CANDIDATE_TARGET, b"generated-candidate\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))

    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=_selected(CANDIDATE_TARGET),
    )

    root = project.joinpath(*view.root.split("/"))
    assert not (root / "app").exists()
    assert not (root / "src").exists()
    assert list(root.rglob("main.py")) == []
    assert (project / "app" / "main.py").read_bytes() == b"SECRET = 1\n"


def test_conflicts_block_view_creation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote(project, "api", CANDIDATE_TARGET, b"generated-candidate\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    occupied = project / "qa" / "changes" / CHANGE_ID / ".staging" / "execution" / BATCH_ID
    occupied.mkdir(parents=True)
    (occupied / "occupied.txt").write_text("stale\n", encoding="utf-8")

    with pytest.raises(ValueError, match="conflict"):
        build_execution_view(
            project,
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            merged=merged,
            selected=_selected(CANDIDATE_TARGET),
        )


def test_cleanup_does_not_delete_canonical_evidence(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote(project, "api", CANDIDATE_TARGET, b"generated-candidate\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=_selected(CANDIDATE_TARGET),
    )
    evidence = _write(
        project,
        f"qa/changes/{CHANGE_ID}/execution/execute-result.json",
        json.dumps({"status": "passed"}).encode("utf-8"),
    )

    discard_execution_view(project, view)

    assert not project.joinpath(*view.root.split("/")).exists()
    assert evidence.is_file()
    assert json.loads(evidence.read_text(encoding="utf-8")) == {"status": "passed"}


def test_failed_materialization_does_not_leave_a_blocking_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    _promote(project, "api", CANDIDATE_TARGET, b"generated-candidate\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))

    def boom(self: Path, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", boom)
    with pytest.raises(ValueError, match="could not materialize"):
        build_execution_view(
            project,
            change_id=CHANGE_ID,
            batch_id=BATCH_ID,
            merged=merged,
            selected=_selected(CANDIDATE_TARGET),
        )
    monkeypatch.undo()

    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=_selected(CANDIDATE_TARGET),
    )
    assert view.root == f"qa/changes/{CHANGE_ID}/.staging/execution/{BATCH_ID}"
    shadowed = project.joinpath(*view.root.split("/"), *CANDIDATE_TARGET.split("/"))
    assert shadowed.read_bytes() == b"generated-candidate\n"


def test_execution_view_digest_covers_selected_materialized_files(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote(project, "api", CANDIDATE_TARGET, b"generated-candidate\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))

    view = build_execution_view(
        project,
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        merged=merged,
        selected=_selected(CANDIDATE_TARGET),
    )

    assert view.digest
    assert view.digest != merged.digest
    assert isinstance(view.digest, str)


def test_verified_view_is_execution_scoped_and_read_only(tmp_path: Path) -> None:
    project = _project(tmp_path)
    original = _write(project, SUPPORT_TARGET, b"import pytest\n")
    original.chmod(0o640)
    _promote(project, "api", CANDIDATE_TARGET, b"def test_ok():\n    assert True\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    execution_id = "01234567-89ab-4def-8123-456789abcdef"
    request = merged.execution_view_input(
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        execution_id=execution_id,
        selected=(f"{CANDIDATE_TARGET}::test_ok[param=one]",),
    )

    view = build_execution_view(project, request=request)

    assert view.execution_id == execution_id
    assert view.mode == "verified"
    assert view.root.endswith(f"/{BATCH_ID}/{execution_id}")
    root = project.joinpath(*view.root.split("/"))
    assert stat.S_IMODE(root.stat().st_mode) == 0o555
    assert stat.S_IMODE((root / CANDIDATE_TARGET).stat().st_mode) == 0o444
    assert stat.S_IMODE((root / SUPPORT_TARGET).stat().st_mode) == 0o444
    assert stat.S_IMODE(original.stat().st_mode) == 0o640


def test_verified_view_input_authenticates_generated_descriptors(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote(project, "api", CANDIDATE_TARGET, b"def test_ok():\n    assert True\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    request = merged.execution_view_input(
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        execution_id="01234567-89ab-4def-8123-456789abcdef",
        selected=(f"{CANDIDATE_TARGET}::test_ok",),
    )
    staged = project.joinpath(*merged.files[0].staged_path.split("/"))
    staged.write_text("def test_ok():\n    assert False\n", encoding="utf-8")

    with pytest.raises(ValueError, match="descriptor"):
        build_execution_view(project, request=request)


def test_verified_view_input_is_closed_and_distinguishes_real_reruns(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _promote(project, "api", CANDIDATE_TARGET, b"def test_ok():\n    assert True\n")
    merged = merge_generated(project, CHANGE_ID, ("api",))
    first_request = merged.execution_view_input(
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        execution_id="01234567-89ab-4def-8123-456789abcdef",
        selected=(f"{CANDIDATE_TARGET}::test_ok[one]",),
    )
    second_request = merged.execution_view_input(
        change_id=CHANGE_ID,
        batch_id=BATCH_ID,
        execution_id="11234567-89ab-4def-8123-456789abcdef",
        selected=(f"{CANDIDATE_TARGET}::test_ok[two]",),
    )

    first = build_execution_view(project, request=first_request)
    second = build_execution_view(project, request=second_request)

    assert first.root != second.root
    assert first.digest != second.digest
    with pytest.raises(ValidationError):
        ExecutionViewInputV1.model_validate({**first_request.model_dump(mode="json"), "passed": True})
