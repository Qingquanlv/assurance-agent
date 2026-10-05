"""e2e codegen review: check the authored suite against the frozen plan."""

from __future__ import annotations

from assurance_generation.ops.codegen_factory import build_codegen_review
from assurance_generation.ops.e2e_codegen_review import hooks

op = build_codegen_review("e2e", hooks)

__all__ = ["op"]
