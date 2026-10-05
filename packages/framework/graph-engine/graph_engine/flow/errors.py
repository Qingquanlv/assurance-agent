"""Compile-time failures for a flow declaration."""

from __future__ import annotations


class FlowCheckError(ValueError):
    """A flow declaration breaks a rule the compiler can check before execution."""


__all__ = ["FlowCheckError"]
