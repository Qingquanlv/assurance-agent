"""C3 seed-replay telemetry: pure rate + API adversarial expansion gate (M3 Task 3).

Receipts are immutable discovery artifacts. This module never invents a vacuous
``0/0 = 1.0`` rate: zero receipts → ``rate is None`` (not_evaluated). C3 is
**not** a ``MetricKey`` and does not enter single-change verdict floors.
I/O (load/write) lives in ``workflow.discovery.replay_receipts``.
"""

from __future__ import annotations

from collections.abc import Sequence

from assurance_kernel.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_kernel.artifacts.models.discovery import ReplayAttemptReceipt

# Design §5-C3: below 1.0 = flaky. API e2e/fuzz adversarial expansion requires
# meeting this floor first (helper only — collectors for other layers not added).
DEFAULT_API_C3_MIN_RATE: float = 1.0

REPLAY_RECEIPT_DIR_REL = "discovery/counterexamples"


def compute_seed_replay_rate(
    receipts: Sequence[ReplayAttemptReceipt],
) -> tuple[int, int, float | None]:
    """Pure C3 rate: ``(success, attempts, rate|None)``.

    ``success`` counts receipts whose outcome is oracle ``violate`` (reproduced).
    Zero attempts → ``rate is None`` (not_evaluated); never ``0/0 = 1.0``.
    """
    attempts = len(receipts)
    if attempts == 0:
        return 0, 0, None
    success = sum(1 for receipt in receipts if receipt.outcome == "violate")
    return success, attempts, success / attempts


def api_adversarial_expansion_allowed(rate: float | None, threshold: float) -> bool:
    """Gate e2e/fuzz adversarial expansion behind API C3 rate.

    ``rate is None`` (not_evaluated) never allows expansion. Callers pass
    ``DEFAULT_API_C3_MIN_RATE`` unless a project constant overrides it.
    """
    if rate is None:
        return False
    return rate >= threshold


def receipt_payload_digest(receipt: ReplayAttemptReceipt) -> str:
    """Content digest of a receipt excluding ``recorded_at`` (clock-stable)."""
    payload = receipt.model_dump(mode="json")
    payload.pop("recorded_at", None)
    return sha256_bytes(canonical_json_bytes(payload))


def replay_receipt_relpath(*, counterexample_id: str, attempt_index: int) -> str:
    """Change-relative path for one attempt receipt."""
    return f"{REPLAY_RECEIPT_DIR_REL}/{counterexample_id}/replay/attempt-{attempt_index}.json"


__all__ = [
    "DEFAULT_API_C3_MIN_RATE",
    "REPLAY_RECEIPT_DIR_REL",
    "api_adversarial_expansion_allowed",
    "compute_seed_replay_rate",
    "receipt_payload_digest",
    "replay_receipt_relpath",
]
