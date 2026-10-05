"""Read attempt ops without importing capability packages."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.flow.errors import FlowCheckError
from graph_engine.stategraph.ledger import InputBinding, NamedWrite


@dataclass(frozen=True, slots=True)
class OpView:
    contract_id: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    namespace: str
    writes: tuple[NamedWrite, ...]
    bindings: tuple[InputBinding, ...]

    def write_keys(self) -> frozenset[str]:
        return frozenset(f"{self.namespace}.{item.name}" for item in self.writes)


def view_op(op: object) -> OpView:
    """Adapt a ``TaskAttemptContract`` or an object with the flow op methods."""
    if isinstance(op, ResolvedAttemptContract):
        op = op.contract
    if isinstance(op, TaskAttemptContract):
        return _from_contract(op)
    return _from_protocol(op)


def _from_contract(contract: TaskAttemptContract[BaseModel, BaseModel]) -> OpView:
    namespace = contract.ledger_namespace()
    _namespace(namespace)
    return OpView(
        contract_id=contract.contract_id,
        input_model=contract.input_model,
        output_model=contract.output_model,
        namespace=namespace,
        writes=contract.ledger_writes(),
        bindings=contract.input_bindings(),
    )


def _from_protocol(op: object) -> OpView:
    required = (
        "contract_id",
        "input_model",
        "output_model",
        "ledger_namespace",
        "ledger_writes",
        "input_bindings",
    )
    missing = [name for name in required if not hasattr(op, name)]
    if missing:
        raise FlowCheckError(f"op is missing {', '.join(missing)}")
    contract_id = _text(_attribute(op, "contract_id"), "contract_id")
    input_model = _model(_attribute(op, "input_model"), "input_model")
    output_model = _model(_attribute(op, "output_model"), "output_model")
    namespace = _namespace(_method(op, "ledger_namespace"))
    writes = _writes(_method(op, "ledger_writes"))
    bindings = _bindings(_method(op, "input_bindings"))
    names = [item.name for item in writes]
    if len(names) != len(set(names)):
        raise FlowCheckError(f"{contract_id} names a write twice")
    fields = [item.field for item in bindings]
    if len(fields) != len(set(fields)):
        raise FlowCheckError(f"{contract_id} binds the same input slot twice")
    for binding in bindings:
        if binding.field not in input_model.model_fields:
            raise FlowCheckError(f"{contract_id} binds unknown input {binding.field}")
    return OpView(
        contract_id=contract_id,
        input_model=input_model,
        output_model=output_model,
        namespace=namespace,
        writes=writes,
        bindings=bindings,
    )


def _attribute(op: object, name: str) -> object:
    return getattr(op, name)


def _method(op: object, name: str) -> object:
    value = getattr(op, name)
    if not callable(value):
        raise FlowCheckError(f"{name}() must be callable")
    return value()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FlowCheckError(f"{label} must be a nonempty string")
    return value


def _namespace(value: object) -> str:
    namespace = _text(value, "ledger_namespace()")
    if namespace.startswith(".") or namespace.endswith("."):
        raise FlowCheckError(f"ledger namespace {namespace!r} is not canonical")
    return namespace


def _model(value: object, label: str) -> type[BaseModel]:
    if isinstance(value, type) and issubclass(value, BaseModel):
        return value
    raise FlowCheckError(f"{label} must be a Pydantic model")


def _writes(value: object) -> tuple[NamedWrite, ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, NamedWrite) for item in value):
        raise FlowCheckError("ledger_writes() must return a tuple of NamedWrite")
    return value


def _bindings(value: object) -> tuple[InputBinding, ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, InputBinding) for item in value):
        raise FlowCheckError("input_bindings() must return a tuple of InputBinding")
    return value


__all__ = ["OpView", "view_op"]
