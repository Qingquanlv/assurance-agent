"""Read-only port for one invocation's organized attempt evidence.

The framework returns a JSON document. It does not read the journal and does
not know which product built the document. The host supplies that projection
and drops the attempt that is asking, which is still open.
"""

from __future__ import annotations

from typing import Protocol

from graph_engine.canonical import JSONValue

RUNTIME_EVIDENCE = "runtime_evidence"


class RuntimeEvidenceSource(Protocol):
    async def project(
        self,
        *,
        invocation_id: str,
        exclude_attempt_key_digest: str,
    ) -> JSONValue: ...


__all__ = ["RUNTIME_EVIDENCE", "RuntimeEvidenceSource"]
