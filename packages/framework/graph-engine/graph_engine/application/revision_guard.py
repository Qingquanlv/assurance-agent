from __future__ import annotations

import re

from graph_engine.errors import GraphEngineError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RevisionMismatch(GraphEngineError):
    """Raised when an Invocation is bound to a different installed graph revision."""

    def __init__(self, *, required: str, installed: str) -> None:
        self.required = required
        self.installed = installed
        super().__init__(f"installed revision {installed} does not match required deployment {required}")


def _revision_id(value: str, kind: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{kind} revision must be a lowercase SHA-256 hex value")
    return value


def require_revision(*, required: str, installed: str) -> str:
    required_id = _revision_id(required, "required")
    installed_id = _revision_id(installed, "installed")
    if required_id != installed_id:
        raise RevisionMismatch(required=required_id, installed=installed_id)
    return required_id


__all__ = ["RevisionMismatch", "require_revision"]
