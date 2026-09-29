from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def _mark(fn: F, phase: str) -> F:
    if not inspect.iscoroutinefunction(fn):
        raise TypeError("agent lifecycle methods must be async")
    if hasattr(fn, "__agent_lifecycle_phase__"):
        raise TypeError("agent lifecycle method already has a phase")
    setattr(fn, "__agent_lifecycle_phase__", phase)
    return fn


def before(fn: F) -> F:
    return _mark(fn, "before")


def after(fn: F) -> F:
    return _mark(fn, "after")


def finally_(fn: F) -> F:
    return _mark(fn, "finally")


@dataclass(frozen=True, slots=True)
class FinallyContext:
    contract_id: str
    attempt_key: str
    mode: Literal["execute", "reconcile"]
    last_phase: Literal["prepare", "runtime", "finalize"] | None
    outcome: Literal["executed", "resolution", "exception"]
    error_type: str | None = None


def validate_task_type(task_type: type[object]) -> None:
    required = {"prepare": ("before", 3), "run": (None, 3), "finalize": ("after", 3)}
    expected = {**required, "on_exit": ("finally", 2)}
    for name, (phase, count) in expected.items():
        method = getattr(task_type, name, None)
        if method is None and name == "on_exit":
            continue
        if not inspect.iscoroutinefunction(method):
            raise TypeError(f"{task_type.__name__}.{name} must be async")
        if getattr(method, "__agent_lifecycle_phase__", None) != phase:
            raise TypeError(f"{task_type.__name__}.{name} has an invalid lifecycle phase")
        try:
            inspect.signature(method).bind(*(object() for _ in range(count)))
        except TypeError as error:
            raise TypeError(f"{task_type.__name__}.{name} has an invalid signature") from error
    for name, method in inspect.getmembers(task_type, inspect.isfunction):
        if name not in expected and hasattr(method, "__agent_lifecycle_phase__"):
            raise TypeError(f"{task_type.__name__}.{name} is an unexpected lifecycle method")


__all__ = ["FinallyContext", "after", "before", "finally_", "validate_task_type"]
