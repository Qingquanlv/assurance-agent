"""Declared capability ops: one declaration per op, one entry for every handler id.

A capability declares each op once in ``ops/<op>/__init__.py`` through
``router.agent`` or ``router.task``. The router derives attempt contracts,
handler ids, output routes, and the resource manifest from those
declarations, and dispatches every handler id through the capability's single
module-level ``execute``.
"""

from __future__ import annotations

import importlib
import hashlib
import pkgutil
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Generic, Literal, Protocol, TypeVar, cast

import yaml
from pydantic import BaseModel, ValidationError, field_serializer, field_validator

from graph_engine.artifacts import (
    ArtifactReadError,
    ArtifactRef,
    coerce_artifact_ref,
    open_artifact,
    read_workspace_file,
    stage_json_artifact,
)
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.plugin_api import (
    AttemptContractRef,
    FrozenModel,
    ResourceClaims,
    TaskContext,
    TaskHandler,
    TaskOutcome,
)
from graph_engine.stategraph.ledger import InputBinding, NamedWrite

from agent_runtime_contracts.wire.prepare import PreparedAgentRun
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
from agent_runtime_contracts.ops.receipt import ArtifactListResultV1
from agent_runtime_contracts.ops.request import result_contract_from, skill_request
from agent_runtime_contracts.wire.models import AgentRunRequest, AgentRunResult, JSONValue, ResultContract
from agent_runtime_contracts.wire.schema import (
    canonical_digest,
    freeze_json,
    result_schema_from_model,
    thaw_json,
)

InputT = TypeVar("InputT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
DepT = TypeVar("DepT")

_SKILL_FILE = "SKILL.md"
_SKILL_SUFFIX = ".SKILL.md"


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


def _plain_json(raw: object) -> object:
    """Thaw frozen wire JSON so prepare and finalize see ordinary dicts and lists."""

    if isinstance(raw, Mapping):
        return thaw_json(raw)
    return raw


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
        self._business: BaseModel | None = None
        self._deps: dict[object, object] = {}
        self._files: dict[str, object] = {}
        self._project_images: dict[str, bytes] = {}
        self._project_refs: dict[str, ArtifactRef] = {}
        self._extra: dict[str, JSONValue] = {}
        self._bound: dict[str, tuple[str, ...]] = {}

    def dep(self, dependency: Callable[..., DepT] | ArtifactHandle[DepT]) -> DepT:
        """Resolved value of one of the op's declared ``depends``."""
        return _cached_dep(self, dependency)

    def file(self, relative: str) -> object:
        return self._files[relative]

    def get_file(self, relative: str) -> object | None:
        return self._files.get(relative)

    def project_image(self, relative: str) -> bytes:
        return self._project_images[relative]

    def project_ref(self, relative: str) -> ArtifactRef:
        return self._project_refs[relative]

    def write(self, relative: str, data: bytes) -> None:
        _write_claimed(self.write_root, relative, data, self.op.prepare.claim_paths(), phase="prepare")

    def extra(self, name: str, value: JSONValue) -> None:
        """Add a prompt-only field next to the business input."""
        self._extra[name] = value

    def bind(self, root: str, paths: Iterable[str]) -> None:
        """Name this run's exact files under a ``Dir`` whose ``files`` needs the workspace."""
        if root in self._bound:
            raise ValueError(f"directory is already bound: {root}")
        self._bound[root] = tuple(paths)


class FinalizeContext:
    """Finalize-phase view: workspace roots, run evidence, and finalize write claims."""

    def __init__(
        self,
        op: AgentOp[Any, Any, Any],
        task: TaskContext,
        agent_result: AgentRunResult,
        prepared: Mapping[str, Any],
    ) -> None:
        self.op = op
        self.project_root = task.project_root
        self.write_root = task.write_root
        self.agent_result = agent_result
        self.prepared = prepared
        self._business: BaseModel | None = None
        self._deps: dict[object, object] = {}
        self._project_files: dict[str, object] = {}
        self._project_images: dict[str, bytes] = {}
        self._project_refs: dict[str, ArtifactRef] = {}
        self._files: dict[str, object] = {}
        self._images: dict[str, bytes] = {}
        self._refs: dict[str, ArtifactRef] = {}
        self._staged_paths: set[str] = set()

    def dep(self, dependency: ArtifactHandle[DepT]) -> DepT:
        """Decoded artifact dependency, resolved from the same attempt input as prepare."""
        return _cached_dep(self, dependency)

    def write(self, relative: str, data: bytes) -> None:
        _write_claimed(self.write_root, relative, data, self.op.finalize.claim_paths(), phase="finalize")

    def file(self, relative: str) -> object:
        """Typed value (or bytes) captured and validated before the After hook."""
        return self._files[relative]

    def project_file(self, relative: str) -> object:
        return self._project_files[relative]

    def get_project_file(self, relative: str) -> object | None:
        return self._project_files.get(relative)

    def project_image(self, relative: str) -> bytes:
        return self._project_images[relative]

    def project_ref(self, relative: str) -> ArtifactRef:
        return self._project_refs[relative]

    def ref(self, relative: str) -> ArtifactRef:
        return self._refs[relative]

    def images(self) -> Mapping[str, bytes]:
        return MappingProxyType(self._images)

    def staged_paths(self) -> frozenset[str]:
        return frozenset(self._staged_paths)

    def refs(self, paths: Iterable[str] | None = None) -> tuple[ArtifactRef, ...]:
        selected = self._refs if paths is None else {path: self._refs[path] for path in paths}
        return tuple(selected[path] for path in sorted(selected))

    def stage(self, relative: str, document: BaseModel) -> ArtifactRef:
        """Validate and stage a declared derived JSON document with one canonical ref."""
        _canonical_relative(relative)
        if not _claimed(relative, self.op.finalize.claim_paths()):
            raise WriteScopeError(f"finalize write is outside the declared claims: {relative}")
        ref = stage_json_artifact(self.write_root, relative, document)
        self._files[relative] = document
        self._refs[relative] = ref
        self._staged_paths.add(relative)
        return ref

    def verify_staged_files(self) -> None:
        """Detect changes after capture, before returning a successful finalize outcome."""
        for path in self._staged_paths:
            try:
                data = read_workspace_file(self.write_root, path)
            except ArtifactReadError as error:
                raise OutputError(f"staged output changed during finalization: {path}: {error}") from error
            if hashlib.sha256(data).hexdigest() != self._refs[path].digest:
                raise OutputError(f"staged output changed during finalization: {path}")


Dependency = Callable[[PrepareContext, InputT], object]
Before = Callable[[PrepareContext, InputT], BaseModel]
After = Callable[[FinalizeContext, InputT, ResultT], OutputT | Mapping[str, Any]]
OnOutputError = Callable[[FinalizeContext, InputT, OutputError], OutputT]
Run = Callable[..., OutputT]
RequestBuild = Callable[..., AgentRunRequest]


def _sorted_paths(paths: tuple[str, ...], *, kind: str) -> tuple[str, ...]:
    for path in paths:
        _canonical_relative(path)
    if len(set(paths)) != len(paths):
        raise ValueError(f"{kind} must be unique")
    return tuple(sorted(paths))


def _write_name(name: str) -> str:
    if not name or name != name.strip() or "." in name or "/" in name:
        raise ValueError(f"write name must be a single path segment, got {name!r}")
    return name


def _entry_name(entry: WriteEntry) -> str | None:
    if isinstance(entry, Out):
        return entry.name
    if isinstance(entry, Dir):
        return entry.name
    return None


def _claim_path(entry: WriteEntry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, Out):
        return entry.path
    return entry.root


def _claim_paths(entries: tuple[WriteEntry, ...], *, kind: str) -> tuple[str, ...]:
    return _sorted_paths(tuple(_claim_path(entry) for entry in entries), kind=kind)


def _reject_duplicate_names(entries: tuple[WriteEntry, ...]) -> None:
    names = [name for entry in entries if (name := _entry_name(entry)) is not None]
    if len(names) != len(set(names)):
        raise ValueError("write names must be unique")


def _named_writes(entries: Iterable[WriteEntry]) -> tuple[NamedWrite, ...]:
    writes: list[NamedWrite] = []
    for entry in entries:
        name = _entry_name(entry)
        if name is None:
            continue
        writes.append(
            NamedWrite(
                name=name,
                root=_claim_path(entry),
                many=isinstance(entry, Dir),
                accumulate=isinstance(entry, Dir) and entry.accumulate,
            )
        )
    return tuple(writes)


@dataclass(frozen=True, slots=True)
class Out:
    """One named file. The name is handed off; the path is the write claim."""

    name: str
    path: str
    model: type[BaseModel] | None = None
    format: str = "bytes"
    context: Callable[[BaseModel], dict[str, object]] | None = None
    optional: bool = False
    input_source: Literal["project", "stage_first"] = "project"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _write_name(self.name))
        _canonical_relative(self.path)
        if self.format not in {"bytes", "json", "yaml"}:
            raise ValueError("file format must be bytes, json, or yaml")
        if self.model is not None and self.format == "bytes":
            raise ValueError("typed output requires json or yaml format")


@dataclass(frozen=True, slots=True)
class Dir:
    """A directory the Agent may write into; ``files`` names this run's exact files under it.

    Leave ``files`` empty when the exact files are only known once prepare can see the
    workspace. The prepare hook then calls ``PrepareContext.bind``. ``name`` hands the
    directory's committed files off. An unnamed directory is authorized and committed
    only.
    """

    root: str
    files: Callable[[Any], Iterable[str]] | None = None
    name: str | None = None
    model: type[BaseModel] | None = None
    format: str = "bytes"
    context: Callable[[BaseModel], dict[str, object]] | None = None
    accumulate: bool = False

    def __post_init__(self) -> None:
        _canonical_relative(self.root)
        if self.name is not None:
            object.__setattr__(self, "name", _write_name(self.name))
        if self.format not in {"bytes", "json", "yaml"}:
            raise ValueError("file format must be bytes, json, or yaml")
        if self.model is not None and self.format == "bytes":
            raise ValueError("typed output requires json or yaml format")

    def expand(self, paths: Iterable[str]) -> tuple[str, ...]:
        root = PurePosixPath(self.root)
        expanded = tuple(paths)
        for path in expanded:
            try:
                inside = root in _canonical_relative(path).parents
            except ValueError as error:
                raise InputError(str(error)) from error
            if not inside:
                raise InputError(f"{path} is outside the declared directory {self.root}")
        return expanded


WriteEntry = str | Out | Dir


def _declared_file_specs(
    entries: Iterable[WriteEntry], business: BaseModel
) -> Iterator[
    tuple[str, type[BaseModel] | None, str, Callable[[BaseModel], dict[str, object]] | None, bool, str]
]:
    for entry in entries:
        if isinstance(entry, str):
            yield entry, None, "bytes", None, False, "project"
        elif isinstance(entry, Out):
            yield entry.path, entry.model, entry.format, entry.context, entry.optional, entry.input_source
        else:
            if entry.files is None:
                raise InputError(f"{entry.root} has no files for this run")
            for path in entry.expand(entry.files(business)):
                yield path, entry.model, entry.format, entry.context, False, "project"


def _decoded_file(
    data: bytes,
    *,
    path: str,
    model: type[BaseModel] | None,
    format_name: str,
    context: Callable[[BaseModel], dict[str, object]] | None,
    business: BaseModel,
    error_type: type[InputError] | type[OutputError],
) -> object:
    if model is None:
        return data
    try:
        if format_name == "yaml":
            return model.model_validate(
                yaml.safe_load(data), context=context(business) if context is not None else None
            )
        return model.model_validate_json(data, context=context(business) if context is not None else None)
    except (yaml.YAMLError, UnicodeError, ValidationError, ValueError) as error:
        raise error_type(f"invalid declared file {path}: {error}") from error


def _capture_project_files(
    entries: Iterable[WriteEntry],
    root: Path,
    business: BaseModel,
    *,
    images: dict[str, bytes],
    refs: dict[str, ArtifactRef],
    stage_root: Path | None = None,
    authenticated_refs: Mapping[str, str] | None = None,
) -> dict[str, object]:
    files: dict[str, object] = {}
    for path, model, format_name, context, optional, source in _declared_file_specs(entries, business):
        selected_project = True
        try:
            if source == "stage_first" and stage_root is not None:
                try:
                    data = read_workspace_file(stage_root, path)
                    selected_project = False
                except ArtifactReadError as stage_error:
                    if stage_error.reason != "missing":
                        raise
                    data = read_workspace_file(root, path)
            else:
                data = read_workspace_file(root, path)
        except ArtifactReadError as error:
            if optional and error.reason == "missing":
                continue
            raise InputError(f"invalid declared input {path}: {error}") from error
        digest = hashlib.sha256(data).hexdigest()
        expected = (authenticated_refs or {}).get(path) if selected_project else None
        if expected is not None and digest != expected:
            raise InputError(f"declared input changed after authentication: {path}")
        files[path] = _decoded_file(
            data,
            path=path,
            model=model,
            format_name=format_name,
            context=context,
            business=business,
            error_type=InputError,
        )
        images[path] = data
        refs[path] = ArtifactRef(path=path, digest=digest)
    return files


def _authenticated_input_refs(dependencies: Iterable[object], business: BaseModel) -> dict[str, str]:
    """Bind a typed second read to the refs whose handles were eagerly opened."""
    refs: dict[str, str] = {}
    for dependency in dependencies:
        if not isinstance(dependency, ArtifactHandle):
            continue
        raw = (
            dependency.ref(business)
            if dependency.ref is not None
            else getattr(business, dependency.slot or "", None)
        )
        if raw is None:
            continue
        values = cast(Sequence[object], raw) if dependency.many else (raw,)
        for value in values:
            ref = coerce_artifact_ref(cast(Any, value))
            previous = refs.get(ref.path)
            if previous is not None and previous != ref.digest:
                raise InputError(f"conflicting authenticated input refs: {ref.path}")
            refs[ref.path] = ref.digest
    return refs


_ABSENT = object()


def _top_field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value[name] if name in value else _ABSENT
    return getattr(value, name, _ABSENT)


def _require_same(
    left: object,
    right: object,
    names: tuple[str, ...],
    *,
    error: type[Exception],
    label: str,
) -> None:
    """Compare named top-level fields. Nested paths are not read."""
    for name in names:
        actual = _top_field(left, name)
        expected = _top_field(right, name)
        if actual is _ABSENT or expected is _ABSENT or actual != expected:
            raise error(f"{label} {name} does not match")


@dataclass(frozen=True, slots=True)
class ArtifactHandle(Generic[DepT]):
    """A named write another op reads. The ledger key is ``{owner-namespace}.{name}``.

    ``same`` compares those top-level fields with the business input after the
    document loads. A mismatch is an input failure. Nested fields are not compared.
    """

    ledger_key: str
    slot: str | None = None
    many: bool = False
    model: type[BaseModel] | None = None
    loader: Callable[[Path, object], object] | None = None
    check: Callable[[object, BaseModel], None] | None = None
    read_error: Callable[[ArtifactReadError], str] | None = None
    ref: Callable[[BaseModel], object] | None = None
    optional: bool = False
    format: str = "bytes"
    context: Callable[[BaseModel], Mapping[str, object]] | None = None
    same: tuple[str, ...] = ()

    def load(self, root: Path, business: BaseModel) -> DepT:
        if self.slot is None and self.ref is None:
            raise InputError(f"{self.ledger_key} has no attempt-input slot")
        try:
            raw = self.ref(business) if self.ref is not None else getattr(business, self.slot or "", None)
        except (KeyError, ValueError) as error:
            raise InputError(f"{self.ledger_key} has no valid artifact ref: {error}") from error
        if raw is None:
            if self.optional:
                return cast(DepT, None)
            raise InputError(f"{self.ledger_key} is not on the attempt input")
        try:
            if self.many:
                if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                    raise InputError(f"{self.ledger_key} refs must be a list")
                value: object = tuple(self._open(root, item, business) for item in raw)
            else:
                value = self._open(root, raw, business)
        except ArtifactReadError as error:
            message = self.read_error(error) if self.read_error is not None else str(error)
            raise InputError(message) from error
        if self.same:
            documents = value if self.many else (value,)
            for document in cast(Sequence[object], documents):
                _require_same(document, business, self.same, error=InputError, label=self.ledger_key)
        if self.check is not None:
            self.check(value, business)
        return cast(DepT, value)

    def _open(self, root: Path, ref: object, business: BaseModel) -> object:
        if self.loader is not None:
            return self.loader(root, ref)
        if self.model is not None:
            return open_artifact(
                root,
                cast(Any, ref),
                model=self.model,
                loader=cast(Any, self.format),
                context=self.context(business) if self.context is not None else None,
            )
        return open_artifact(root, cast(Any, ref))


def _dependency_key(dependency: object) -> object:
    if isinstance(dependency, ArtifactHandle):
        return ("artifact", dependency.ledger_key)
    return dependency


def _stored_dep(deps: Mapping[object, object], op_name: str, dependency: object) -> Any:
    key = _dependency_key(dependency)
    if key not in deps:
        label = (
            dependency.ledger_key
            if isinstance(dependency, ArtifactHandle)
            else getattr(dependency, "__qualname__", dependency)
        )
        raise LookupError(f"{op_name} does not depend on {label}")
    return deps[key]


def _cached_dep(ctx: PrepareContext | FinalizeContext, dependency: object) -> Any:
    if isinstance(dependency, ArtifactHandle):
        declared = any(
            isinstance(item, ArtifactHandle) and item.ledger_key == dependency.ledger_key
            for item in ctx.op.prepare.depends
        )
        if not declared:
            raise LookupError(f"{ctx.op.name} does not depend on {dependency.ledger_key}")
        key = _dependency_key(dependency)
        if key not in ctx._deps:
            if ctx._business is None:
                raise LookupError(f"{ctx.op.name} has no attempt input for {dependency.ledger_key}")
            ctx._deps[key] = dependency.load(ctx.project_root, ctx._business)
        return ctx._deps[key]
    return _stored_dep(ctx._deps, ctx.op.name, dependency)


def _ledger_namespace(owner: str) -> str:
    return owner.rsplit(".", 1)[-1]


def _resolve_dependency(
    router: OpRouter,
    dependency: object,
    ctx: PrepareContext,
    business: BaseModel,
) -> object:
    if isinstance(dependency, ArtifactHandle):
        return dependency.load(ctx.project_root, business)
    if not callable(dependency):
        raise TypeError("depends entries must be artifact handles or callables")
    return router.resolve(dependency)(ctx, business)


@dataclass(frozen=True, slots=True, kw_only=True)
class Prepare(Generic[InputT]):
    """Kernel phase before the Agent: resolve dependencies, run the hook, write seed files."""

    hook: Before[InputT] | None = None
    depends: tuple[Dependency[InputT] | ArtifactHandle[Any], ...] = ()
    writes: tuple[WriteEntry, ...] = ()
    errors: tuple[type[Exception], ...] = ()
    request: RequestBuild | None = None
    eager_artifacts: bool = False
    reads: tuple[WriteEntry, ...] = ()

    def __post_init__(self) -> None:
        _claim_paths(self.writes, kind="prepare writes")
        _claim_paths(self.reads, kind="prepare reads")
        _reject_duplicate_names(self.writes)

    def claim_paths(self) -> tuple[str, ...]:
        return _claim_paths(self.writes, kind="prepare writes")


@dataclass(frozen=True, slots=True, kw_only=True)
class Agent(Generic[ResultT]):
    """The Agent run: persona, skill, typed result, and the workspace paths it may write.

    ``result`` defaults to ``ArtifactListResultV1``, the receipt of the files the run wrote.
    """

    profile: str
    skill: str
    result: type[ResultT] = cast(Any, ArtifactListResultV1)
    writes: tuple[WriteEntry, ...]
    strict_files: bool = False
    baseline_digests: Callable[[BaseModel], Mapping[str, str]] | None = None

    def __post_init__(self) -> None:
        self.claims()
        _reject_duplicate_names(self.writes)

    def files(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                entry if isinstance(entry, str) else entry.path
                for entry in self.writes
                if not isinstance(entry, Dir)
            )
        )

    def routes(self) -> tuple[str, ...]:
        """Exact files when the Agent declares any; otherwise the directories it edits."""
        return self.files() or tuple(sorted(entry.root for entry in self.writes if isinstance(entry, Dir)))

    def claims(self) -> tuple[str, ...]:
        return _claim_paths(self.writes, kind="agent writes")

    def allowed_outputs(
        self, business: BaseModel, bound: Mapping[str, tuple[str, ...]] | None = None
    ) -> tuple[str, ...]:
        bound = {} if bound is None else bound
        expanded: list[str] = []
        for entry in self.writes:
            if isinstance(entry, Dir):
                expanded.extend(self._expand_dir(entry, business, bound))
        return (*self.files(), *expanded)

    def named_outputs(
        self, business: BaseModel, bound: Mapping[str, tuple[str, ...]] | None = None
    ) -> dict[str, str | list[str]]:
        """Name to path for one file, or name to paths for a directory, for this run."""
        bound = {} if bound is None else bound
        named: dict[str, str | list[str]] = {}
        for entry in self.writes:
            if isinstance(entry, Out):
                named[entry.name] = entry.path
            elif isinstance(entry, Dir) and entry.name is not None:
                named[entry.name] = list(self._expand_dir(entry, business, bound))
        return named

    def _expand_dir(
        self, entry: Dir, business: BaseModel, bound: Mapping[str, tuple[str, ...]]
    ) -> tuple[str, ...]:
        if entry.root in bound:
            paths: Iterable[str] = bound[entry.root]
        elif entry.files is not None:
            paths = entry.files(business)
        else:
            raise InputError(f"{entry.root} has no files for this run")
        return entry.expand(paths)

    def capture(self, ctx: FinalizeContext, business: BaseModel, result: BaseModel) -> None:
        """Enforce the declared receipt and capture typed bytes before business acceptance."""
        if not self.strict_files:
            return
        expected = set(self.allowed_outputs(business))
        locked = getattr(business, "artifact_paths", None)
        if locked is not None and not locked:
            raise InputError("artifact_paths must lock the expected output files")
        receipt = getattr(result, "output_files", None)
        if receipt is not None and set(receipt) != expected:
            raise OutputError(
                f"agent receipt does not match declared outputs: "
                f"missing={sorted(expected - set(receipt))}, extra={sorted(set(receipt) - expected)}"
            )
        if locked is not None:
            unlocked = sorted(path for path in expected if not _claimed(path, locked))
            if unlocked:
                raise InputError(f"declared outputs are outside artifact_paths: {unlocked}")
        baseline = self.baseline_digests(business) if self.baseline_digests is not None else {}
        for entry in (*self.writes, *ctx.op.prepare.writes):
            if entry in ctx.op.prepare.writes and isinstance(entry, str):
                continue
            if isinstance(entry, str):
                specs = ((entry, None, "bytes", None),)
            elif isinstance(entry, Out):
                specs = ((entry.path, entry.model, entry.format, entry.context),)
            else:
                specs = tuple(
                    (path, entry.model, entry.format, entry.context)
                    for path in self._expand_dir(entry, business, {})
                )
            for path, model, format_name, context in specs:
                staged = True
                try:
                    data = read_workspace_file(ctx.write_root, path)
                except ArtifactReadError as error:
                    if error.reason != "missing" or path not in baseline:
                        if error.reason == "missing":
                            raise OutputError(f"declared output file is missing: {path}") from error
                        raise OutputError(f"invalid declared output {path}: {error}") from error
                    try:
                        data = open_artifact(
                            ctx.project_root,
                            ArtifactRef(path=path, digest=baseline[path]),
                        )
                    except ArtifactReadError as baseline_error:
                        raise OutputError(
                            f"invalid baseline output {path}: {baseline_error}"
                        ) from baseline_error
                    staged = False
                value = _decoded_file(
                    data,
                    path=path,
                    model=model,
                    format_name=format_name,
                    context=context,
                    business=business,
                    error_type=OutputError,
                )
                ctx._files[path] = value
                ctx._images[path] = data
                ctx._refs[path] = ArtifactRef(path=path, digest=hashlib.sha256(data).hexdigest())
                if staged:
                    ctx._staged_paths.add(path)


@dataclass(frozen=True, slots=True, kw_only=True)
class Finalize(Generic[InputT, ResultT, OutputT]):
    """Kernel phase after the Agent: check the result, run the hook, write sealed files.

    ``same`` compares top-level fields of the agent result and the business input.
    A mismatch is an output failure. ``errors`` maps other exceptions to an input
    or output failure. ``artifacts="auto"`` replaces ``artifacts`` with this phase's
    refs, sorted and unique by path. Nested fields are not compared.
    """

    hook: After[InputT, ResultT, OutputT] | None = None
    on_output_error: OnOutputError[InputT, OutputT] | None = None
    writes: tuple[WriteEntry, ...] = ()
    same: tuple[str, ...] = ()
    errors: tuple[type[Exception], ...] = ()
    error_failure: Literal["input", "output"] = "input"
    artifacts: Literal["auto"] | None = None

    def __post_init__(self) -> None:
        _claim_paths(self.writes, kind="finalize writes")
        _reject_duplicate_names(self.writes)
        if self.error_failure not in {"input", "output"}:
            raise ValueError("finalize error_failure must be input or output")
        if self.artifacts not in {None, "auto"}:
            raise ValueError("finalize artifacts must be auto or omitted")

    def claim_paths(self) -> tuple[str, ...]:
        return _claim_paths(self.writes, kind="finalize writes")


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentOp(Generic[InputT, ResultT, OutputT]):
    """One Agent operation: prepare builds the run request, finalize seals the result."""

    router: OpRouter
    name: str
    input: type[InputT]
    prepare: Prepare[InputT]
    agent: Agent[ResultT]
    finalize: Finalize[InputT, ResultT, OutputT]
    output: type[OutputT]
    retry: AttemptRetryPolicy | None = None
    transport_business: bool = False
    validators: tuple[str, ...] = ()

    @property
    def directory(self) -> str:
        return self.name.replace("-", "_").replace(".", "_")

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

    @property
    def input_model(self) -> type[InputT]:
        return self.input

    @property
    def output_model(self) -> type[OutputT]:
        return self.output

    def ledger_namespace(self) -> str:
        return _ledger_namespace(self.router.owner)

    def ledger_writes(self) -> tuple[NamedWrite, ...]:
        return _named_writes((*self.prepare.writes, *self.agent.writes, *self.finalize.writes))

    def input_bindings(self) -> tuple[InputBinding, ...]:
        return tuple(
            InputBinding(ledger_key=item.ledger_key, field=item.slot, many=item.many)
            for item in self.prepare.depends
            if isinstance(item, ArtifactHandle) and item.slot is not None
        )

    def artifact(
        self,
        name: str,
        *,
        model: type[BaseModel] | None = None,
        loader: Callable[[Path, object], object] | None = None,
        check: Callable[[object, BaseModel], None] | None = None,
        same: tuple[str, ...] = (),
        slot: str | None = None,
        many: bool | None = None,
        read_error: Callable[[ArtifactReadError], str] | None = None,
    ) -> ArtifactHandle[Any]:
        """Handle for one named write. ``output`` is already the result model."""
        spec = next((item for item in self.ledger_writes() if item.name == name), None)
        if spec is None:
            raise ValueError(f"{self.name} does not name a write {name!r}")
        return ArtifactHandle(
            ledger_key=f"{self.ledger_namespace()}.{name}",
            slot=slot,
            many=spec.many if many is None else many,
            model=model,
            loader=loader,
            check=check,
            same=same,
            read_error=read_error,
        )

    def contract(self) -> AgentExecutionContract[InputT, ResultT, OutputT]:
        prepare = self.prepare.claim_paths()
        finalize = self.finalize.claim_paths()
        claims = self.agent.claims()
        # Prepare may seed a directory the Agent then edits, so a shared path stays
        # in both phases. Finalize-owned paths stay out of the runtime claim.
        runtime = tuple(path for path in claims if path not in finalize)
        return AgentExecutionContract(
            contract_id=self.contract_id,
            owner_id=self.router.owner,
            prepare_handler_id=self.prepare_handler_id,
            finalize_handler_id=self.finalize_handler_id,
            skill_id=self.agent.skill,
            agent_profile=self.agent.profile,
            input_model=self.input,
            agent_result_model=self.agent.result,
            output_model=self.output,
            resources=ResourceClaims(
                reads=self.router.reads,
                writes=tuple(sorted({*claims, *prepare, *finalize})),
            ),
            retry=self.router.agent_retry if self.retry is None else self.retry,
            timeout=self.router.timeout,
            validators=self.validators,
            phase_write_claims=AgentPhaseWriteClaims(prepare=prepare, runtime=runtime, finalize=finalize),
        )

    def resource_files(self) -> dict[str, str]:
        owner = self.router.owner
        skill = self.agent.skill
        base = f"ops/{self.directory}"
        manifest = {f"{owner}.skill.{skill}.v1": f"{base}/{_SKILL_FILE}"}
        for filename in self.router.list_files(base):
            if filename == _SKILL_FILE or not filename.endswith(".md"):
                continue
            if filename.endswith(_SKILL_SUFFIX):
                raise ValueError(
                    f"{base}/{filename}: an op has one SKILL.md; declare another skill as its own op"
                )
            manifest[f"{owner}.skill.{skill}.{filename.removesuffix('.md')}.v1"] = f"{base}/{filename}"
        return manifest

    def result_contract(self) -> ResultContract:
        schema = cast(JSONValue, result_schema_from_model(self.agent.result))
        return result_contract_from(self.result_schema_id, schema)

    def handle_prepare(self, request: OpRequest, task: TaskContext) -> TaskOutcome:
        try:
            business = validate_model(self.input, _plain_json(request.input))
            binding = validate_binding(request.binding_data)
            ctx = PrepareContext(self, task)
            ctx._business = business
            for dependency in self.prepare.depends:
                if isinstance(dependency, ArtifactHandle):
                    if self.prepare.eager_artifacts:
                        ctx._deps[_dependency_key(dependency)] = _resolve_dependency(
                            self.router, dependency, ctx, business
                        )
                    continue
                ctx._deps[_dependency_key(dependency)] = _resolve_dependency(
                    self.router, dependency, ctx, business
                )
            ctx._files = _capture_project_files(
                self.prepare.reads,
                ctx.project_root,
                business,
                images=ctx._project_images,
                refs=ctx._project_refs,
                stage_root=ctx.write_root,
                authenticated_refs=(
                    _authenticated_input_refs(self.prepare.depends, business)
                    if self.prepare.eager_artifacts
                    else None
                ),
            )
            if self.prepare.hook is not None:
                business = self.prepare.hook(ctx, business)
                ctx._business = business
            named = self.agent.named_outputs(business, ctx._bound)
            if named:
                ctx._extra["outputs"] = cast(JSONValue, named)
            allowed = self.agent.allowed_outputs(business, ctx._bound)
            skill_text = self.router.resource_text(f"ops/{self.directory}/{_SKILL_FILE}")
            result = self.result_contract()
            if self.prepare.request is None:
                run = skill_request(
                    skill_text=skill_text,
                    business=cast(FrozenModel, business),
                    business_extra=ctx._extra or None,
                    binding=binding,
                    result=result,
                    roots=task,
                    allowed_outputs=allowed,
                    scope_id=self.router.scope(business),
                )
            else:
                run = self.prepare.request(ctx, business, binding, allowed, result, skill_text)
            if self.transport_business:
                return TaskOutcome.succeeded(
                    PreparedAgentRun(
                        run_request=run,
                        prepared_business=business.model_dump(mode="json"),
                    ).model_dump(mode="json")
                )
            return TaskOutcome.succeeded(run.model_dump(mode="json"))
        except (InputError, ValidationError, *self.prepare.errors) as error:
            return failed_input(error)

    def _finalize_parts(self, raw: object) -> tuple[dict[str, Any], AgentRunResult]:
        if not isinstance(raw, Mapping):
            raise InputError("finalize input must be an object")
        payload = dict(raw)
        if "prepare" in payload and set(payload) <= {"prepare", "agent_result"}:
            envelope = validate_model(AgentOpFinalizeInputV1, payload)
            prepared = thaw_json(envelope.prepare)
            if not isinstance(prepared, dict):
                raise InputError("prepared business input must be an object")
            return dict(prepared), envelope.agent_result
        if "agent_result" not in payload:
            raise InputError("finalize input requires agent_result")
        result = validate_model(AgentRunResult, payload.pop("agent_result"))
        return payload, result

    def handle_finalize(self, request: OpRequest, task: TaskContext) -> TaskOutcome:
        try:
            prepared, agent_result = self._finalize_parts(_plain_json(request.input))
            fields = self.input.model_fields
            business = validate_model(
                self.input, {key: value for key, value in prepared.items() if key in fields}
            )
        except InputError as error:
            return failed_input(error)
        ctx = FinalizeContext(self, task, agent_result, prepared)
        ctx._business = business
        hook = self.finalize.hook
        try:
            if self.prepare.eager_artifacts:
                for dependency in self.prepare.depends:
                    if isinstance(dependency, ArtifactHandle):
                        ctx._deps[_dependency_key(dependency)] = dependency.load(ctx.project_root, business)
            ctx._project_files = _capture_project_files(
                self.prepare.reads,
                ctx.project_root,
                business,
                images=ctx._project_images,
                refs=ctx._project_refs,
                stage_root=ctx.write_root,
                authenticated_refs=(
                    _authenticated_input_refs(self.prepare.depends, business)
                    if self.prepare.eager_artifacts
                    else None
                ),
            )
            try:
                result = validate_output(self.agent.result, thaw_json(agent_result.result_payload))
                self.agent.capture(ctx, business, result)
                _require_same(result, business, self.finalize.same, error=OutputError, label="agent result")
                produced: BaseModel | Mapping[str, Any] = (
                    result if hook is None else hook(ctx, business, result)
                )
            except OutputError as error:
                if self.finalize.on_output_error is None:
                    raise
                produced = self.finalize.on_output_error(ctx, business, error)
            data = produced.model_dump(mode="json") if isinstance(produced, BaseModel) else dict(produced)
            if self.finalize.artifacts == "auto":
                data = {
                    **data,
                    "artifacts": [ref.model_dump(mode="json") for ref in ctx.refs()],
                }
            output = validate_output(self.output, data)
            ctx.verify_staged_files()
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
        except Exception as error:
            if self.finalize.errors and isinstance(error, self.finalize.errors):
                if self.finalize.error_failure == "output":
                    return failed_output(str(error))
                return failed_input(error)
            raise
        return TaskOutcome.succeeded(cast(JSONValue, output.model_dump(mode="json")))


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskOp(Generic[InputT, OutputT]):
    """One deterministic operation: validated input in, validated output out."""

    router: OpRouter
    name: str
    input: type[InputT]
    output: type[OutputT]
    run: Run[OutputT]
    reads: tuple[str, ...]
    writes: tuple[WriteEntry, ...]
    depends: tuple[ArtifactHandle[Any], ...] = ()
    errors: tuple[type[Exception], ...] = (InputError,)
    validators: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reads", _sorted_paths(self.reads, kind="reads"))
        _claim_paths(self.writes, kind="writes")
        _reject_duplicate_names(self.writes)

    def claim_paths(self) -> tuple[str, ...]:
        return _claim_paths(self.writes, kind="writes")

    def ledger_namespace(self) -> str:
        return _ledger_namespace(self.router.owner)

    def ledger_writes(self) -> tuple[NamedWrite, ...]:
        return _named_writes(self.writes)

    def input_bindings(self) -> tuple[InputBinding, ...]:
        return tuple(
            InputBinding(ledger_key=item.ledger_key, field=item.slot, many=item.many)
            for item in self.depends
            if item.slot is not None
        )

    @property
    def input_model(self) -> type[InputT]:
        return self.input

    @property
    def output_model(self) -> type[OutputT]:
        return self.output

    def artifact(
        self,
        name: str,
        *,
        model: type[BaseModel] | None = None,
        loader: Callable[[Path, object], object] | None = None,
        check: Callable[[object, BaseModel], None] | None = None,
        same: tuple[str, ...] = (),
        slot: str | None = None,
        many: bool | None = None,
        read_error: Callable[[ArtifactReadError], str] | None = None,
    ) -> ArtifactHandle[Any]:
        spec = next((item for item in self.ledger_writes() if item.name == name), None)
        if spec is None:
            raise ValueError(f"{self.name} does not name a write {name!r}")
        return ArtifactHandle(
            ledger_key=f"{self.ledger_namespace()}.{name}",
            slot=slot,
            many=spec.many if many is None else many,
            model=model,
            loader=loader,
            check=check,
            same=same,
            read_error=read_error,
        )

    @property
    def directory(self) -> str:
        return self.name.replace("-", "_").replace(".", "_")

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
            resources=ResourceClaims(reads=self.reads, writes=self.claim_paths()),
            retry=self.router.task_retry,
            timeout=self.router.timeout,
            validators=self.validators,
        )

    def execute(self, request: OpRequest, task: TaskContext) -> TaskOutcome:
        try:
            business = validate_model(self.input, request.input)
            if self.depends:
                deps = {item.ledger_key: item.load(task.project_root, business) for item in self.depends}
                output = self.run(task, business, MappingProxyType(deps))
            else:
                output = self.run(task, business)
        except OutputError as error:
            return failed_output(str(error))
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
        input: type[InputT],
        prepare: Prepare[InputT] | None = None,
        agent: Agent[ResultT],
        finalize: Finalize[InputT, ResultT, OutputT] | None = None,
        output: type[OutputT],
        retry: AttemptRetryPolicy | None = None,
        transport_business: bool = False,
        validators: tuple[str, ...] = (),
    ) -> AgentOp[InputT, ResultT, OutputT]:
        """Declare one Agent op; ``retry`` overrides the router's ``agent_retry`` for it."""
        op = AgentOp(
            router=self,
            name=name,
            input=input,
            prepare=Prepare() if prepare is None else prepare,
            agent=agent,
            finalize=Finalize() if finalize is None else finalize,
            output=output,
            retry=retry,
            transport_business=transport_business,
            validators=validators,
        )
        self._register(op)
        return op

    def task(
        self,
        name: str,
        *,
        input: type[InputT],
        output: type[OutputT],
        run: Run[OutputT],
        reads: tuple[str, ...],
        writes: tuple[WriteEntry, ...],
        depends: tuple[ArtifactHandle[Any], ...] = (),
        errors: tuple[type[Exception], ...] = (InputError,),
        validators: tuple[str, ...] = (),
    ) -> TaskOp[InputT, OutputT]:
        op = TaskOp(
            router=self,
            name=name,
            input=input,
            output=output,
            run=run,
            reads=reads,
            writes=writes,
            depends=depends,
            errors=errors,
            validators=validators,
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
        return MappingProxyType({name: op.agent.routes() for name, op in self.agent_ops().items()})

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
                table[op.prepare_handler_id] = op.handle_prepare
                table[op.finalize_handler_id] = op.handle_finalize
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
    "ArtifactHandle",
    "FinalizeContext",
    "OpRequest",
    "OpRouter",
    "Out",
    "PrepareContext",
    "TaskOp",
    "WriteScopeError",
]
