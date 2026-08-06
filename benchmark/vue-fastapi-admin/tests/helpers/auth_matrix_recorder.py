"""Recorder for executed authorization-matrix cells (§5-A3).

A matrix-generated parameterized test calls :func:`record_auth_cell` with the
status code the SUT actually returned. The row lands in the JSONL file named by
``AA_AUTH_MATRIX_RECORD`` (set by ``aa run`` for the api target), which the
runner joins with the pytest report to produce
``raw/auth-matrix-executions.json``.

Without that variable every call is a no-op, so a hand-run pytest neither writes
anything nor needs the harness. The status code travels through this channel
rather than being recovered from the test name because a cell counts as asserted
only with status-code proof, and a nodeid carries none.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TextIO

RECORD_ENV = "AA_AUTH_MATRIX_RECORD"

_PARAM_BRACKET = re.compile(r"\[([^\]]+)\]")
_handle: TextIO | None = None


def record_auth_cell(*, route: str, method: str, token: str, status_code: int) -> None:
    """Append one executed cell, tagged with the current parameterized id."""
    global _handle
    target = os.environ.get(RECORD_ENV)
    if not target:
        return
    if _handle is None:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        _handle = path.open("a", encoding="utf-8")
    row = {
        "route": route,
        "method": method,
        "token": token,
        "status_code": int(status_code),
        "parameterized_id": _current_parameterized_id(),
    }
    _handle.write(json.dumps(row, sort_keys=True) + "\n")


def close_auth_matrix_recorder() -> None:
    """Flush and drop the handle (session teardown; safe to call unrecorded)."""
    global _handle
    if _handle is not None:
        _handle.close()
        _handle = None


def _current_parameterized_id() -> str:
    """The ``[...]`` param of the running test, or ``""`` when unparameterized.

    Empty is recorded as-is: the runner drops rows it cannot join to a reported
    test rather than guessing which cell they belong to.
    """
    match = _PARAM_BRACKET.search(os.environ.get("PYTEST_CURRENT_TEST", ""))
    return match.group(1) if match is not None else ""


__all__ = ["RECORD_ENV", "close_auth_matrix_recorder", "record_auth_cell"]
