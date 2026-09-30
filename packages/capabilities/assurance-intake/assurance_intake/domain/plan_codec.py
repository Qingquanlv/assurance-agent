"""Seal and decode frozen assurance plans at the operation boundary."""

from __future__ import annotations

import hashlib
import json
from typing import cast

from pydantic_core import to_jsonable_python

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_intake.contracts.plan import ResolvedAssurancePlan, plan_artifact_ref
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def seal_plan(payload: dict[str, object]) -> ResolvedAssurancePlan:
    if "plan_digest" in payload:
        raise ValueError("unsealed plan payload cannot supply plan_digest")
    projection = cast(JSONValue, to_jsonable_python(payload))
    digest = canonical_digest(projection)
    return ResolvedAssurancePlan.model_validate({**payload, "plan_digest": digest})


def decode_plan(data: bytes, ref: EvidenceArtifactRefV1) -> ResolvedAssurancePlan:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("plan is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("plan document must be an object")
    if canonical_json_bytes(cast(JSONValue, payload)) != data:
        raise ValueError("plan bytes must be canonical JSON")
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise ValueError("plan_ref digest does not match plan bytes")
    plan = ResolvedAssurancePlan.model_validate(payload)
    if plan_artifact_ref(plan) != ref:
        raise ValueError("plan_ref path does not match plan_digest")
    return plan
