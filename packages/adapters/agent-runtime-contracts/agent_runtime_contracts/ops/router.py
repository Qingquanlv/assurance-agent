"""Declared capability ops: one declaration per op, one entry for every handler id.

A capability declares each op once in ``ops/<op>/__init__.py`` through
``router.agent`` or ``router.task``. The router derives attempt contracts,
handler ids, output routes, and the resource manifest from those
declarations, and dispatches every handler id through the capability's single
module-level ``execute``.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Generic, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError, field_serializer, field_validator

from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.plugin_api import (
    AttemptContractRef,
    FrozenModel,
    ResourceClaims,
    TaskContext,
    TaskHandler,
    TaskOutcome,
)

from agent_runtime_contracts.ops.binding import validate_binding
from agent_runtime_contracts.ops.contract import AgentExecutionContract, AgentPhaseWriteClaims
from agent_runtime_contracts.ops.errors import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    validate_model,
    validate_output,
)
from agent_runtime_contracts.ops.request import prepared_outcome, result_contract_from, skill_request
from agent_runtime_contracts.wire.models import AgentRunResult, JSONValue, ResultContract
from agent_runtime_contracts.wire.schema import canonical_digest, freeze_json, thaw_json

InputT = TypeVar("InputT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
DepT = TypeVar("DepT")

_SKILL_FILE = "SKILL.md"
_ALT_SKILL_SUFFIX = ".SKILL.md"
_RESULT_FILE = "result.schema.json"


class OpRequest(Protocol):
    @property
    def capability_id(self) -> str: ...

    @property
    def target_capability_id(self) -> str | None: ...

    @property
    def input(self) -> object: ...

    @property
    def binding_data(self) -> object: ...


class WriteScopeError(RuntimeError):
    """An op wrote outside the claims it declared for the current phase."""


class AgentOpFinalizeInputV1(FrozenModel):
    """Finalize envelope for declared Agent ops: prepared business input plus run evidence."""

    prepare: JSONValue
    agent_result: AgentRunResult

    @field_validator("prepare", mode="after")
    @classmethod
    def _freeze_prepare(cls, value: JSONValue) -> Any:
        return freeze_json(value)

    @field_serializer("prepare")
    def _serialize_prepare(self, value: object) -> Any:
        return thaw_json(value)


def _canonical_relative(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValueError(f"path must be canonical and relative: {relative!r}")
    return path


def _claimed(relative: str, claims: tuple[str, ...]) -> bool:
    return any(relative == claim or relative.startswith(f"{claim}/") for claim in claims)


def _write_claimed(root: Path, relative: str, data: bytes, claims: tuple[str, ...], *, phase: str) -> None:
    path = _canonical_relative(relative)
    if not _claimed(relative, claims):
        raise WriteScopeError(f"{phase} write is outside the declared claims: {relative}")
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise WriteScopeError(f"{phase} write must not traverse a symlink: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve().is_relative_to(root.resolve()):
        raise WriteScopeError(f"{phase} write escapes the write root: {relative}")
    target.write_bytes(data)


class PrepareContext:
    """Prepare-phase view: workspace roots, prepare write claims, and request shaping."""

    def __init__(self, op: AgentOp[Any, Any, Any], task: TaskContext) -> None:
        self.op = op
        self.project_root = task.project_root
        self.write_root = task.write_root
        self._deps: dict[Callable[..., object], object] = {}
        self._skill: str | None = None
        self._extra: dict[str, JSONValue] = {}

    def dep(self, dependency: Callable[..., DepT]) -> DepT:
        """Resolved value of one of the op's declared ``depends``."""
        if dependency not in self._deps:
            raise LookupError(f"{self.op.name} does not depend on {dependency.__qualname__}")
        return cast(DepT, self._deps[dependency])

    def write(self, relative: str, data: bytes) -> None:
        _write_claimed(self.write_root, relative, data, self.op.prepare_writes, phase="prepare")

    def use_skill(self, key: str) -> None:
        """Send the op's alternative ``<key>.SKILL.md`` instead of ``SKILL.md``."""
        if key not in self.op.skills:
            raise LookupError(f"{self.op.name} declares no alternative skill {key!r}")
        self._skill = key

    def extra(self, name: str, value: JSONValue) -> None:
        """Add a prompt-only field next to the business input."""
        self._extra[name] = value


class FinalizeContext:
    """Finalize-phase view: workspace roots, run evidence, and finalize write claims."""

    def __init__(self, op: AgentOp[Any, Any, Any], task: TaskContext, agent_result: AgentRunResult) -> None:
        self.op = op
        self.project_root = task.project_root
        self.write_root = task.write_root
        self.agent_result = agent_result

    def write(self, relative: str, data: bytes) -> None:
        _write_claimed(self.write_root, relative, data, self.op.finalize_writes, phase="finalize")


Dependency = Callable[[PrepareContext, InputT], object]
Before = Callable[[PrepareContext, InputT], InputT]
After = Callable[[FinalizeContext, InputT, ResultT], OutputT | Mapping[str, Any]]
OnOutputError = Callable[[FinalizeContext, InputT, OutputError], OutputT]
Outputs = Callable[[InputT], tuple[str, ...]]
Run = Callable[[TaskContext, InputT], OutputT]


def _sorted_paths(paths: tuple[str, ...], *, kind: str) -> tuple[str, ...]:
    for path in paths:
        _canonical_relative(path)
    if len(set(paths)) != len(paths):
        raise ValueError(f"{kind} must be unique")
    return tuple(sorted(paths))


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentOp(Generic[InputT, ResultT, OutputT]):
    """One Agent operation: prepare builds the run request, finalize seals the result."""

    router: OpRouter
    name: str
    profile: str
    skill: str
    persona: str
    input: type[InputT]
    result: type[ResultT]
    output: type[OutputT]
    writes: tuple[str, ...]
    prepare_writes: tuple[str, ...] = ()
    finalize_writes: tuple[str, ...] = ()
    routes: tuple[str, ...] | None = None
    outputs: Outputs[InputT] | None = None
    skills: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    depends: tuple[Dependency[InputT], ...] = ()
    before: Before[InputT] | None = None
    after: After[InputT, ResultT, OutputT] | None = None
    on_output_error: OnOutputError[InputT, OutputT] | None = None
    input_errors: tuple[type[Exception], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "writes", _sorted_paths(self.writes, kind="writes"))
        object.__setattr__(self, "prepare_writes", _sorted_paths(self.prepare_writes, kind="prepare_writes"))
        object.__setattr__(
            self, "finalize_writes", _sorted_paths(self.finalize_writes, kind="finalize_writes")
        )
        if self.routes is not None:
            object.__setattr__(self, "routes", _sorted_paths(self.routes, kind="routes"))
        object.__setattr__(self, "skills", MappingProxyType(dict(self.skills)))

    @property
    def directory(self) -> str:
        return self.name.replace("-", "_")

    @property
    def contract_id(self) -> str:
        return f"{self.router.owner}.agent.{self.name}.v1"

    @property
    def prepare_handler_id(self) -> str:
        return f"{self.router.owner}.{self.name}.prepare"

    @property
    def finalize_handler_id(self) -> str:
        return f"{self.router.owner}.{self.name}.finalize"

    @property
    def result_schema_id(self) -> str:
        return f"{self.router.owner}.result.{self.name}.v1"

    def contract(self) -> AgentExecutionContract[InputT, ResultT, OutputT]:
        prepare = self.prepare_writes
        finalize = self.finalize_writes
        runtime = tuple(path for path in self.writes if path not in prepare and path not in finalize)
        return AgentExecutionContract(
            contract_id=self.contract_id,
            owner_id=self.router.owner,
            prepare_handler_id=self.prepare_handler_id,
            finalize_handler_id=self.finalize_handler_id,
            skill_id=self.skill,
            agent_profile=self.profile,
            input_model=self.input,
            agent_result_model=self.result,
            output_model=self.output,
            resources=ResourceClaims(
                reads=self.router.reads,
                writes=tuple(sorted({*self.writes, *prepare, *finalize})),
            ),
            retry=self.router.agent_retry,
            timeout=self.router.timeout,
            validators=(),
            phase_write_claims=AgentPhaseWriteClaims(prepare=prepare, runtime=runtime, finalize=finalize),
        )

    def resource_files(self) -> dict[str, str]:
        owner = self.router.owner
        base = f"ops/{self.directory}"
        manifest = {
            f"{owner}.skill.{self.skill}.v1": f"{base}/{_SKILL_FILE}",
            self.result_schema_id: f"{base}/{_RESULT_FILE}",
            f"{owner}.persona.{self.persona}.v1": f"personas/{self.persona}.md",
        }
        for filename in self.router.list_files(base):
            if filename == _SKILL_FILE or not filename.endswith(".md"):
                continue
            if filename.endswith(_ALT_SKILL_SUFFIX):
                key = filename.removesuffix(_ALT_SKILL_SUFFIX)
                if key not in self.skills:
                    raise ValueError(f"{base}/{filename} is not declared in skills")
                manifest[f"{owner}.skill.{self.skills[key]}.v1"] = f"{base}/{filename}"
            else:
                manifest[f"{owner}.skill.{self.skill}.{filename.removesuffix('.md')}.v1"] = (
                    f"{base}/{filename}"
                )
        for key in self.skills:
            if f"{base}/{key}{_ALT_SKILL_SUFFIX}" not in manifest.values():
                raise ValueError(
                    f"{self.name} declares skill {key!r} without {base}/{key}{_ALT_SKILL_SUFFIX}"
                )
        return manifest

    def result_contract(self) -> ResultContract:
        path = f"ops/{self.directory}/{_RESULT_FILE}"
        return result_contract_from(self.result_schema_id, json.loads(self.router.resource_bytes(path)))

    def prepare(self, request: OpRequest, task: TaskContext) -> TaskOutcome:
        try:
            business = validate_model(self.input, request.input)
            binding = validate_binding(request.binding_data)
            ctx = PrepareContext(self, task)
            for dependency in self.depends:
                ctx._deps[dependency] = self.router.resolve(dependency)(ctx, business)
            if self.before is not None:
                business = self.before(ctx, business)
            if ctx._skill is None:
                skill_path = f"ops/{self.directory}/{_SKILL_FILE}"
            else:
                skill_path = f"ops/{self.directory}/{ctx._skill}{_ALT_SKILL_SUFFIX}"
            return prepared_outcome(
                skill_request(
                    skill_text=self.router.resource_text(skill_path),
                    persona_text=self.router.resource_text(f"personas/{self.persona}.md"),
                    business=cast(FrozenModel, business),
                    business_extra=ctx._extra or None,
                    binding=binding,
                    result=self.result_contract(),
                    roots=task,
                    allowed_outputs=self.writes if self.outputs is None else self.outputs(business),
                    scope_id=self.router.scope(business),
                )
            )
        except (InputError, ValidationError, *self.input_errors) as error:
            return failed_input(error)

    def finalize(self, request: OpRequest, task: TaskContext) -> TaskOutcome:
        try:
            envelope = validate_model(AgentOpFinalizeInputV1, request.input)
            prepared = thaw_json(envelope.prepare)
            if not isinstance(prepared, dict):
                raise InputError("prepared business input must be an object")
            fields = self.input.model_fields
            business = validate_model(
                self.input, {key: value for key, value in prepared.items() if key in fields}
            )
        except InputError as error:
            return failed_input(error)
        ctx = FinalizeContext(self, task, envelope.agent_result)
        try:
            try:
                result = validate_output(self.result, thaw_json(envelope.agent_result.result_payload))
                produced: BaseModel | Mapping[str, Any] = (
                    result if self.after is None else self.after(ctx, business, result)
                )
            except OutputError as error:
                if self.on_output_error is None:
                    raise
                produced = self.on_output_error(ctx, business, error)
            data = produced.model_dump(mode="json") if isinstance(produced, BaseModel) else produced
            output = validate_output(self.output, data)
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskOp(Generic[InputT, OutputT]):
    """One deterministic operation: validated input in, validated output out."""

    router: OpRouter
    name: str
    input: type[InputT]
    output: type[OutputT]
    run: Run[InputT, OutputT]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    errors: tuple[type[Exception], ...] = (InputError,)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reads", _sorted_paths(self.reads, kind="reads"))
        object.__setattr__(self, "writes", _sorted_paths(self.writes, kind="writes"))

    @property
    def directory(self) -> str:
        return self.name.replace("-", "_")

    @property
    def contract_id(self) -> str:
        return f"{self.router.owner}.task.{self.name}"

    @property
    def handler_id(self) -> str:
        return f"{self.router.owner}.{self.name}"

    def contract(self) -> TaskAttemptContract[InputT, OutputT]:
        return TaskAttemptContract(
            contract_id=self.contract_id,
            owner_id=self.router.owner,
            handler_id=self.handler_id,
            input_model=self.input,
            output_model=self.output,
            resources=ResourceClaims(reads=self.reads, writes=self.writes),
            retry=self.router.task_retry,
            timeout=self.router.timeout,
            validators=(),
        )

    def execute(self, request: OpRequest, task: TaskContext) -> TaskOutcome:
        try:
            output = self.run(task, validate_model(self.input, request.input))
        except (InputError, *self.errors) as error:
            return failed_input(error)
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


Route = Callable[[OpRequest, TaskContext], TaskOutcome]


class OpRouter:
    """Registry of one capability's declared ops, discovered from ``<package>.<op>``."""

    def __init__(
        self,
        owner: str,
        *,
        package: str,
        reads: tuple[str, ...],
        agent_retry: AttemptRetryPolicy,
        task_retry: AttemptRetryPolicy,
        timeout: AttemptTimeoutPolicy,
        scope: Callable[[Any], str],
    ) -> None:
        root, _, _ = package.rpartition(".")
        if not root or root not in sys.modules:
            raise ValueError(f"op package must live inside an imported capability package: {package}")
        self.owner = owner
        self.package = package
        self.reads = _sorted_paths(reads, kind="reads")
        self.agent_retry = agent_retry
        self.task_retry = task_retry
        self.timeout = timeout
        self.scope = scope
        # Bind the capability package object now; resolving it by name later
        # can re-import it after a test harness pops sys.modules.
        self._files = files(sys.modules[root])
        self._ops: dict[str, AgentOp[Any, Any, Any] | TaskOp[Any, Any]] = {}
        self._overrides: dict[Callable[..., object], Callable[..., object]] = {}
        self._discovered = False

    def _register(self, op: AgentOp[Any, Any, Any] | TaskOp[Any, Any]) -> None:
        directories = {item.directory for item in self._ops.values()}
        if op.name in self._ops or op.directory in directories:
            raise ValueError(f"duplicate op declaration: {self.owner}.{op.name}")
        self._ops[op.name] = op

    def agent(
        self,
        name: str,
        *,
        profile: str,
        skill: str,
        persona: str,
        input: type[InputT],
        result: type[ResultT],
        output: type[OutputT],
        writes: tuple[str, ...],
        prepare_writes: tuple[str, ...] = (),
        finalize_writes: tuple[str, ...] = (),
        routes: tuple[str, ...] | None = None,
        outputs: Outputs[InputT] | None = None,
        skills: Mapping[str, str] | None = None,
        depends: tuple[Dependency[InputT], ...] = (),
        before: Before[InputT] | None = None,
        after: After[InputT, ResultT, OutputT] | None = None,
        on_output_error: OnOutputError[InputT, OutputT] | None = None,
        input_errors: tuple[type[Exception], ...] = (),
    ) -> AgentOp[InputT, ResultT, OutputT]:
        op = AgentOp(
            router=self,
            name=name,
            profile=profile,
            skill=skill,
            persona=persona,
            input=input,
            result=result,
            output=output,
            writes=writes,
            prepare_writes=prepare_writes,
            finalize_writes=finalize_writes,
            routes=routes,
            outputs=outputs,
            skills=MappingProxyType(dict(skills or {})),
            depends=depends,
            before=before,
            after=after,
            on_output_error=on_output_error,
            input_errors=input_errors,
        )
        self._register(op)
        return op

    def task(
        self,
        name: str,
        *,
        input: type[InputT],
        output: type[OutputT],
        run: Run[InputT, OutputT],
        reads: tuple[str, ...],
        writes: tuple[str, ...],
        errors: tuple[type[Exception], ...] = (InputError,),
    ) -> TaskOp[InputT, OutputT]:
        op = TaskOp(
            router=self,
            name=name,
            input=input,
            output=output,
            run=run,
            reads=reads,
            writes=writes,
            errors=errors,
        )
        self._register(op)
        return op

    def discover(self) -> None:
        """Import every ``<package>.<op>`` subpackage; each must declare exactly its own op."""
        if self._discovered:
            return
        self._discovered = True
        try:
            package = importlib.import_module(self.package)
            found = sorted(info.name for info in pkgutil.iter_modules(package.__path__) if info.ispkg)
            for name in found:
                importlib.import_module(f"{self.package}.{name}")
            declared = sorted(op.directory for op in self._ops.values())
            if declared != found:
                raise RuntimeError(
                    f"{self.package} op packages {found} disagree with declarations {declared}"
                )
        except BaseException:
            self._discovered = False
            raise

    def ops(self) -> Mapping[str, AgentOp[Any, Any, Any] | TaskOp[Any, Any]]:
        self.discover()
        return MappingProxyType(dict(sorted(self._ops.items())))

    def agent_ops(self) -> Mapping[str, AgentOp[Any, Any, Any]]:
        return MappingProxyType({name: op for name, op in self.ops().items() if isinstance(op, AgentOp)})

    def task_ops(self) -> Mapping[str, TaskOp[Any, Any]]:
        return MappingProxyType({name: op for name, op in self.ops().items() if isinstance(op, TaskOp)})

    def agent_contracts(self) -> Mapping[str, AgentExecutionContract[Any, Any, Any]]:
        return MappingProxyType({name: op.contract() for name, op in self.agent_ops().items()})

    def task_contracts(self) -> Mapping[str, TaskAttemptContract[Any, Any]]:
        return MappingProxyType({name: op.contract() for name, op in self.task_ops().items()})

    def output_routes(self) -> Mapping[str, tuple[str, ...]]:
        return MappingProxyType(
            {name: op.writes if op.routes is None else op.routes for name, op in self.agent_ops().items()}
        )

    def attempt_contract_refs(self) -> tuple[AttemptContractRef, ...]:
        contracts = (*self.agent_contracts().values(), *self.task_contracts().values())
        return tuple(
            sorted(
                (
                    AttemptContractRef(
                        contract_id=contract.contract_id,
                        digest=canonical_digest(cast(JSONValue, contract.canonical_projection())),
                    )
                    for contract in contracts
                ),
                key=lambda item: item.contract_id,
            )
        )

    def resource_files(self) -> Mapping[str, str]:
        manifest: dict[str, str] = {}
        for op in self.agent_ops().values():
            for resource_id, path in op.resource_files().items():
                if manifest.setdefault(resource_id, path) != path:
                    raise ValueError(f"resource id is declared twice: {resource_id}")
        return MappingProxyType(dict(sorted(manifest.items())))

    def resource_bytes(self, relative: str) -> bytes:
        return self._files.joinpath(*_canonical_relative(relative).parts).read_bytes()

    def resource_text(self, relative: str) -> str:
        return self.resource_bytes(relative).decode("utf-8")

    def list_files(self, relative: str) -> tuple[str, ...]:
        directory = self._files.joinpath(*_canonical_relative(relative).parts)
        return tuple(sorted(item.name for item in directory.iterdir() if item.is_file()))

    def routes(self) -> Mapping[str, Route]:
        table: dict[str, Route] = {}
        for op in self.ops().values():
            if isinstance(op, AgentOp):
                table[op.prepare_handler_id] = op.prepare
                table[op.finalize_handler_id] = op.finalize
            else:
                table[op.handler_id] = op.execute
        return MappingProxyType(table)

    def handlers(self, entry: TaskHandler) -> Mapping[str, TaskHandler]:
        """Map every declared handler id to the capability's single entry module."""
        return MappingProxyType({handler_id: entry for handler_id in sorted(self.routes())})

    async def execute(self, request: OpRequest, context: TaskContext) -> TaskOutcome:
        handler_id = request.target_capability_id or request.capability_id
        route = self.routes().get(handler_id)
        if route is None:
            return TaskOutcome.failed(
                "configuration", f"no declared op handles {handler_id}", retryable=False
            )
        return route(request, context)

    def resolve(self, dependency: Callable[..., DepT]) -> Callable[..., DepT]:
        return cast(Callable[..., DepT], self._overrides.get(dependency, dependency))

    @contextmanager
    def override(
        self, dependency: Callable[..., object], replacement: Callable[..., object]
    ) -> Iterator[None]:
        """Swap one declared dependency for a test double inside the ``with`` block."""
        previous = self._overrides.get(dependency)
        self._overrides[dependency] = replacement
        try:
            yield
        finally:
            if previous is None:
                del self._overrides[dependency]
            else:
                self._overrides[dependency] = previous


__all__ = [
    "AgentOp",
    "AgentOpFinalizeInputV1",
    "FinalizeContext",
    "OpRequest",
    "OpRouter",
    "PrepareContext",
    "TaskOp",
    "WriteScopeError",
]
