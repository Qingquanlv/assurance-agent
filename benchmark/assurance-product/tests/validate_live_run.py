#!/usr/bin/env python3
"""Fail-closed admission checks for the Phase 5 OpenCode live item.

The harness diagnostics are deliberately not a second result tree.  This
checker therefore accepts only a direct change result represented in the
admission document, authenticated against the pinned manifest and evidence
digest.  It rejects the historical Tree/HEAD export vocabulary outright.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping


_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")
_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|bearer|token|secret)\s*[:=]\s*\S+|sk-[A-Za-z0-9-]+")
_FORBIDDEN_KEYS = frozenset({"tree", "tree_id", "head", "head.json", "export", "exports", "workspace"})


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_item(manifest: Mapping[str, Any]) -> dict[str, Any]:
    items = manifest.get("items")
    if not isinstance(items, list):
        raise ValueError("manifest.items must be a list")
    matches = [
        item for item in items if isinstance(item, dict) and item.get("id") == "opencode-ret-dept-management"
    ]
    if len(matches) != 1:
        raise ValueError("manifest must define exactly one OpenCode live item")
    return matches[0]


def _nested_values(value: Any, path: str = "$") -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                values.append((path, "non-text object key"))
                continue
            lowered = key.lower()
            if lowered in _FORBIDDEN_KEYS or "session_text" in lowered or "provider_text" in lowered:
                values.append((f"{path}.{key}", "forbidden result/session field"))
            values.extend(_nested_values(nested, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            values.extend(_nested_values(nested, f"{path}[{index}]"))
    elif isinstance(value, str) and _SECRET.search(value):
        values.append((path, "credential-looking text"))
    return values


def _git_head(repo: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=False, text=True, capture_output=True
    )
    if completed.returncode:
        raise ValueError("could not resolve repository HEAD")
    return completed.stdout.strip()


def _required_steps(item: Mapping[str, Any]) -> set[str]:
    steps = item.get("required_steps")
    if not isinstance(steps, list) or not steps or any(not isinstance(step, str) for step in steps):
        raise ValueError("manifest OpenCode item has invalid required_steps")
    return set(steps)


def validate(
    admission: Mapping[str, Any],
    evidence: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    evidence_digest: str,
    repo: Path,
) -> list[str]:
    """Return every admission failure; an empty list is an authenticated live run."""
    errors: list[str] = []
    errors.extend(f"{path}: {message}" for path, message in _nested_values(admission))
    errors.extend(f"evidence{path[1:]}: {message}" for path, message in _nested_values(evidence))
    try:
        item = _manifest_item(manifest)
        expected_steps = _required_steps(item)
    except ValueError as error:
        return errors + [str(error)]

    if admission.get("schema_version") != "1":
        errors.append("admission.schema_version must be '1'")
    if admission.get("admission_status") != "complete":
        errors.append("admission_status must be complete")
    source_commit = admission.get("source_commit")
    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        errors.append("source_commit must be a full git commit")
    elif source_commit != _git_head(repo):
        errors.append("source_commit does not authenticate the current committed source")
    if admission.get("manifest_sha256") != _sha256(repo / "benchmark/assurance-product/manifest.json"):
        errors.append("manifest_sha256 does not authenticate the pinned manifest")
    if admission.get("evidence_sha256") != evidence_digest:
        errors.append("evidence_sha256 does not authenticate provider diagnostics")

    expected_model = "volcengine/deepseek-v4-flash"
    expected_effort = "max"
    routes = item.get("routing_assignments")
    if not isinstance(routes, Mapping) or not routes:
        errors.append("manifest OpenCode routing assignments are absent")
    else:
        for route, assignment in routes.items():
            if not isinstance(assignment, Mapping):
                errors.append(f"route {route!r} is not an object")
            elif (
                assignment.get("provider_model") != expected_model
                or assignment.get("worker_profile") != expected_effort
            ):
                errors.append(f"route {route!r} is not locked to {expected_model}/{expected_effort}")

    if evidence.get("item_id") != item.get("id"):
        errors.append("evidence item_id does not match the pinned OpenCode item")
    if evidence.get("provider_model") != expected_model or evidence.get("worker_profile") != expected_effort:
        errors.append("evidence provider/model/effort does not match the locked OpenCode route")
    if evidence.get("outcome") != "completed" or evidence.get("terminal_status") != "completed":
        errors.append("provider diagnostics are not a completed terminal run")

    provider = evidence.get("provider")
    if not isinstance(provider, Mapping):
        errors.append("provider reference is absent")
    else:
        session = provider.get("session")
        if not isinstance(session, str) or not _SESSION_ID.fullmatch(session):
            errors.append("provider session must be a real opaque session identifier")
        process = provider.get("process")
        binding = item.get("adapter_binding")
        if not isinstance(process, Mapping) or not isinstance(binding, Mapping):
            errors.append("provider binding is absent")
        elif process.get("endpoint") != binding.get("endpoint") or process.get("protocol") != binding.get(
            "protocol_profile"
        ):
            errors.append("provider binding does not match the pinned OpenCode manifest")

    status = admission.get("workflow")
    if not isinstance(status, Mapping):
        errors.append("workflow projection is absent")
    else:
        if status.get("terminal") != "achieved":
            errors.append("workflow terminal must be achieved")
        steps = status.get("completed_steps")
        if not isinstance(steps, list) or any(not isinstance(step, str) for step in steps):
            errors.append("workflow completed_steps must be a string list")
        elif set(steps) != expected_steps:
            errors.append("workflow completed_steps does not equal the required-step projection")
        receipt = status.get("publish_receipt")
        if not isinstance(receipt, Mapping):
            errors.append("authenticated publish receipt is absent")
        elif not isinstance(receipt.get("receipt_id"), str) or not _SESSION_ID.fullmatch(
            str(receipt.get("receipt_id"))
        ):
            errors.append("publish receipt must have an opaque receipt_id")
        elif not isinstance(receipt.get("sha256"), str) or not _HEX_64.fullmatch(str(receipt.get("sha256"))):
            errors.append("publish receipt must have a SHA-256 digest")
    return errors


def _fixture(repo: Path, evidence_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = _load_object(repo / "benchmark/assurance-product/manifest.json")
    item = _manifest_item(manifest)
    evidence = {
        "item_id": item["id"],
        "provider_model": "volcengine/deepseek-v4-flash",
        "worker_profile": "max",
        "outcome": "completed",
        "terminal_status": "completed",
        "provider": {
            "session": "ses_0123456789abcdef",
            "process": {
                "endpoint": item["adapter_binding"]["endpoint"],
                "protocol": item["adapter_binding"]["protocol_profile"],
            },
        },
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    admission = {
        "schema_version": "1",
        "admission_status": "complete",
        "source_commit": _git_head(repo),
        "manifest_sha256": _sha256(repo / "benchmark/assurance-product/manifest.json"),
        "evidence_sha256": _sha256(evidence_path),
        "workflow": {
            "terminal": "achieved",
            "completed_steps": item["required_steps"],
            "publish_receipt": {"receipt_id": "rcpt_0123456789abcdef", "sha256": "a" * 64},
        },
    }
    return admission, evidence, manifest


class LiveAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).resolve().parents[3]

    def test_accepts_only_complete_direct_change_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "evidence.json"
            admission, evidence, manifest = _fixture(self.repo, evidence_path)
            self.assertEqual(
                validate(
                    admission, evidence, manifest, evidence_digest=_sha256(evidence_path), repo=self.repo
                ),
                [],
            )

    def test_rejects_tree_head_export_and_secret_session_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "evidence.json"
            admission, evidence, manifest = _fixture(self.repo, evidence_path)
            admission["Tree"] = "legacy"
            evidence["status"] = {"provider_text": "Authorization: Bearer redacted"}
            failures = validate(
                admission, evidence, manifest, evidence_digest=_sha256(evidence_path), repo=self.repo
            )
            self.assertTrue(any("forbidden" in failure for failure in failures), failures)
            self.assertTrue(any("credential" in failure for failure in failures), failures)

    def test_rejects_missing_session_or_incomplete_step_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "evidence.json"
            admission, evidence, manifest = _fixture(self.repo, evidence_path)
            evidence["provider"]["session"] = None
            admission["workflow"]["completed_steps"] = admission["workflow"]["completed_steps"][:-1]
            failures = validate(
                admission, evidence, manifest, evidence_digest=_sha256(evidence_path), repo=self.repo
            )
            self.assertTrue(any("session" in failure for failure in failures), failures)
            self.assertTrue(any("required-step" in failure for failure in failures), failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.self_test:
        result = unittest.main(argv=[sys.argv[0]], exit=False)
        return 0 if result.result.wasSuccessful() else 1
    if arguments.admission is None or arguments.evidence is None:
        parser.error("--admission and --evidence are required unless --self-test is used")
    try:
        admission = _load_object(arguments.admission)
        evidence = _load_object(arguments.evidence)
        manifest = _load_object(arguments.repo / "benchmark/assurance-product/manifest.json")
        errors = validate(
            admission, evidence, manifest, evidence_digest=_sha256(arguments.evidence), repo=arguments.repo
        )
    except ValueError as error:
        print(f"phase5-opencode-admission: {error}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"phase5-opencode-admission: {error}", file=sys.stderr)
        return 1
    print("phase5-opencode-admission: authenticated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
