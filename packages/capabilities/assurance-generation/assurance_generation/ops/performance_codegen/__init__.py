"""performance codegen: seed locked tests, then author the family suite."""

from __future__ import annotations

from assurance_generation.ops.codegen_factory import build_codegen
from assurance_generation.ops.performance_codegen import hooks

op = build_codegen("performance", hooks)

__all__ = ["op"]
