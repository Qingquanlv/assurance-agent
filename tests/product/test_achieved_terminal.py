from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

CHANGE_ID = "CH-DEMO-001"
TARGET = "tests/api/test_users.py"
_SHA = "a" * 64
_PUBLICATION_STATES = ("not_ready", "ready", "published", "drifted")


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _write(project: Path, relative: str, content: bytes) -> Path:
    path = project.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _staged_path(family: str, target: str) -> str:
    return f"qa/changes/{CHANGE_ID}/generated/{family}/files/{target}"


def _promote(project: Path, family: str, target: str, content: bytes) -> None:
    _write(project, _staged_path(family, target), content)
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


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "qa" / "changes" / CHANGE_ID).mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    return project


def valid_status(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "invocation_id": "inv-achieved-001",
        "lock_digest": _SHA,
        "root_input_digest": _SHA,
        "status": "completed",
        "entrypoint": "full",
        "graph_hierarchy": (),
        "node_states": (),
        "selected_test_families": ("api",),
        "coverage_progress": None,
        "durable_effects": (),
        "adapter_evidence": (),
        "pending_interrupt": None,
        "terminal_reason": None,
        "change": {"change_id": CHANGE_ID, "state": "achieved"},
        "apply": {"manifest_digest": None, "file_count": 0},
        "publication": {"status": "not_ready"},
    }
    payload.update(overrides)
    return payload


def _ready_change(tmp_path: Path, *, execution_status: str = "passed") -> Path:
    project = _project(tmp_path)
    _promote(project, "api", TARGET, b"generated-candidate\n")
    (project / TARGET).write_bytes(b"original-sut\n")
    _write(
        project,
        f"qa/changes/{CHANGE_ID}/execution/execute-result.json",
        json.dumps({"status": execution_status}).encode("utf-8"),
    )
    _write(
        project,
        f"qa/changes/{CHANGE_ID}/inspect/inspection.json",
        json.dumps(
            {
                "coverage": {
                    "measured": 1.0,
                    "threshold": 0.9,
                    "rounds_used": 1,
                    "rounds_budget": 1,
                    "decision": True,
                }
            }
        ).encode("utf-8"),
    )
    _write(project, f"qa/changes/{CHANGE_ID}/report/report.md", b"# report\n")
    return project


def test_status_v1_rejects_tree_fields():
    from assurance_product.models import StatusV1

    with pytest.raises(ValidationError):
        StatusV1.model_validate(valid_status(initial_tree_id=_SHA))
    with pytest.raises(ValidationError):
        StatusV1.model_validate(valid_status(current_head_tree_id=_SHA))


@pytest.mark.parametrize("state", _PUBLICATION_STATES)
def test_status_v1_accepts_closed_publication_status(state: str):
    from assurance_product.models import StatusV1

    value = StatusV1.model_validate(valid_status(publication={"status": state}))
    assert value.publication.status == state


def test_status_v1_rejects_unknown_publication_status():
    from assurance_product.models import StatusV1

    with pytest.raises(ValidationError):
        StatusV1.model_validate(valid_status(publication={"status": "exported"}))
    with pytest.raises(ValidationError):
        StatusV1.model_validate({key: value for key, value in valid_status().items() if key != "publication"})


def test_finalize_achieved_writes_status_and_apply_manifest(tmp_path: Path):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    original = (project / TARGET).read_bytes()

    status = finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status())

    assert status.change.change_id == CHANGE_ID
    assert status.change.state == "achieved"
    assert status.publication.status == "ready"
    assert status.apply.file_count == 1
    assert status.apply.manifest_digest is not None
    status_path = project / "qa" / "changes" / CHANGE_ID / "status.json"
    manifest_path = project / "qa" / "changes" / CHANGE_ID / "apply-manifest.json"
    assert status_path.is_file()
    assert manifest_path.is_file()
    written_status = json.loads(status_path.read_text(encoding="utf-8"))
    written_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written_status["publication"]["status"] == "ready"
    assert written_status["change"]["state"] == "achieved"
    assert written_manifest["change_id"] == CHANGE_ID
    assert written_manifest["files"][0]["target_path"] == TARGET
    assert written_manifest["files"][0]["source_path"] == _staged_path("api", TARGET)
    assert written_manifest["files"][0]["source_sha256"] == _digest(b"generated-candidate\n")
    assert written_manifest["files"][0]["baseline_sha256"] == _digest(b"original-sut\n")
    assert (project / TARGET).read_bytes() == original
    assert not (project / "qa" / "archive").exists()


@pytest.mark.parametrize("execution_status", ["failed", "product_issue", "infrastructure_failure"])
def test_finalize_achieved_rejects_failed_execution_without_writing(tmp_path: Path, execution_status: str):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path, execution_status=execution_status)

    with pytest.raises(ValueError, match="execution"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status())

    change = project / "qa" / "changes" / CHANGE_ID
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()
    assert (project / TARGET).read_bytes() == b"original-sut\n"


def test_finalize_achieved_rejects_invalid_merge_without_writing(tmp_path: Path):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    _write(project, _staged_path("api", "tests/api/extra.py"), b"extra\n")

    with pytest.raises(ValueError, match="extra"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status())

    change = project / "qa" / "changes" / CHANGE_ID
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()


@pytest.mark.parametrize(
    "coverage",
    [
        {
            "measured": 0.40,
            "threshold": 0.9,
            "rounds_used": 1,
            "rounds_budget": 1,
            "decision": False,
            "coverage_state": "exhausted",
        },
        {
            "measured": 0.40,
            "threshold": 0.9,
            "rounds_used": 0,
            "rounds_budget": 1,
            "decision": False,
            "coverage_state": "inconclusive",
        },
        {
            "measured": 0.95,
            "threshold": 0.9,
            "rounds_used": 0,
            "rounds_budget": 1,
            "decision": True,
            "coverage_state": "needs_human",
        },
    ],
)
def test_finalize_achieved_rejects_unsatisfied_coverage_without_writing(
    tmp_path: Path, coverage: dict[str, object]
) -> None:
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    _write(
        project,
        f"qa/changes/{CHANGE_ID}/inspect/inspection.json",
        json.dumps({"coverage": coverage, "coverage_state": coverage["coverage_state"]}).encode("utf-8"),
    )

    with pytest.raises(ValueError, match="quality"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status())

    change = project / "qa" / "changes" / CHANGE_ID
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()


def test_finalize_achieved_requires_terminal_full_success(tmp_path: Path):
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path)
    pending = {
        "node_id": "human-review",
        "actions": ("approve", "reject"),
        "reason_category": "needs_human_review",
    }

    with pytest.raises(ValueError, match="terminal"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status(status="failed"))
    with pytest.raises(ValueError, match="interrupt"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status(pending_interrupt=pending))
    with pytest.raises(ValueError, match="full"):
        finalize_achieved(project, CHANGE_ID, ("api",), invocation=valid_status(entrypoint="archive"))

    change = project / "qa" / "changes" / CHANGE_ID
    assert not (change / "status.json").exists()
    assert not (change / "apply-manifest.json").exists()
