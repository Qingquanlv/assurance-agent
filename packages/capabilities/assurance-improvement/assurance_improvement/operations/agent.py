"""Retro commit checks shared by the improvement agent finalize hooks."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, OutputError
from graph_engine.artifacts import ArtifactReadError, read_workspace_file

from assurance_improvement.contracts.agent import (
    RetroAnalysisInputV1,
    RetroAnalysisResultV3,
    RetroSynthesisInputV1,
)

DomainName = Literal["issue", "workflow", "eval", "discovery", "coverage_gap"]


class _WriteRoot(Protocol):
    write_root: Path


def _source_ids(result: RetroAnalysisResultV3) -> tuple[str, ...]:
    ids: list[str] = []
    for signal in result.signals:
        ids.extend(signal.source_refs.all_ids())
    for candidate in result.candidates:
        ids.extend(candidate.source_refs.all_ids())
    return tuple(ids)


def _workspace_file(workspace: Path, relative: str) -> bytes:
    try:
        return read_workspace_file(workspace, relative)
    except ArtifactReadError as error:
        raise OutputError(str(error)) from error


def commit_retro(
    business: RetroSynthesisInputV1 | RetroAnalysisInputV1,
    result: RetroAnalysisResultV3,
    context: _WriteRoot,
    *,
    expected_domain: DomainName | None,
) -> RetroAnalysisResultV3:
    document = result
    if expected_domain is None:
        if not isinstance(business, RetroSynthesisInputV1) or business.context is None:
            raise InputError("retro synthesis requires the locked context")
        context_lock = business.context
        if document.retro_id != context_lock.retro_id:
            raise OutputError("retro analysis identity does not match the locked retro")
        if document.domain != expected_domain:
            raise OutputError(f"retro analysis domain must be {expected_domain}")
        allowed = context_lock.source_manifest.resolvable_ids()
        if document.signals:
            raise OutputError("synthesis cannot change the locked context signals")
        from assurance_improvement.operations.retro import validate_candidates

        try:
            validate_candidates(context_lock, document.candidates)
        except (InputError, ValidationError) as error:
            raise OutputError(str(error)) from error
    else:
        if not isinstance(business, RetroAnalysisInputV1) or business.evidence_slice is None:
            raise InputError("retro analysis requires the locked evidence slice")
        slice_lock = business.evidence_slice
        if document.retro_id != slice_lock.retro_id:
            raise OutputError("retro analysis identity does not match the locked retro")
        if document.domain != expected_domain:
            raise OutputError(f"retro analysis domain must be {expected_domain}")
        if slice_lock.domain != expected_domain:
            raise InputError("analysis input slice domain does not match its handler")
        if document.candidates:
            raise OutputError("domain analysis produces signals, not improvement candidates")
        allowed = slice_lock.resolvable_ids()
        ids = [signal.signal_id for signal in document.signals]
        deterministic_ids = {signal.signal_id for signal in slice_lock.deterministic_signals}
        if len(ids) != len(set(ids)) or deterministic_ids.intersection(ids):
            raise OutputError("analysis signals must be unique and must not repeat deterministic signals")
    for source_id in _source_ids(document):
        if source_id not in allowed:
            raise OutputError("candidate source is outside the retro manifest")
    suffix = f"retro-{expected_domain}-analysis" if expected_domain else "retro"
    try:
        written = RetroAnalysisResultV3.model_validate_json(
            _workspace_file(context.write_root, f"qa/results/retro/{suffix}.json")
        )
    except (OSError, ValueError) as error:
        raise OutputError(f"required Retro result artifact is invalid: {suffix}.json") from error
    if written != document:
        raise OutputError("written Retro result differs from the assistant result")
    return document


__all__ = ["commit_retro"]
