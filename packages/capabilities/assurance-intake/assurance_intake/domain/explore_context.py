"""Read the sealed or draft exploration document shared by every downstream stage."""

from __future__ import annotations

import json

from assurance_intake.contracts.explore import ExploreAdvisoryV1, PreparedExploreV1


def load_exploration_document(data: bytes) -> ExploreAdvisoryV1 | PreparedExploreV1:
    """Read official PreparedExploreV1 when sealed; otherwise the authoring draft."""
    try:
        payload = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("exploration artifact is invalid") from error
    coverage = payload.get("minimum_required_coverage") if isinstance(payload, dict) else None
    first = coverage[0] if isinstance(coverage, list) and coverage else None
    try:
        if isinstance(first, dict) and "mrc_id" in first:
            return PreparedExploreV1.model_validate(payload)
        return ExploreAdvisoryV1.model_validate(payload)
    except ValueError as error:
        raise ValueError("exploration artifact is invalid") from error
