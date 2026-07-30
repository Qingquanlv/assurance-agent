"""Normalized four-profile assurance registry manifest and digest."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from assurance_agent.artifacts.models.assurance import LAYER_NAMES, PLAN_CHECK_IDS
from assurance_agent.verification.checks.registry import CHECKS_BY_ID
from assurance_agent.verification.profiles import iter_layer_assurance_profiles

PROFILE_MANIFEST_SCHEMA_VERSION = "1"


def normalized_assurance_profile_manifest() -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    for profile in iter_layer_assurance_profiles():
        applicable = [check_id for check_id in PLAN_CHECK_IDS if check_id in profile.applicable_check_ids]
        profiles.append(
            {
                "layer": profile.layer,
                "case_type": profile.case_type,
                "plan_artifacts": list(profile.plan_artifacts),
                "review_artifact": profile.review_artifact,
                "review_alias": profile.review_alias,
                "checks_artifact": profile.checks_artifact,
                "gate_id": profile.gate_id,
                "applicable_check_ids": applicable,
                "capability_contract_enabled": profile.capability_contract_enabled,
                "review_model": f"{profile.review_model.__module__}.{profile.review_model.__name__}",
            }
        )

    check_catalog = [
        {
            "check_id": check_id,
            "callable": f"{CHECKS_BY_ID[check_id].__module__}.{CHECKS_BY_ID[check_id].__qualname__}",
        }
        for check_id in PLAN_CHECK_IDS
    ]

    return {
        "schema_version": PROFILE_MANIFEST_SCHEMA_VERSION,
        "layer_names": list(LAYER_NAMES),
        "plan_check_ids": list(PLAN_CHECK_IDS),
        "profiles": profiles,
        "check_catalog": check_catalog,
    }


def assurance_profile_bytes(manifest: dict[str, Any] | None = None) -> bytes:
    payload = manifest if manifest is not None else normalized_assurance_profile_manifest()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (canonical + "\n").encode("utf-8")


def assurance_profile_digest(manifest: dict[str, Any] | None = None) -> str:
    return hashlib.sha256(assurance_profile_bytes(manifest)).hexdigest()
