from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_ACTIVATION_VALUE_LENGTH = 64
_CANONICAL_TRIGGER_ID = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_ROOT_ACTIVATION_VALUE = "1"


class BusinessActivation(FrozenModel):
    kind: Literal["root", "round", "trigger"]
    value: str = Field(min_length=1, max_length=_MAX_ACTIVATION_VALUE_LENGTH)

    @model_validator(mode="after")
    def _validate_canonical_value(self) -> BusinessActivation:
        if self.value != self.value.strip():
            raise ValueError("business activation value must be canonical")
        if self.kind == "root" and self.value != _ROOT_ACTIVATION_VALUE:
            raise ValueError("root business activation must use the fixed one-shot value")
        if self.kind == "round":
            if not self.value.isdigit() or (len(self.value) > 1 and self.value.startswith("0")):
                raise ValueError("round business activation must be a canonical integer")
        if self.kind == "trigger" and _CANONICAL_TRIGGER_ID.fullmatch(self.value) is None:
            raise ValueError("trigger arrival id is not canonical")
        return self

    @classmethod
    def one_shot(cls) -> BusinessActivation:
        return cls(kind="root", value=_ROOT_ACTIVATION_VALUE)

    @classmethod
    def for_round(cls, index: int) -> BusinessActivation:
        if index < 0:
            raise ValueError("business round must not be negative")
        return cls(kind="round", value=str(index))

    @classmethod
    def for_trigger(cls, arrival_id: str) -> BusinessActivation:
        if not isinstance(arrival_id, str) or not arrival_id.strip():
            raise ValueError("trigger arrival id must be nonempty")
        if arrival_id != arrival_id.strip():
            raise ValueError("trigger arrival id must be canonical")
        if len(arrival_id) > _MAX_ACTIVATION_VALUE_LENGTH:
            raise ValueError("trigger arrival id exceeds bound")
        if _CANONICAL_TRIGGER_ID.fullmatch(arrival_id) is None:
            raise ValueError("trigger arrival id is not canonical")
        return cls(kind="trigger", value=arrival_id)


class AttemptKey(FrozenModel):
    digest: str = Field(pattern=_SHA256_PATTERN)


def derive_attempt_key(
    *,
    invocation_id: str,
    graph_revision: str,
    public_entrypoint: str,
    semantic_node_id: str,
    business_activation: BusinessActivation,
    contract_id: str,
    validated_input: BaseModel,
) -> AttemptKey:
    if not isinstance(validated_input, BaseModel):
        raise TypeError("validated_input must be a Pydantic model")
    input_payload: JSONValue = validated_input.model_dump(mode="json")
    projection: JSONValue = {
        "invocation_id": invocation_id,
        "graph_revision": graph_revision,
        "public_entrypoint": public_entrypoint,
        "semantic_node_id": semantic_node_id,
        "business_activation": {
            "kind": business_activation.kind,
            "value": business_activation.value,
        },
        "contract_id": contract_id,
        "task_input_digest": canonical_digest(input_payload),
    }
    return AttemptKey(digest=canonical_digest(projection))


__all__ = [
    "AttemptKey",
    "BusinessActivation",
    "derive_attempt_key",
]
