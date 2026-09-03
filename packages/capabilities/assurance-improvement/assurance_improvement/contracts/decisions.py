from __future__ import annotations

APPLY_HUMAN_ACTIONS: tuple[str, ...] = ("approve", "reject", "request_rework", "supersede")
AUTO_REVIEW_DECISIONS: tuple[str, ...] = ("pass", "changes_requested", "needs_human_review", "reject")
APPLY_EVALUATION_OUTCOMES: tuple[str, ...] = ("passed", "regressed", "awaiting_baseline", "error")


__all__ = [
    "APPLY_EVALUATION_OUTCOMES",
    "APPLY_HUMAN_ACTIONS",
    "AUTO_REVIEW_DECISIONS",
]
