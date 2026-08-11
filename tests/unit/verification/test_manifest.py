"""GeneratedManifest on-disk validation (adversarial discovery Phase 1, Task C)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.discovery import GeneratedFileEntry, GeneratedManifest
from assurance_agent.verification.manifest import (
    ManifestValidationError,
    digest_file,
    validate_generated_manifest,
)


def _write(path: Path, content: bytes | str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    path.write_bytes(data)
    return sha256_bytes(data)


def _manifest(
    *,
    source: str = "generated/tests/api/test_tenant.py",
    target: str = "tests/api/test_tenant.py",
    sha256: str,
    role: str = "search_test",
    selection: tuple[str, ...] | None = None,
    extra_files: tuple[GeneratedFileEntry, ...] = (),
) -> GeneratedManifest:
    entry = GeneratedFileEntry(
        source=source,
        target=target,
        sha256=sha256,
        role=role,  # type: ignore[arg-type]
    )
    files = (entry, *extra_files)
    return GeneratedManifest(
        schema_version="1",
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        round_id="R0001",
        parent_round_ids=(),
        base_revision="deadbeef",
        strategy_ids=("api.auth.tenant-boundary",),
        files=files,
        execution_selection=selection if selection is not None else (target,),
        oracle_refs=("ORACLE-auth-tenant-isolation",),
        seed=12345,
    )


def _round_with_file(tmp_path: Path, content: str = "assert True\n") -> tuple[Path, str]:
    round_root = tmp_path / "R0001"
    source = round_root / "generated" / "tests" / "api" / "test_tenant.py"
    digest = _write(source, content)
    return round_root, digest


def test_digest_file_matches_canonical_prefix(tmp_path: Path) -> None:
    path = tmp_path / "f.py"
    data = b"hello"
    path.write_bytes(data)
    assert digest_file(path) == "sha256:" + hashlib.sha256(data).hexdigest()
    assert digest_file(path) == sha256_bytes(data)


def test_validate_happy_path(tmp_path: Path) -> None:
    round_root, digest = _round_with_file(tmp_path)
    result = validate_generated_manifest(round_root, _manifest(sha256=digest))
    assert result.round_id == "R0001"
    assert result.file_digests["tests/api/test_tenant.py"] == digest


def test_reject_missing_source(tmp_path: Path) -> None:
    round_root = tmp_path / "R0001"
    (round_root / "generated").mkdir(parents=True)
    fake = "sha256:" + ("a" * 64)
    with pytest.raises(ManifestValidationError) as exc:
        validate_generated_manifest(round_root, _manifest(sha256=fake))
    assert exc.value.code == "source_missing"


def test_reject_digest_mismatch(tmp_path: Path) -> None:
    round_root, _digest = _round_with_file(tmp_path, "body-a\n")
    wrong = "sha256:" + ("b" * 64)
    with pytest.raises(ManifestValidationError) as exc:
        validate_generated_manifest(round_root, _manifest(sha256=wrong))
    assert exc.value.code == "digest_mismatch"


def test_reject_extra_unmanifested_file(tmp_path: Path) -> None:
    round_root, digest = _round_with_file(tmp_path)
    _write(round_root / "generated" / "tests" / "api" / "orphan.py", "orphan\n")
    with pytest.raises(ManifestValidationError) as exc:
        validate_generated_manifest(round_root, _manifest(sha256=digest))
    assert exc.value.code == "extra_unmanifested_file"
    assert exc.value.path is not None
    assert "orphan.py" in exc.value.path


def test_reject_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n")
    round_root = tmp_path / "R0001"
    target_link = round_root / "generated" / "tests" / "api" / "test_tenant.py"
    target_link.parent.mkdir(parents=True)
    target_link.symlink_to(outside)
    digest = digest_file(outside)
    with pytest.raises(ManifestValidationError) as exc:
        validate_generated_manifest(round_root, _manifest(sha256=digest))
    assert exc.value.code == "symlink_escape"


def test_reject_overlay_role_phase1(tmp_path: Path) -> None:
    round_root, digest = _round_with_file(tmp_path)
    with pytest.raises(ManifestValidationError) as exc:
        validate_generated_manifest(round_root, _manifest(sha256=digest, role="overlay"))
    assert exc.value.code == "overlay_forbidden"


def test_reject_execution_selection_not_in_manifest(tmp_path: Path) -> None:
    round_root, digest = _round_with_file(tmp_path)
    with pytest.raises(ManifestValidationError) as exc:
        validate_generated_manifest(
            round_root,
            _manifest(
                sha256=digest,
                selection=("tests/api/test_other.py",),
            ),
        )
    assert exc.value.code == "execution_selection_unknown"


def test_reject_source_not_under_generated(tmp_path: Path) -> None:
    round_root = tmp_path / "R0001"
    # Place file outside generated/ but under round root with a path that model allows
    # (no ..). Validation against disk must still require generated/ prefix.
    source_rel = "decision.json"
    digest = _write(round_root / source_rel, '{"ok":true}\n')
    # Build via model_construct to bypass pydantic source shape if needed — but
    # "decision.json" is a safe relpath; GeneratedFileEntry allows it.
    entry = GeneratedFileEntry(
        source=source_rel,
        target="tests/api/test_tenant.py",
        sha256=digest,
        role="search_test",
    )
    manifest = GeneratedManifest(
        schema_version="1",
        change_id="CH-DEMO-001",
        campaign_id="CAM-001",
        round_id="R0001",
        base_revision="deadbeef",
        strategy_ids=("api.auth.tenant-boundary",),
        files=(entry,),
        execution_selection=("tests/api/test_tenant.py",),
        seed=1,
    )
    with pytest.raises(ManifestValidationError) as exc:
        validate_generated_manifest(round_root, manifest)
    assert exc.value.code == "source_not_under_generated"
