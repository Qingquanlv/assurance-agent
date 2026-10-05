"""api codegen review: check the authored suite against the frozen plan."""

from __future__ import annotations

from assurance_generation.ops.api_codegen_review import hooks
from assurance_generation.ops.codegen_factory import build_codegen_review

op = build_codegen_review("api", hooks)

__all__ = ["op"]
