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
    # The trace-sufficiency gate's only read. Audited because its
    # `needs_human_review` route offers `accept_risk`: a decision has to be
    # anchored to the bytes it was taken on, or one taken over a thin projection
    # would keep excusing whatever the next fold produces.
    re.compile(r"^inspect/trace-sufficiency\.json$"),
    # metrics-sufficiency-gate's only read — same accept_risk anchoring.
    re.compile(r"^inspect/metrics\.json$"),
)


def is_audited_gate_read(rel_path: str) -> bool:
    return any(pattern.match(rel_path) for pattern in AUDITED_GATE_READS)
