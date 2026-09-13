from __future__ import annotations

_ROOT_FILES = frozenset({".qa.yaml", "requirement.md", "proposal.md", "status.json", "events.jsonl"})
_DIRECT_PREFIXES = ("cases/", "tests/", "fixtures/", ".staging/", ".runtime/")
_QA = "qa"
_CHANGES = "changes"
_ARCHIVE = "archive"


def _is_legacy_suffix(suffix: str) -> bool:
    return (
        suffix.startswith(f"{_QA}/{_CHANGES}")
        or suffix.startswith(f"{_QA}/{_ARCHIVE}")
        or suffix.startswith(f"{_CHANGES}/")
        or suffix.startswith(f"{_ARCHIVE}/")
    )


def _reject_unsafe_path_segments(suffix: str) -> None:
    for part in suffix.split("/"):
        if part in {".", ".."}:
            raise ValueError(f"qa suffix is not a safe relative path: {suffix}")


def qa_join(suffix: str) -> str:
    if not suffix or "\\" in suffix or "\x00" in suffix or suffix.startswith(("/", "~")):
        raise ValueError(f"qa suffix is not a safe relative path: {suffix}")
    _reject_unsafe_path_segments(suffix)
    if _is_legacy_suffix(suffix):
        raise ValueError(f"legacy qa path is not allowed: {suffix}")
    if suffix.startswith("qa/"):
        raise ValueError(f"qa suffix must not already be rooted: {suffix}")
    if suffix in _ROOT_FILES or suffix.startswith(_DIRECT_PREFIXES):
        return f"qa/{suffix}"
    return f"qa/results/{suffix}"


def qa_route(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted({qa_join(item) for item in suffixes}))
