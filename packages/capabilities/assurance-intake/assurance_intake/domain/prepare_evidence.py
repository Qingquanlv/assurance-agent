"""Authenticate committed Intake evidence before Agent preparation."""

from __future__ import annotations

from graph_engine.artifacts import ArtifactReadError


def evidence_read_error(error: ArtifactReadError) -> str:
    path = error.path
    if error.reason == "digest":
        return f"evidence digest changed after it was committed: {path}"
    if error.reason == "symlink":
        return f"case-review input must not contain a symlink: {path}"
    if error.reason == "missing":
        return f"missing case-review input: {path}"
    return f"case-review input must be a regular single-link file: {path}"
