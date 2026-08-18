"""Resolve the exact test files a change owns, scoped from its codegen plan.

Each `qa/changes/<change-id>/plans/<target>-codegen-plan.md` documents the
concrete test file(s) generated for that change. API/E2E use the
"Test Function Mapping" section. Fuzz and Performance prefer "Target Files"
but also accept their generated plan variants, "Test Function Mapping" and
"Task Mapping" respectively. `aa run` must execute only those files — never
the whole shared `tests/<target>/` tree, which also contains every other
change's tests.

If a plan is absent, callers may fall back to running the full `test_dir` for
legacy changes. Once a plan exists, an unreadable plan, an unknown mapping
shape, or a mapping without an executable target is an explicitly scoped empty
result and must fail closed rather than widening to the shared test tree.
"""

import json
import re
from pathlib import Path

from assurance_agent.artifacts.models import CodegenMapping

_HEADING_RE = re.compile(r"^(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_TEST_PATH_RE = re.compile(
    r"`(?P<quoted>tests/[^`\r\n|]+\.py)`"
    r"|(?<![`A-Za-z0-9_./-])(?P<plain>tests/[A-Za-z0-9_./-]+\.py)"
    r"(?![`A-Za-z0-9_./-])"
)


def resolve_test_paths(change_dir: Path, target: str) -> list[str] | None:
    """Return the sorted, deduped test file paths mapped to `target` for this change.

    ``None`` means a legacy change has no plan and therefore no scoping
    contract. An empty list means a current plan cannot declare an executable
    test target safely.
    """
    mapping_path = change_dir / "plans" / f"{target}-codegen-mapping.json"
    if mapping_path.is_file():
        return _resolve_structured_mapping(mapping_path, target)

    plan_path = change_dir / "plans" / f"{target}-codegen-plan.md"
    if not plan_path.is_file():
        return None
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return []
    headings = {
        "fuzz": ("Target Files", "Test Function Mapping"),
        "performance": ("Target Files", "Task Mapping"),
    }.get(target, ("Test Function Mapping",))
    section = next(
        (section for heading in headings if (section := _extract_section(text, heading)) is not None),
        None,
    )
    if section is None:
        return []
    paths: set[str] = set()
    for match in _TEST_PATH_RE.finditer(section):
        path = match.group("quoted") or match.group("plain")
        if path:
            paths.add(path)
    if target == "fuzz":
        fuzz_root = Path("tests/fuzz")
        paths = {
            path
            for path in paths
            if Path(path).parent == fuzz_root
            and Path(path).name.startswith("test_")
            and Path(path).suffix == ".py"
        }
    return sorted(paths)


def _resolve_structured_mapping(mapping_path: Path, target: str) -> list[str]:
    """Resolve the graph-validated JSON relation; malformed current data fails closed."""
    try:
        document = CodegenMapping.model_validate(json.loads(mapping_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if document.layer != target:
        return []

    root = {
        "api": Path("tests/api"),
        "e2e": Path("tests/e2e"),
        "fuzz": Path("tests/fuzz"),
        "performance": Path("tests/perf"),
    }.get(target)
    if root is None:
        return []

    paths: set[str] = set()
    for entry in document.entries:
        path = Path(entry.target_file)
        if not path.is_relative_to(root) or path.suffix != ".py":
            return []
        if target == "performance":
            if path.parent != root or not path.name.startswith("locustfile"):
                return []
        elif target == "fuzz":
            if path.parent != root or not path.name.startswith("test_"):
                return []
        elif not path.name.startswith("test_"):
            return []
        paths.add(entry.target_file)
    return sorted(paths)


def _extract_section(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    heading_lower = heading.strip().lower()
    start = None
    heading_level = 0
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match and (match.group(2) or "").strip().lower() == heading_lower:
            start = i + 1
            heading_level = len(match.group(1))
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        match = _HEADING_RE.match(lines[j].strip())
        if match and len(match.group(1)) <= heading_level:
            end = j
            break
    return "\n".join(lines[start:end])
