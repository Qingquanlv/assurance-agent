"""Audited gate-read path patterns — which gate reads participate in tamper detection.

Mirror of TS ``src/workflow/core/audit_scope.ts``. Only paths matching these
patterns are hashed into ``gate_verdict.reads_sha256`` and later re-checked by
the read-side audit.
"""

from __future__ import annotations

import re

AUDITED_GATE_READS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^review/[^/]+\.json$"),
    re.compile(r"^repo:\.aa/data-knowledge\.yaml$"),
    re.compile(r"^healing/fixer-safety-check\.json$"),
    re.compile(r"^healing/(api|e2e)-apply-summary\.json$"),
    re.compile(r"^inspect/inspect-safety-check\.json$"),
)


def is_audited_gate_read(rel_path: str) -> bool:
    return any(pattern.match(rel_path) for pattern in AUDITED_GATE_READS)
