"""Resolve the exact test files a change owns, scoped from its codegen plan.

Each `qa/changes/<change-id>/plans/<target>-codegen-plan.md` documents a
"Test Function Mapping" table (`| Case ID | Test Function | Target File |`)
listing the concrete test file(s) generated for that change. `aa run` must
execute only those files — never the whole shared `tests/<target>/` tree,
which also contains every other change's tests.

If a plan is absent or has no parseable mapping table, callers fall back to
running the full `test_dir` (existing behaviour) — this module never widens
scope, only narrows it when it safely can.
"""

import re
from pathlib import Path

_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$")
_BACKTICK_PATH_RE = re.compile(r"`(tests/[^`]+\.py)`")


def resolve_test_paths(change_dir: Path, target: str) -> list[str]:
    """Return the sorted, deduped test file paths mapped to `target` for this change.

    Returns an empty list when the plan is missing or has no mapping table —
    callers must treat that as "no scoping available", not "no tests".
    """
    plan_path = change_dir / "plans" / f"{target}-codegen-plan.md"
    if not plan_path.is_file():
        return []
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return []
    section = _extract_section(text, "Test Function Mapping")
    if section is None:
        return []
    paths = {m.group(1) for m in _BACKTICK_PATH_RE.finditer(section)}
    return sorted(paths)


def _extract_section(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    heading_lower = heading.strip().lower()
    start = None
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match and match.group(1).strip().lower() == heading_lower:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].strip().startswith("#"):
            end = j
            break
    return "\n".join(lines[start:end])
