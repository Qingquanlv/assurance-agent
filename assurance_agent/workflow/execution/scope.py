"""Resolve the exact test files a change owns, scoped from its codegen plan.

Each `qa/changes/<change-id>/plans/<target>-codegen-plan.md` documents the
concrete test file(s) generated for that change. API/E2E use the
"Test Function Mapping" section; Fuzz and Performance use "Target Files".
`aa run` must execute only those files — never the whole shared
`tests/<target>/` tree, which also contains every other change's tests.

If a plan is absent or has no parseable mapping table, callers may fall back to
running the full `test_dir` for legacy changes. A parseable section with no
executable target is different: it is an explicitly scoped empty result and
must fail closed rather than widening to the shared test tree.
"""

import re
from pathlib import Path

_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$")
_BACKTICK_PATH_RE = re.compile(r"`(tests/[^`]+\.py)`")


def resolve_test_paths(change_dir: Path, target: str) -> list[str] | None:
    """Return the sorted, deduped test file paths mapped to `target` for this change.

    ``None`` means a legacy plan has no usable scoping contract. An empty list
    means a current, parseable contract declared no executable test target.
    """
    plan_path = change_dir / "plans" / f"{target}-codegen-plan.md"
    if not plan_path.is_file():
        return None
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return None
    section = _extract_section(
        text,
        "Target Files" if target in {"fuzz", "performance"} else "Test Function Mapping",
    )
    if section is None:
        return None
    paths = {m.group(1) for m in _BACKTICK_PATH_RE.finditer(section)}
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
