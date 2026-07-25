"""Guard against legacy known-product issue file references in production paths."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_LEGACY_STEM = "known-product-issues"
_FORBIDDEN_MD = _LEGACY_STEM + ".md"
_FORBIDDEN_JSON = _LEGACY_STEM + ".json"

_FORBIDDEN_PATTERNS = (
    re.compile(re.escape(_FORBIDDEN_MD)),
    re.compile(re.escape(_FORBIDDEN_JSON)),
)

_GUARD_FILE = Path(__file__).resolve()
_REPO_ROOT = _GUARD_FILE.parents[3]


def _iter_scan_files() -> list[Path]:
    files: list[Path] = []

    assurance_agent = _REPO_ROOT / "assurance_agent"
    files.extend(p for p in assurance_agent.rglob("*.py") if p.is_file())

    skills_root = assurance_agent / "_resources" / "skills"
    if skills_root.is_dir():
        files.extend(p for p in skills_root.rglob("*") if p.is_file())

    resources_root = assurance_agent / "_resources"
    for path in resources_root.rglob("*"):
        if not path.is_file():
            continue
        if skills_root in path.parents or path.parent == skills_root:
            continue
        if path.suffix in {".md", ".yaml", ".yml", ".json", ".txt", ".html"}:
            files.append(path)

    schemas_doc = _REPO_ROOT / "docs" / "schemas.md"
    if schemas_doc.is_file():
        files.append(schemas_doc)

    tests_root = _REPO_ROOT / "tests"
    if tests_root.is_dir():
        files.extend(p for p in tests_root.rglob("*") if p.is_file())

    # De-duplicate while preserving order.
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(path)
    return ordered


def _line_allowed_in_guard(line: str) -> bool:
    if _LEGACY_STEM not in line:
        return True
    if "+" in line and (_FORBIDDEN_MD in line or _FORBIDDEN_JSON in line or ".md" in line or ".json" in line):
        return True
    return False


def _violations_in_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    violations: list[str] = []
    rel = path.relative_to(_REPO_ROOT)
    for line_no, line in enumerate(text.splitlines(), start=1):
        if path.resolve() == _GUARD_FILE and _line_allowed_in_guard(line):
            continue
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(line):
                violations.append(f"{rel}:{line_no}: {line.strip()}")
                break
    return violations


def test_no_legacy_known_product_issue_filenames_in_production_paths() -> None:
    violations: list[str] = []
    for path in _iter_scan_files():
        violations.extend(_violations_in_file(path))
    assert not violations, "Legacy known-product issue filenames found:\n" + "\n".join(violations)


def test_known_product_issue_classifier_is_classification_only_hint() -> None:
    """Legacy enum label must not drive filesystem IO or lifecycle authority."""
    from assurance_agent import resources
    from assurance_agent.risk import context as risk_context
    from assurance_agent.workflow.report import failure_classifier, report_builder
    from assurance_agent.workflow.report.failure_classifier import classify_failure

    risk_source = Path(risk_context.__file__).read_text(encoding="utf-8")
    report_source = Path(report_builder.__file__).read_text(encoding="utf-8")
    rules_source = resources.read_text("rules", "failure-classification.yaml")

    for label, source in (
        ("risk.context", risk_source),
        ("failure_classifier", Path(failure_classifier.__file__).read_text(encoding="utf-8")),
        ("report_builder", report_source),
        ("failure-classification.yaml", rules_source),
    ):
        assert _FORBIDDEN_MD not in source, f"{label} references legacy markdown issue files"
        assert _FORBIDDEN_JSON not in source, f"{label} references legacy json issue files"

    assert "known_product_issue" in rules_source
    result = classify_failure(
        message="expected-product-fail: known product issue documented",
        log_excerpt="",
        target="api",
    )
    assert result.category == "known_product_issue"
    assert "known_product_issue" in report_source
