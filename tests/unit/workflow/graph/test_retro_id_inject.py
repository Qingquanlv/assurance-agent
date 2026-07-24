"""Tests for ensure_retro_params param injection in GraphRuntime."""

from __future__ import annotations

import re

import pytest

import assurance_agent.workflow.graph.runtime  # noqa: F401 — install Task 12 planner patches
from assurance_agent.workflow.graph.runtime import ensure_retro_params


def test_resolve_or_inject_retro_id_fills_empty() -> None:
    params = {"retro_id": "", "retro_last": 10, "retro_min_evidence": 2, "retro_dry_run": False}
    out = ensure_retro_params(params)
    assert out["retro_id"].startswith("retro-")


def test_ensure_retro_params_preserves_existing_id() -> None:
    params = {"retro_id": "retro-20260725-120000", "retro_last": 5, "retro_min_evidence": 1, "retro_dry_run": False}
    out = ensure_retro_params(params)
    assert out["retro_id"] == "retro-20260725-120000"


def test_ensure_retro_params_fills_none() -> None:
    params: dict[str, object] = {"retro_id": None, "retro_last": 10, "retro_min_evidence": 2, "retro_dry_run": False}
    out = ensure_retro_params(params)
    assert isinstance(out["retro_id"], str)
    assert out["retro_id"].startswith("retro-")


def test_ensure_retro_params_fills_whitespace_only() -> None:
    params = {"retro_id": "   ", "retro_last": 10, "retro_min_evidence": 2, "retro_dry_run": False}
    out = ensure_retro_params(params)
    assert out["retro_id"].startswith("retro-")


def test_ensure_retro_params_does_not_mutate_input() -> None:
    params: dict[str, object] = {"retro_id": "", "retro_last": 10, "retro_min_evidence": 2, "retro_dry_run": False}
    original = dict(params)
    ensure_retro_params(params)
    assert params == original


def test_ensure_retro_params_rejects_unsafe_id() -> None:
    from assurance_agent.identifiers import UnsafeIdentifierError

    params = {"retro_id": "../evil", "retro_last": 10, "retro_min_evidence": 2, "retro_dry_run": False}
    with pytest.raises(UnsafeIdentifierError):
        ensure_retro_params(params)


def test_ensure_retro_params_generated_id_is_path_safe() -> None:
    params: dict[str, object] = {"retro_id": "", "retro_last": 10, "retro_min_evidence": 2, "retro_dry_run": False}
    out = ensure_retro_params(params)
    rid = out["retro_id"]
    assert isinstance(rid, str)
    assert re.match(r"^[a-zA-Z0-9._-]+$", rid), f"unsafe generated retro_id: {rid!r}"
