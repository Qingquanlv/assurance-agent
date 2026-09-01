from __future__ import annotations

from pydantic import BaseModel
import pytest

from graph_engine.persistence.journal import strict_checkpoint_serializer


class UnsafeCheckpointObject:
    def __init__(self) -> None:
        self.secret = "not-for-checkpoint"


class SecretHandle:
    def __init__(self, token: str) -> None:
        self.token = token


class KernelService:
    def dispatch(self) -> str:
        return "live-service"


class ControlProjection(BaseModel):
    change_id: str
    receipt_digest: str


def test_strict_serializer_rejects_unapproved_python_type() -> None:
    serializer = strict_checkpoint_serializer()
    with pytest.raises((TypeError, ValueError)):
        serializer.dumps_typed(UnsafeCheckpointObject())


def test_strict_serializer_rejects_secrets_and_service_objects() -> None:
    serializer = strict_checkpoint_serializer()
    with pytest.raises((TypeError, ValueError)):
        serializer.dumps_typed(SecretHandle("super-secret"))
    with pytest.raises((TypeError, ValueError)):
        serializer.dumps_typed(KernelService())
    with pytest.raises((TypeError, ValueError)):
        serializer.dumps_typed({"secret": SecretHandle("super-secret")})
    with pytest.raises((TypeError, ValueError)):
        serializer.dumps_typed({"kernel": KernelService()})


def test_strict_serializer_accepts_data_only_builtins_and_pydantic_json() -> None:
    serializer = strict_checkpoint_serializer()
    assert serializer.pickle_fallback is False
    assert serializer._allowed_json_modules is None
    assert serializer._allowed_msgpack_modules is None
    encoded = serializer.dumps_typed(
        {
            "invocation_id": "inv-1",
            "round": 2,
            "receipts": [{"receipt_id": "r1", "receipt_digest": "b" * 64}],
            "decision": ControlProjection(
                change_id="chg-1",
                receipt_digest="b" * 64,
            ).model_dump(mode="json"),
        }
    )
    assert encoded[0] == "msgpack"
    assert encoded[1]
