"""performance codegen review: check the authored suite against the frozen plan."""

from __future__ import annotations

from assurance_generation.ops.codegen_factory import build_codegen_review
from assurance_generation.ops.performance_codegen_review import hooks

op = build_codegen_review("performance", hooks)

__all__ = ["op"]
