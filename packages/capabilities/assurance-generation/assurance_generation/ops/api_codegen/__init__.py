"""api codegen: seed locked tests, then author the family suite."""

from __future__ import annotations

from assurance_generation.ops.api_codegen import hooks
from assurance_generation.ops.codegen_factory import build_codegen

op = build_codegen("api", hooks)

__all__ = ["op"]
