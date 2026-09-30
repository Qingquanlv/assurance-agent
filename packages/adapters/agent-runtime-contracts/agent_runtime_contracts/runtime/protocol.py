from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, TypeVar

from graph_engine.attempts import AuthorizedAttemptScope, PermanentTaskFailure

from agent_runtime_contracts.wire.models import AgentRunResult


_PreparedT_contra = TypeVar("_PreparedT_contra", contravariant=True)


def canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


@dataclass(frozen=True, slots=True)
class ReadOnlyRawWorkspace:
    _root: Path
    identity_digest: str = ""

    def _resolved(self, relative: str) -> Path:
        if not canonical_relative(relative):
            raise ValueError(f"raw workspace path must be canonical and relative: {relative}")
        path = self._root
        for part in PurePosixPath(relative).parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"raw workspace path is a symlink: {relative}")
        try:
            path.resolve().relative_to(self._root.resolve())
        except ValueError as error:
            raise ValueError(
                f"raw workspace path must stay inside the authorized root: {relative}"
            ) from error
        return path

    def read_bytes(self, relative: str) -> bytes:
        path = self._resolved(relative)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(relative)
        return path.read_bytes()

    def read_text(self, relative: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(relative).decode(encoding)


@dataclass(frozen=True, slots=True)
class RawAgentRuntimeOutcome:
    run_result: AgentRunResult
    raw_workspace: ReadOnlyRawWorkspace


class RuntimePhase(Protocol[_PreparedT_contra]):
    async def execute(
        self,
        prepared: _PreparedT_contra,
        scope: AuthorizedAttemptScope,
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure: ...
