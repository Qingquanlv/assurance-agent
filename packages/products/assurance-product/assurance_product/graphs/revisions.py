from __future__ import annotations

from types import MappingProxyType
from typing import get_type_hints

from pydantic import BaseModel

from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.canonical import JSONValue, canonical_digest

from assurance_product.graphs.state import ProductState
from assurance_product.models import PRODUCT_ENTRYPOINTS, ProductInputV1, ProductPublicOutput

STATE_SCHEMA_VERSION = "1"
ENTRYPOINT_RECURSION_LIMITS: MappingProxyType[str, int] = MappingProxyType(
    {
        "intake": 2048,
        "case": 1024,
        "full": 8192,
        "execute": 4096,
        "archive": 512,
        "retro": 2048,
        "issue-review": 512,
        "issue-analyze": 512,
        "issue-reconcile": 512,
        "improvement-review": 512,
        "improvement-evaluate": 512,
        "improvement-export": 512,
        "improvement-apply": 1024,
        "improvement-rollback": 512,
    }
)


def _schema_digest(model: type[BaseModel]) -> str:
    return canonical_digest(model.model_json_schema())


def _product_state_schema_digest() -> str:
    hints = get_type_hints(ProductState, include_extras=True)
    return canonical_digest({name: str(hints[name]) for name in sorted(hints)})


_INPUT_MODEL = "assurance_product.models.ProductInputV1"
_OUTPUT_MODEL = "assurance_product.models.ProductPublicOutput"
_STATE_MODEL = "assurance_product.graphs.state.ProductState"
_INPUT_DIGEST = _schema_digest(ProductInputV1)
_OUTPUT_DIGEST = _schema_digest(ProductPublicOutput)
_STATE_DIGEST = _product_state_schema_digest()


def _contract(name: str) -> EntrypointGraphContract:
    return EntrypointGraphContract(
        name=name,
        input_model=_INPUT_MODEL,
        output_model=_OUTPUT_MODEL,
        state_model=_STATE_MODEL,
        input_schema_digest=_INPUT_DIGEST,
        output_schema_digest=_OUTPUT_DIGEST,
        state_schema_digest=_STATE_DIGEST,
        state_schema_version=STATE_SCHEMA_VERSION,
        recursion_limit=ENTRYPOINT_RECURSION_LIMITS[name],
    )


if set(ENTRYPOINT_RECURSION_LIMITS) != set(PRODUCT_ENTRYPOINTS):
    raise RuntimeError("entrypoint recursion limits must cover every public Product entrypoint")

ENTRYPOINT_CONTRACTS: MappingProxyType[str, EntrypointGraphContract] = MappingProxyType(
    {name: _contract(name) for name in sorted(PRODUCT_ENTRYPOINTS)}
)


def digest(contract: EntrypointGraphContract) -> str:
    return canonical_digest(contract.canonical_projection())


def canonical_contract_projection(contract: EntrypointGraphContract) -> dict[str, JSONValue]:
    return contract.canonical_projection()


__all__ = [
    "ENTRYPOINT_CONTRACTS",
    "ENTRYPOINT_RECURSION_LIMITS",
    "STATE_SCHEMA_VERSION",
    "canonical_contract_projection",
    "digest",
]
