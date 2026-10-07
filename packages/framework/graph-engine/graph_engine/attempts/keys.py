from __future__ import annotations

import re
from collections.abc import Mapping
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


class AttemptIdentity(FrozenModel):
    """The shared activation identity in both live keys and retained generation scopes."""

    invocation_id: str
    graph_revision: str
    public_entrypoint: str
    semantic_node_id: str
    business_activation: BusinessActivation
    contract_id: str

    @classmethod
    def from_scope(cls, scope: Mapping[str, JSONValue]) -> AttemptIdentity:
        # A generation scope also carries contract metadata, which is not key material.
        return cls.model_validate({name: scope[name] for name in cls.model_fields})

    def derive_key(self, input_payload: JSONValue, *, technical_attempt: int = 1) -> AttemptKey:
        if isinstance(technical_attempt, bool) or not isinstance(technical_attempt, int):
            raise TypeError("technical_attempt must be an integer")
        if technical_attempt < 1:
            raise ValueError("technical_attempt must be positive")
        projection: JSONValue = {
            "invocation_id": self.invocation_id,
            "graph_revision": self.graph_revision,
            "public_entrypoint": self.public_entrypoint,
            "semantic_node_id": self.semantic_node_id,
            "business_activation": {
                "kind": self.business_activation.kind,
                "value": self.business_activation.value,
            },
            "contract_id": self.contract_id,
            "technical_attempt": technical_attempt,
            "task_input_digest": canonical_digest(input_payload),
        }
        return AttemptKey(digest=canonical_digest(projection))


def derive_attempt_key(
    *,
    invocation_id: str,
    graph_revision: str,
    public_entrypoint: str,
    semantic_node_id: str,
    business_activation: BusinessActivation,
    contract_id: str,
    validated_input: BaseModel,
    technical_attempt: int = 1,
) -> AttemptKey:
    if not isinstance(validated_input, BaseModel):
        raise TypeError("validated_input must be a Pydantic model")
    return AttemptIdentity(
        invocation_id=invocation_id,
        graph_revision=graph_revision,
        public_entrypoint=public_entrypoint,
        semantic_node_id=semantic_node_id,
        business_activation=business_activation,
        contract_id=contract_id,
    ).derive_key(validated_input.model_dump(mode="json"), technical_attempt=technical_attempt)


__all__ = [
    "AttemptIdentity",
    "AttemptKey",
    "BusinessActivation",
    "derive_attempt_key",
]
