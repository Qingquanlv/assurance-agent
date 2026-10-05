from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, get_args, get_origin, get_type_hints

from pydantic import TypeAdapter

from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.flow import Flow, root_schemas

from assurance_product.models import PRODUCT_ENTRYPOINTS, ProductPublicOutput

STATE_SCHEMA_VERSION = "6"
ENTRYPOINT_RECURSION_LIMITS: MappingProxyType[str, int] = MappingProxyType(
    {
        "intake": 2048,
        "full": 8192,
        "init": 512,
        "retro": 2048,
        "issue-review": 512,
        "issue-analyze": 512,
        "issue-reconcile": 512,
    }
)


def _schema_name(schema: type) -> str:
    module = getattr(schema, "__module__", "")
    name = getattr(schema, "__qualname__", None) or getattr(schema, "__name__", "")
    if module:
        return f"{module}.{name}"
    return name


def _is_reducer(extra: object) -> bool:
    return callable(extra) and not isinstance(extra, type)


def _field_state_projection(name: str, hint: object) -> JSONValue:
    origin = get_origin(hint)
    reducers: list[JSONValue] = []
    schema_hint = hint
    if origin is Annotated:
        annotated_origin, *metadata = get_args(hint)
        remaining: list[object] = []
        for extra in metadata:
            if _is_reducer(extra):
                reducers.append({"module": extra.__module__, "qualname": extra.__qualname__})
                continue
            remaining.append(extra)
        schema_hint = annotated_origin if not remaining else Annotated[annotated_origin, *remaining]
    try:
        schema = TypeAdapter(schema_hint).json_schema()
    except Exception as error:
        raise TypeError(f"state field {name!r} cannot produce JSON schema") from error
    if not isinstance(schema, dict):
        raise TypeError(f"state field {name!r} JSON schema must be an object")
    return {"schema": schema, "reducers": reducers}


def _typeddict_digest(schema: type) -> str:
    hints = get_type_hints(schema, include_extras=True)
    return canonical_digest({name: _field_state_projection(name, hints[name]) for name in sorted(hints)})


def _model_schema_digest(model: type) -> str:
    schema = model.model_json_schema()
    if not isinstance(schema, dict):
        raise TypeError("model JSON schema must be an object")
    return canonical_digest(schema)


def contract_for_root(name: str, flow: Flow) -> EntrypointGraphContract:
    """Input and output digests are JSON schemas. State is reducer ids plus TypeAdapter JSON schema."""
    if name not in ENTRYPOINT_RECURSION_LIMITS:
        raise KeyError(name)
    state_type, _, _, _ = root_schemas(flow)
    return EntrypointGraphContract(
        name=name,
        input_model=_schema_name(flow.input),
        output_model=_schema_name(ProductPublicOutput),
        state_model=_schema_name(state_type),
        input_schema_digest=_model_schema_digest(flow.input),
        output_schema_digest=_model_schema_digest(ProductPublicOutput),
        state_schema_digest=_typeddict_digest(state_type),
        state_schema_version=STATE_SCHEMA_VERSION,
        recursion_limit=ENTRYPOINT_RECURSION_LIMITS[name],
    )


def contracts_from_roots(roots: Mapping[str, Flow]) -> MappingProxyType[str, EntrypointGraphContract]:
    if set(roots) != set(PRODUCT_ENTRYPOINTS):
        raise RuntimeError("root flows must cover every public Product entrypoint")
    return MappingProxyType({name: contract_for_root(name, roots[name]) for name in sorted(roots)})


if set(ENTRYPOINT_RECURSION_LIMITS) != set(PRODUCT_ENTRYPOINTS):
    raise RuntimeError("entrypoint recursion limits must cover every public Product entrypoint")


def digest(contract: EntrypointGraphContract) -> str:
    return canonical_digest(contract.canonical_projection())


def canonical_contract_projection(contract: EntrypointGraphContract) -> dict[str, JSONValue]:
    return contract.canonical_projection()


__all__ = [
    "ENTRYPOINT_RECURSION_LIMITS",
    "STATE_SCHEMA_VERSION",
    "canonical_contract_projection",
    "contract_for_root",
    "contracts_from_roots",
    "digest",
]
