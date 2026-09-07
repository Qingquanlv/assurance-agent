from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import stat
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.evidence.events import TaskActivityTerminalObserved
from graph_engine.frozen_json import FrozenJSONValue, thaw_json
from graph_engine.plugin_api import FrozenModel
from graph_engine.attempts.host_authority import HostAuthorityError, HostSealingAuthority
from graph_engine.attempts.host_protocol import (
    TASK_HOST_WIRE_SCHEMA_VERSION,
    TaskHostCallIdentity,
    TaskHostTerminalReceipt,
)


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


class TerminalReceiptError(GraphEngineError):
    """Raised when a host terminal receipt cannot be authenticated or installed."""


class HostSelectionReceipt(FrozenModel):
    """Immutable host-owned selection for one canonical scope."""

    schema_version: Literal["1"] = "1"
    authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_version: Literal["1"] = "1"
    namespace: str = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    scope: FrozenJSONValue
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection: FrozenJSONValue
    selection_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _digests_match(self) -> Self:
        scope = thaw_json(self.scope)
        selection = thaw_json(self.selection)
        if not isinstance(scope, Mapping) or not isinstance(selection, Mapping):
            raise ValueError("host selection scope and payload must be JSON objects")
        if canonical_digest(cast(JSONValue, scope)) != self.scope_digest:
            raise ValueError("host selection scope digest mismatch")
        if canonical_digest(cast(JSONValue, selection)) != self.selection_digest:
            raise ValueError("host selection payload digest mismatch")
        return self


def _receipt_cut(name: str) -> None:
    del name


def _selection_publication_cut(name: str) -> None:
    del name


def _selection_signature_payload(receipt: HostSelectionReceipt) -> bytes:
    payload = receipt.model_dump(mode="json", exclude={"signature"})
    return canonical_json_bytes(cast(JSONValue, payload))


def _quiescence_cut(name: str) -> None:
    del name


def prove_call_quiescent(
    *,
    writer_identities: tuple[str, ...] = (),
    descendant_identities: tuple[str, ...] = (),
) -> str:
    """Return a quiescence digest only after every writer and descendant has stopped."""

    if writer_identities or descendant_identities:
        raise TerminalReceiptError("call is not quiescent")
    payload: JSONValue = {
        "descendants": list(descendant_identities),
        "writers": list(writer_identities),
    }
    return canonical_digest(payload)


def _identity_filename(identity: TaskHostCallIdentity) -> str:
    if identity.activity_id is None:
        raise TerminalReceiptError("terminal receipt requires an activity id")
    payload: JSONValue = {
        "activation_id": identity.activation_id,
        "activity_id": identity.activity_id,
        "attempt": identity.attempt,
        "invocation_id": identity.invocation_id,
        "operation": identity.operation,
        "task_id": identity.task_id,
    }
    return f"{canonical_digest(payload)}.json"


def _receipt_matches_identity(receipt: TaskHostTerminalReceipt, identity: TaskHostCallIdentity) -> bool:
    if identity.wire_schema_version != receipt.wire_schema_version:
        return False
    if receipt.schema_version != "3":
        return False
    return (
        receipt.invocation_id == identity.invocation_id
        and receipt.task_id == identity.task_id
        and receipt.activation_id == identity.activation_id
        and receipt.attempt == identity.attempt
        and receipt.activity_id == identity.activity_id
        and receipt.operation == identity.operation
        and receipt.attempt_key_digest == identity.attempt_key_digest
        and receipt.authorization_id == identity.authorization_id
        and receipt.fencing_token == identity.fencing_token
        and receipt.phase == identity.phase
        and receipt.workspace_identity_digest == identity.workspace_identity_digest
        and receipt.request_digest == identity.request_digest
        and receipt.graph_revision == identity.graph_revision
        and receipt.product_lock_digest == identity.product_lock_digest
        and receipt.handler_id == identity.handler_id
        and receipt.host_implementation_digest == identity.host_implementation_digest
        and receipt.wire_schema_version == identity.wire_schema_version
    )


class TerminalReceiptSink:
    """Write-only installer bound to one host-call identity."""

    __slots__ = ("_dir_fd", "_filename", "_identity", "_host_call_id")

    def __init__(
        self,
        *,
        dir_fd: int,
        filename: str,
        identity: TaskHostCallIdentity,
        host_call_id: int,
    ) -> None:
        self._dir_fd = dir_fd
        self._filename = filename
        self._identity = identity
        self._host_call_id = host_call_id

    @property
    def host_call_id(self) -> int:
        return self._host_call_id

    def install(self, receipt: TaskHostTerminalReceipt) -> None:
        if not _receipt_matches_identity(receipt, self._identity):
            raise TerminalReceiptError("foreign terminal receipt")
        if receipt.host_call_id != self._host_call_id:
            raise TerminalReceiptError("non-monotonic host-call id")
        content = canonical_json_bytes(receipt.model_dump(mode="json"))
        temporary = f".tmp-{uuid.uuid4().hex}"
        descriptor = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=self._dir_fd)
        installed = False
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
            _receipt_cut("after_receipt_fsync")
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise TerminalReceiptError("terminal receipt is not a stable regular file")
        finally:
            os.close(descriptor)
        try:
            _rename_no_replace_at(self._dir_fd, temporary, self._filename)
            installed = True
            _receipt_cut("after_receipt_rename")
            os.fsync(self._dir_fd)
        except FileExistsError as error:
            raise TerminalReceiptError("multiple terminal receipts") from error
        finally:
            if not installed:
                try:
                    os.unlink(temporary, dir_fd=self._dir_fd)
                except FileNotFoundError:
                    pass


class TerminalReceiptStore:
    """Engine-owned immutable store for host-call terminal receipts."""

    def __init__(
        self,
        root: Path,
        *,
        selection_authority: HostSealingAuthority | None = None,
        _parent_fd: int | None = None,
    ) -> None:
        self.root = Path(root).absolute()
        self._selection_authority = selection_authority
        self._parent_fd = _parent_fd

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        selection_authority: HostSealingAuthority | None = None,
    ) -> TerminalReceiptStore:
        store = cls(root, selection_authority=selection_authority)
        parent_fd = _open_absolute_directory(store.root.parent)
        try:
            return store._create_at(parent_fd, store.root.name)
        finally:
            os.close(parent_fd)

    @classmethod
    def create_at(
        cls,
        parent_fd: int,
        name: str,
        *,
        display_root: Path,
        selection_authority: HostSealingAuthority | None = None,
    ) -> TerminalReceiptStore:
        store = cls.at(
            parent_fd,
            name,
            display_root=display_root,
            selection_authority=selection_authority,
        )
        store._create_at(parent_fd, name)
        return store

    @classmethod
    def at(
        cls,
        parent_fd: int,
        name: str,
        *,
        display_root: Path,
        selection_authority: HostSealingAuthority | None = None,
    ) -> TerminalReceiptStore:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("receipt store name must be one path component")
        root = Path(display_root)
        if root.name != name:
            raise ValueError("display root must end with the receipt store name")
        return cls(root, selection_authority=selection_authority, _parent_fd=parent_fd)

    @classmethod
    def open_or_create(
        cls,
        root: Path,
        *,
        selection_authority: HostSealingAuthority | None = None,
    ) -> TerminalReceiptStore:
        store = cls(root, selection_authority=selection_authority)
        parent_fd = _open_absolute_directory(store.root.parent)
        try:
            try:
                os.mkdir(store.root.name, mode=0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
            return store
        finally:
            os.close(parent_fd)

    @classmethod
    def open_or_create_at(
        cls,
        parent_fd: int,
        name: str,
        *,
        display_root: Path,
        selection_authority: HostSealingAuthority | None = None,
    ) -> TerminalReceiptStore:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        return cls.at(
            parent_fd,
            name,
            display_root=display_root,
            selection_authority=selection_authority,
        )

    def _create_at(self, parent_fd: int, name: str) -> TerminalReceiptStore:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError as error:
            raise TerminalReceiptError(f"receipt store already exists: {self.root}") from error
        return self

    def sink_for(self, identity: TaskHostCallIdentity) -> TerminalReceiptSink:
        if identity.wire_schema_version != TASK_HOST_WIRE_SCHEMA_VERSION:
            raise TerminalReceiptError("host call identity version is not current")
        if identity.activity_id is None:
            raise TerminalReceiptError("terminal receipt requires an activity id")
        directory_fd = self._open_root()
        try:
            host_call_id = self._next_host_call_id(directory_fd)
            return TerminalReceiptSink(
                dir_fd=os.dup(directory_fd),
                filename=_identity_filename(identity),
                identity=identity,
                host_call_id=host_call_id,
            )
        finally:
            os.close(directory_fd)

    def authenticate(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        if identity.wire_schema_version != TASK_HOST_WIRE_SCHEMA_VERSION:
            raise TerminalReceiptError("host call identity version is not current")
        if identity.activity_id is None:
            raise TerminalReceiptError("terminal receipt requires an activity id")
        directory_fd = self._open_root()
        try:
            names = {name for name in _validated_names(directory_fd) if not name.startswith(".")}
            expected = _identity_filename(identity)
            if expected not in names:
                return ()
            receipt = self._read_final(directory_fd, expected)
            if receipt.fencing_token > identity.fencing_token:
                raise TerminalReceiptError("fencing token is stale")
            if not _receipt_matches_identity(receipt, identity):
                raise TerminalReceiptError("foreign terminal receipt")
            return (receipt,)
        finally:
            os.close(directory_fd)

    def publish_selection(
        self,
        *,
        namespace: str,
        scope: dict[str, JSONValue],
        selection: dict[str, JSONValue],
    ) -> HostSelectionReceipt:
        """Create one immutable sealed selection, or replay it exactly."""

        authority = self._require_selection_authority()
        unsigned: dict[str, JSONValue] = {
            "schema_version": "1",
            "authority_id": authority.authority_id,
            "authority_version": authority.authority_version,
            "namespace": namespace,
            "scope": scope,
            "scope_digest": canonical_digest(scope),
            "selection": selection,
            "selection_digest": canonical_digest(selection),
        }
        receipt = HostSelectionReceipt(
            schema_version="1",
            authority_id=authority.authority_id,
            authority_version="1",
            namespace=namespace,
            scope=scope,
            scope_digest=canonical_digest(scope),
            selection=selection,
            selection_digest=canonical_digest(selection),
            signature=authority.sign(canonical_json_bytes(unsigned)),
        )
        root_fd = self._open_root()
        selections_fd: int | None = None
        namespace_fd: int | None = None
        lock_fd: int | None = None
        try:
            selections_fd = _open_or_create_directory_at(root_fd, ".selections", "selection store")
            namespace_fd = _open_or_create_directory_at(
                selections_fd,
                canonical_digest(namespace),
                "selection namespace",
            )
            lock_fd = _lock_selection_namespace(namespace_fd)
            filename = f"{receipt.scope_digest}.json"
            pending = f".{receipt.scope_digest}.pending"
            self._recover_selection_pending(namespace_fd, pending, filename)
            try:
                existing = self._read_selection(namespace_fd, filename)
            except FileNotFoundError:
                pass
            else:
                if existing != receipt:
                    raise TerminalReceiptError("host selection compare-and-swap conflict")
                return existing

            content = canonical_json_bytes(receipt.model_dump(mode="json"))
            descriptor = os.open(pending, _WRITE_FLAGS, 0o600, dir_fd=namespace_fd)
            try:
                _write_all(descriptor, content)
                os.fchmod(descriptor, 0o400)
                _selection_publication_cut("before-selection-file-fsync")
                os.fsync(descriptor)
                _selection_publication_cut("after-selection-file-fsync")
            finally:
                os.close(descriptor)
            _selection_publication_cut("before-selection-install")
            _rename_no_replace_at(namespace_fd, pending, filename)
            _selection_publication_cut("after-selection-install")
            _selection_publication_cut("before-selection-directory-fsync")
            os.fsync(namespace_fd)
            _selection_publication_cut("after-selection-directory-fsync")
            return receipt
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            if namespace_fd is not None:
                os.close(namespace_fd)
            if selections_fd is not None:
                os.close(selections_fd)
            os.close(root_fd)

    def authenticate_selection(
        self,
        *,
        namespace: str,
        scope: dict[str, JSONValue],
    ) -> HostSelectionReceipt | None:
        """Load the sealed selection for an independently supplied scope."""

        expected_scope_digest = canonical_digest(scope)
        root_fd = self._open_root()
        selections_fd: int | None = None
        namespace_fd: int | None = None
        lock_fd: int | None = None
        try:
            try:
                selections_fd = _open_directory_at(root_fd, ".selections", "selection store")
                namespace_fd = _open_directory_at(
                    selections_fd,
                    canonical_digest(namespace),
                    "selection namespace",
                )
            except FileNotFoundError:
                return None
            lock_fd = _lock_selection_namespace(namespace_fd)
            filename = f"{expected_scope_digest}.json"
            self._recover_selection_pending(
                namespace_fd,
                f".{expected_scope_digest}.pending",
                filename,
            )
            try:
                receipt = self._read_selection(namespace_fd, filename)
            except FileNotFoundError:
                return None
            if receipt.namespace != namespace or thaw_json(receipt.scope) != scope:
                raise TerminalReceiptError("foreign host selection receipt")
            return receipt
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            if namespace_fd is not None:
                os.close(namespace_fd)
            if selections_fd is not None:
                os.close(selections_fd)
            os.close(root_fd)

    def authenticate_selections(self, *, namespace: str) -> tuple[HostSelectionReceipt, ...]:
        """Enumerate sealed selections in one host-owned namespace."""

        root_fd = self._open_root()
        selections_fd: int | None = None
        namespace_fd: int | None = None
        lock_fd: int | None = None
        try:
            try:
                selections_fd = _open_directory_at(root_fd, ".selections", "selection store")
                namespace_fd = _open_directory_at(
                    selections_fd,
                    canonical_digest(namespace),
                    "selection namespace",
                )
            except FileNotFoundError:
                return ()
            lock_fd = _lock_selection_namespace(namespace_fd)
            for name in _validated_names(namespace_fd):
                if name == ".publish.lock" or not name.startswith("."):
                    continue
                scope_digest = _pending_selection_scope(name)
                self._recover_selection_pending(
                    namespace_fd,
                    name,
                    f"{scope_digest}.json",
                )
            receipts: list[HostSelectionReceipt] = []
            for name in _validated_names(namespace_fd):
                if name == ".publish.lock":
                    continue
                if name.startswith("."):
                    raise TerminalReceiptError("unknown host selection transaction marker")
                receipt = self._read_selection(namespace_fd, name)
                if receipt.namespace != namespace or name != f"{receipt.scope_digest}.json":
                    raise TerminalReceiptError("foreign host selection receipt")
                receipts.append(receipt)
            return tuple(receipts)
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            if namespace_fd is not None:
                os.close(namespace_fd)
            if selections_fd is not None:
                os.close(selections_fd)
            os.close(root_fd)

    def _recover_selection_pending(
        self,
        namespace_fd: int,
        pending: str,
        filename: str,
    ) -> None:
        try:
            candidate = self._read_selection(namespace_fd, pending)
        except FileNotFoundError:
            return
        if pending != f".{candidate.scope_digest}.pending" or filename != (f"{candidate.scope_digest}.json"):
            raise TerminalReceiptError("foreign host selection transaction marker")
        try:
            installed = self._read_selection(namespace_fd, filename)
        except FileNotFoundError:
            _rename_no_replace_at(namespace_fd, pending, filename)
            os.fsync(namespace_fd)
            return
        if installed != candidate:
            raise TerminalReceiptError("host selection compare-and-swap conflict")
        os.unlink(pending, dir_fd=namespace_fd)
        os.fsync(namespace_fd)

    def _require_selection_authority(self) -> HostSealingAuthority:
        if self._selection_authority is None:
            raise TerminalReceiptError("host selection sealing authority is unavailable")
        return self._selection_authority

    def delete_authenticated(
        self,
        identity: TaskHostCallIdentity,
        expected: TaskHostTerminalReceipt,
        terminal: TaskActivityTerminalObserved,
    ) -> None:
        if terminal.activity_id != expected.activity_id:
            raise TerminalReceiptError("receipt cleanup requires the exact terminal event")
        if terminal.outcome_digest != expected.outcome_digest:
            raise TerminalReceiptError("receipt cleanup requires the exact terminal event")
        found = self.authenticate(identity)
        if found == ():
            return
        if found != (expected,):
            raise TerminalReceiptError("receipt cleanup requires the exact terminal event")
        directory_fd = self._open_root()
        try:
            try:
                os.unlink(_identity_filename(identity), dir_fd=directory_fd)
            except FileNotFoundError:
                return
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _next_host_call_id(self, directory_fd: int) -> int:
        maximum = 0
        for name in _validated_names(directory_fd):
            if name.startswith("."):
                continue
            receipt = self._read_final(directory_fd, name)
            if receipt.host_call_id > maximum:
                maximum = receipt.host_call_id
        return maximum + 1

    def _read_final(self, directory_fd: int, name: str) -> TaskHostTerminalReceipt:
        raw = _read_stable_file(directory_fd, name, "terminal receipt")
        try:
            document = json.loads(raw.decode("utf-8"))
            receipt = TaskHostTerminalReceipt.model_validate(document)
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise TerminalReceiptError("invalid terminal receipt") from error
        canonical = canonical_json_bytes(receipt.model_dump(mode="json"))
        if canonical != raw:
            raise TerminalReceiptError("changed terminal receipt")
        return receipt

    def _read_selection(self, directory_fd: int, name: str) -> HostSelectionReceipt:
        raw = _read_stable_file(directory_fd, name, "host selection receipt")
        try:
            document = json.loads(raw.decode("utf-8"))
            receipt = HostSelectionReceipt.model_validate(document)
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise TerminalReceiptError("invalid host selection receipt") from error
        canonical = canonical_json_bytes(receipt.model_dump(mode="json"))
        if canonical != raw:
            raise TerminalReceiptError("changed host selection receipt")
        try:
            self._require_selection_authority().verify(
                _selection_signature_payload(receipt),
                authority_id=receipt.authority_id,
                authority_version=receipt.authority_version,
                signature=receipt.signature,
            )
        except HostAuthorityError as error:
            raise TerminalReceiptError("host selection signature is not authentic") from error
        return receipt

    def _open_root(self) -> int:
        if self._parent_fd is not None:
            parent_fd = os.dup(self._parent_fd)
            name = self.root.name
        else:
            parent_fd = _open_absolute_directory(self.root.parent)
            name = self.root.name
        try:
            return _open_directory_at(parent_fd, name, "receipt store")
        finally:
            os.close(parent_fd)


def _open_absolute_directory(path: Path) -> int:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.absolute().parts[1:]:
            child = _open_directory_at(descriptor, component, "receipt path component")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent_fd: int, name: str, kind: str) -> int:
    try:
        enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise TerminalReceiptError(f"{kind} must be a no-follow directory: {name}") from error
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        enumerated.st_dev,
        enumerated.st_ino,
    ):
        os.close(descriptor)
        raise TerminalReceiptError(f"{kind} identity changed while opening: {name}")
    return descriptor


def _open_or_create_directory_at(parent_fd: int, name: str, kind: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    return _open_directory_at(parent_fd, name, kind)


def _lock_selection_namespace(directory_fd: int) -> int:
    descriptor = os.open(
        ".publish.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise TerminalReceiptError("host selection publication lock is invalid")
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def _pending_selection_scope(name: str) -> str:
    prefix = "."
    suffix = ".pending"
    if not name.startswith(prefix) or not name.endswith(suffix):
        raise TerminalReceiptError("unknown host selection transaction marker")
    digest = name[len(prefix) : -len(suffix)]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise TerminalReceiptError("unknown host selection transaction marker")
    return digest


def _read_stable_file(directory_fd: int, name: str, kind: str) -> bytes:
    try:
        enumerated = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise
        raise TerminalReceiptError(f"{kind} is unreadable: {name}") from error
    if stat.S_ISLNK(enumerated.st_mode):
        raise TerminalReceiptError(f"symlink {kind}")
    if not stat.S_ISREG(enumerated.st_mode) or enumerated.st_nlink != 1:
        raise TerminalReceiptError(f"linked or unstable {kind}")
    descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (enumerated.st_dev, enumerated.st_ino)
        ):
            raise TerminalReceiptError(f"linked or unstable {kind}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        raw = b"".join(chunks)
        final = os.fstat(descriptor)
        if (
            final.st_nlink != 1
            or (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
        ):
            raise TerminalReceiptError(f"changed {kind}")
    finally:
        os.close(descriptor)
    return raw


def _validated_names(directory_fd: int) -> list[str]:
    try:
        names = os.listdir(directory_fd)
    except OSError as error:
        raise TerminalReceiptError("cannot enumerate receipt directory") from error
    for name in names:
        if name in {"", ".", ".."} or "/" in name:
            raise TerminalReceiptError(f"invalid receipt filename: {name!r}")
    return sorted(names)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise TerminalReceiptError("filesystem write returned zero bytes")
        view = view[written:]


def _rename_no_replace_at(directory_fd: int, source: str, destination: str) -> None:
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        result = libc.renameatx_np(
            directory_fd,
            ctypes.c_char_p(source_bytes),
            directory_fd,
            ctypes.c_char_p(destination_bytes),
            ctypes.c_uint(0x00000004),
        )
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(
            directory_fd,
            ctypes.c_char_p(source_bytes),
            directory_fd,
            ctypes.c_char_p(destination_bytes),
            ctypes.c_uint(1),
        )
    else:  # pragma: no cover
        os.link(
            source,
            destination,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(source, dir_fd=directory_fd)
        return
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    raise OSError(error_number, os.strerror(error_number), destination)


__all__ = [
    "HostSelectionReceipt",
    "TaskActivityTerminalObserved",
    "TerminalReceiptError",
    "TerminalReceiptSink",
    "TerminalReceiptStore",
    "prove_call_quiescent",
]
