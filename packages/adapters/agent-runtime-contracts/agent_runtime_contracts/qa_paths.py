from __future__ import annotations

_ROOT_FILES = frozenset({".qa.yaml", "requirement.md", "proposal.md", "status.json", "events.jsonl"})
_DIRECT_PREFIXES = ("cases/", "tests/", "fixtures/", ".staging/", ".runtime/")
_FORBIDDEN = ("qa/changes/", "qa/archive/", "changes/", "archive/")


def qa_join(suffix: str) -> str:
    if not suffix or "\\" in suffix or "\x00" in suffix or suffix.startswith(("/", "~")):
        raise ValueError(f"qa suffix is not a safe relative path: {suffix}")
    if any(token in suffix for token in _FORBIDDEN) or suffix.startswith("qa/changes") or suffix.startswith("qa/archive"):
        raise ValueError(f"legacy qa path is not allowed: {suffix}")
    if suffix.startswith("qa/"):
        raise ValueError(f"qa suffix must not already be rooted: {suffix}")
    if suffix in _ROOT_FILES or suffix.startswith(_DIRECT_PREFIXES):
        return f"qa/{suffix}"
    return f"qa/results/{suffix}"


def qa_route(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(qa_join(item) for item in suffixes))
