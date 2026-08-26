from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from graph_engine.canonical import JSONValue, canonical_digest
from tests.phase5.test_achieved_terminal import (
    CHANGE_ID as ACHIEVED_CHANGE_ID,
    TARGET as ACHIEVED_TARGET,
    _ready_change,
    valid_status,
)

CHANGE_ID = "CH-PUB-001"
TARGET_A = "tests/api/test_users.py"
TARGET_B = "tests/api/test_orders.py"
UNLISTED = "tests/api/unlisted.py"
README = "README.md"
_SHA = "a" * 64


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _write(project: Path, relative: str, content: bytes, *, mode: int = 0o644) -> Path:
    path = project.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    return path


def _aggregate(mapping: Mapping[str, JSONValue]) -> str:
    return canonical_digest(cast(JSONValue, dict(mapping)))


def _status_payload(
    change_id: str,
    *,
    state: str = "achieved",
    publication: str = "ready",
    manifest_digest: str | None = None,
    file_count: int = 0,
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "invocation_id": "inv-publish-001",
        "lock_digest": _SHA,
        "root_input_digest": _SHA,
        "status": "completed" if state == "achieved" else "running",
        "entrypoint": "full",
        "graph_hierarchy": (),
        "node_states": (),
        "selected_test_families": ("api",),
        "coverage_progress": None,
        "durable_effects": (),
        "adapter_evidence": (),
        "pending_interrupt": None,
        "terminal_reason": None,
        "change": {"change_id": change_id, "state": state},
        "apply": {"manifest_digest": manifest_digest, "file_count": file_count},
        "publication": {"status": publication},
    }


def write_achieved(
    tmp_path: Path,
    *,
    change_id: str = CHANGE_ID,
    files: tuple[tuple[str, bytes, bytes | None], ...] = (
        (TARGET_A, b"generated-a\n", b"original-a\n"),
        (TARGET_B, b"generated-b\n", b"original-b\n"),
    ),
    state: str = "achieved",
    publication: str = "ready",
    extras: dict[str, bytes] | None = None,
    project: Path | None = None,
) -> Path:
    root = project or (tmp_path / "project")
    root.mkdir(parents=True, exist_ok=True)
    change = root / "qa" / "changes" / change_id
    change.mkdir(parents=True, exist_ok=True)
    manifest_files = []
    for target, source, baseline in files:
        source_path = f"qa/changes/{change_id}/generated/api/files/{target}"
        _write(root, source_path, source)
        if baseline is not None:
            _write(root, target, baseline)
        manifest_files.append(
            {
                "target_path": target,
                "source_path": source_path,
                "source_sha256": _digest(source),
                "baseline_sha256": None if baseline is None else _digest(baseline),
                "mode": 0o644,
                "operation": "generated",
            }
        )
    extras = {
        UNLISTED: b"keep-unlisted\n",
        README: b"project readme\n",
        **(extras or {}),
    }
    for relative, content in extras.items():
        _write(root, relative, content)
    manifest_digest = _aggregate(
        {"change_id": change_id, "files": [item["target_path"] for item in manifest_files]}
    )
    manifest = {
        "schema_version": "1",
        "change_id": change_id,
        "digest": manifest_digest,
        "files": manifest_files,
    }
    _write(
        root,
        f"qa/changes/{change_id}/apply-manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
    )
    _write(
        root,
        f"qa/changes/{change_id}/status.json",
        json.dumps(
            _status_payload(
                change_id,
                state=state,
                publication=publication,
                manifest_digest=manifest_digest,
                file_count=len(manifest_files),
            ),
            indent=2,
            sort_keys=True,
        ).encode("utf-8"),
    )
    return root.resolve()


def _source_map(files: tuple[tuple[str, bytes, bytes | None], ...]) -> dict[str, str]:
    return {target: _digest(source) for target, source, _baseline in files}


def _baseline_map(files: tuple[tuple[str, bytes, bytes | None], ...]) -> dict[str, str | None]:
    return {target: None if baseline is None else _digest(baseline) for target, _source, baseline in files}


def test_publish_rejects_non_achieved_change(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path, state="running", publication="not_ready")
    original = (project / TARGET_A).read_bytes()

    with pytest.raises(PublishError, match="achieved"):
        publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == original
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-journal.json").exists()


def test_publish_writes_exact_manifest_files(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved
    from assurance_product.models import PublishReceiptV1
    from assurance_product.status import finalize_achieved

    project = _ready_change(tmp_path).resolve()
    unlisted = _write(project, UNLISTED, b"keep-unlisted\n")
    finalize_achieved(project, ACHIEVED_CHANGE_ID, ("api",), invocation=valid_status())

    receipt = publish_achieved(project, ACHIEVED_CHANGE_ID)

    assert isinstance(receipt, PublishReceiptV1)
    assert receipt.schema_version == "1"
    assert receipt.change_id == ACHIEVED_CHANGE_ID
    assert (project / ACHIEVED_TARGET).read_bytes() == b"generated-candidate\n"
    assert unlisted.read_bytes() == b"keep-unlisted\n"
    written = PublishReceiptV1.model_validate_json(
        (project / "qa" / "changes" / ACHIEVED_CHANGE_ID / "publish-receipt.json").read_bytes()
    )
    assert written == receipt
    status = json.loads(
        (project / "qa" / "changes" / ACHIEVED_CHANGE_ID / "status.json").read_text(encoding="utf-8")
    )
    assert status["publication"]["status"] == "published"
    assert not any(project.rglob("result-tree"))


def test_publish_does_not_write_or_delete_unlisted_files(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved

    files = (
        (TARGET_A, b"generated-a\n", b"original-a\n"),
        (TARGET_B, b"generated-b\n", b"original-b\n"),
    )
    project = write_achieved(tmp_path, files=files)
    extra = project / UNLISTED
    readme = project / README
    extra_mtime = extra.stat().st_mtime_ns
    readme_bytes = readme.read_bytes()

    publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert (project / TARGET_B).read_bytes() == b"generated-b\n"
    assert extra.read_bytes() == b"keep-unlisted\n"
    assert extra.stat().st_mtime_ns == extra_mtime
    assert readme.read_bytes() == readme_bytes
    assert extra.exists()


def test_publish_rejects_source_digest_mismatch(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path)
    source = project / "qa" / "changes" / CHANGE_ID / "generated" / "api" / "files" / TARGET_A
    source.write_bytes(b"tampered-source\n")
    original = (project / TARGET_A).read_bytes()

    with pytest.raises(PublishError, match="source"):
        publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == original
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()


def test_publish_rejects_target_baseline_drift(tmp_path: Path) -> None:
    from assurance_product.export import PublishError, publish_achieved

    project = write_achieved(tmp_path)
    (project / TARGET_A).write_bytes(b"drifted-target\n")

    with pytest.raises(PublishError, match="drift|baseline"):
        publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == b"drifted-target\n"
    assert (project / TARGET_B).read_bytes() == b"original-b\n"
    assert not (project / "qa" / "changes" / CHANGE_ID / "publish-receipt.json").exists()


def test_publish_accepts_already_matching_targets(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved

    files = ((TARGET_A, b"generated-a\n", b"original-a\n"),)
    project = write_achieved(tmp_path, files=files)
    (project / TARGET_A).write_bytes(b"generated-a\n")

    receipt = publish_achieved(project, CHANGE_ID)

    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert receipt.final_digest == receipt.source_digest
    assert receipt.source_digest == _aggregate(_source_map(files))
    assert receipt.target_baseline == _aggregate(_baseline_map(files))


def test_repeated_publish_is_idempotent(tmp_path: Path) -> None:
    from assurance_product.export import publish_achieved

    files = (
        (TARGET_A, b"generated-a\n", b"original-a\n"),
        (TARGET_B, b"generated-b\n", b"original-b\n"),
    )
    project = write_achieved(tmp_path, files=files)

    first = publish_achieved(project, CHANGE_ID)
    second = publish_achieved(project, CHANGE_ID)

    assert second == first
    assert (project / TARGET_A).read_bytes() == b"generated-a\n"
    assert (project / TARGET_B).read_bytes() == b"generated-b\n"
    assert first.source_digest == _aggregate(_source_map(files))
    assert first.final_digest == first.source_digest
    assert stat.S_ISREG((project / TARGET_A).stat().st_mode)
