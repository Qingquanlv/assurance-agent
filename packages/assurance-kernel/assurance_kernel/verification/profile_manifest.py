"""Normalized four-profile assurance registry manifest and digest."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from assurance_kernel.artifacts.models.assurance import (
    LAYER_NAMES,
    PLAN_CHECK_IDS,
    CaseType,
    LayerName,
    PlanCheckId,
)
from assurance_kernel.verification.checks.registry import CHECKS_BY_ID
from assurance_kernel.verification.profiles import iter_layer_assurance_profiles

PROFILE_MANIFEST_SCHEMA_VERSION = "1"
PROFILE_SNAPSHOT_DIRECTORY = ".graph-runtime/assurance-profiles"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class AssuranceProfileManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    layer: LayerName
    case_type: CaseType
    plan_artifacts: tuple[str, ...]
    review_artifact: str
    review_alias: str
    checks_artifact: str
    gate_id: str
    applicable_check_ids: tuple[PlanCheckId, ...]
    capability_contract_enabled: bool
    review_model: str


class AssuranceCheckManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    check_id: PlanCheckId
    callable: str


class AssuranceProfileManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"]
    layer_names: tuple[LayerName, ...]
    plan_check_ids: tuple[PlanCheckId, ...]
    profiles: tuple[AssuranceProfileManifestEntry, ...]
    check_catalog: tuple[AssuranceCheckManifestEntry, ...]


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


def assurance_profile_snapshot_relpath(digest: str) -> str:
    if not _SHA256_HEX.fullmatch(digest):
        raise ValueError(f"assurance profile digest must be lowercase sha256 hex: {digest!r}")
    return f"{PROFILE_SNAPSHOT_DIRECTORY}/{digest}.json"


def parse_assurance_profile_snapshot(data: bytes) -> AssuranceProfileManifest:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"assurance profile snapshot is malformed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("assurance profile snapshot must be a JSON object")
    try:
        parsed = AssuranceProfileManifest.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"assurance profile snapshot is invalid: {exc}") from exc
    if parsed.layer_names != LAYER_NAMES:
        raise ValueError("assurance profile snapshot layer_names must follow canonical LAYER_NAMES order")
    if len(set(parsed.layer_names)) != len(parsed.layer_names):
        raise ValueError("assurance profile snapshot layer_names must be unique")
    if parsed.plan_check_ids != PLAN_CHECK_IDS:
        raise ValueError(
            "assurance profile snapshot plan_check_ids must follow canonical PLAN_CHECK_IDS order"
        )
    if len(set(parsed.plan_check_ids)) != len(parsed.plan_check_ids):
        raise ValueError("assurance profile snapshot plan_check_ids must be unique")
    profile_layers = tuple(entry.layer for entry in parsed.profiles)
    if profile_layers != LAYER_NAMES:
        raise ValueError("assurance profile snapshot profiles must follow canonical LAYER_NAMES order")
    if len(set(profile_layers)) != len(profile_layers):
        raise ValueError("assurance profile snapshot profiles must have unique layers")
    catalog_ids = tuple(entry.check_id for entry in parsed.check_catalog)
    if catalog_ids != PLAN_CHECK_IDS:
        raise ValueError(
            "assurance profile snapshot check_catalog must follow canonical PLAN_CHECK_IDS order"
        )
    if len(set(catalog_ids)) != len(catalog_ids):
        raise ValueError("assurance profile snapshot check_catalog must have unique check_ids")
    for entry in parsed.profiles:
        expected = tuple(check_id for check_id in PLAN_CHECK_IDS if check_id in entry.applicable_check_ids)
        if entry.applicable_check_ids != expected:
            raise ValueError(
                "assurance profile snapshot applicable_check_ids must follow canonical PLAN_CHECK_IDS order"
            )
        if len(set(entry.applicable_check_ids)) != len(entry.applicable_check_ids):
            raise ValueError("assurance profile snapshot applicable_check_ids must be unique")
    if assurance_profile_bytes(parsed.model_dump(mode="json")) != data:
        raise ValueError("assurance profile snapshot bytes are not canonical")
    return parsed
