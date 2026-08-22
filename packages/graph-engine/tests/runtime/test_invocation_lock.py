from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import shutil
import stat
import sys

import pytest

import graph_engine.runtime.engine as engine_runtime
import graph_engine.runtime.invocation_lock as invocation_lock_runtime
import graph_engine.runtime.ledger as ledger_runtime
from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition import (
    EditableWheelPluginSource,
    EditableWheelProductSource,
    FrozenComposition,
    InvocationLock,
    RegistryPlatform,
    ResolutionRequest,
)
from graph_engine.runtime.secret_sources import empty_runtime_authorization
from graph_engine.runtime.seed import EMPTY_RUNTIME_AUTHORIZATION_DIGEST, empty_invocation_seed
from graph_engine.runtime.engine import Engine, EngineError, EnginePublicationIndeterminate
from graph_engine.runtime.events import (
    EventEnvelope,
    InvocationStarted,
    TaskAttemptStarted,
    TaskLeaseAcquired,
)
from graph_engine.runtime.invocation_lock import (
    InvocationDrift,
    authenticate_invocation_lock,
    authenticate_invocation_start_intent,
    install_invocation_lock_at,
    install_invocation_start_intent_at,
    read_invocation_lock_at,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import fold_events
from graph_engine.runtime.planner import plan_next
from graph_engine.runtime.scheduler import FakeClock, Scheduler
from graph_engine.runtime.workspace import SnapshotStore


_LOCK_NAME = "invocation.lock.json"
_START_INTENT_NAME = "invocation.start.json"


def _empty_seed():
    return empty_invocation_seed()


def _start_intent_document(composition: FrozenComposition, entrypoint: str) -> dict[str, object]:
    seed = _empty_seed()
    return {
        "schema_version": "1",
        "event_schema_version": "2",
        "lock_digest": composition.lock_digest,
        "entrypoint": entrypoint,
        "runtime_authorization_digest": EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
        "root_input_digest": seed.root_input_digest,
        "initial_tree_id": seed.workspace.tree_id,
    }


def _install_start_intent_at(
    invocation_fd: int,
    *,
    lock_digest: str,
    entrypoint: str,
) -> object:
    seed = _empty_seed()
    return install_invocation_start_intent_at(
        invocation_fd,
        lock_digest=lock_digest,
        entrypoint=entrypoint,
        runtime_authorization_digest=EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
        root_input_digest=seed.root_input_digest,
        initial_tree_id=seed.workspace.tree_id,
    )


def _lock() -> InvocationLock:
    canonical_bytes = (
        Path(__file__)
        .parents[1]
        .joinpath("composition", "invocation-lock-v2.golden.json")
        .read_text(encoding="utf-8")
        .strip()
        .encode()
    )
    projection = json.loads(canonical_bytes)
    return InvocationLock.model_validate(
        {
            **projection,
            "canonical_bytes": canonical_bytes,
            "digest": hashlib.sha256(canonical_bytes).hexdigest(),
        }
    )


def _directory_descriptor(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _toy_a_composition(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    second_entrypoint: bool = False,
    interrupt: bool = False,
) -> FrozenComposition:
    source = root / "source"
    repository = Path(__file__).parents[4]
    shutil.copytree(
        repository / "examples" / "graph-engine-toy-a",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    if second_entrypoint:
        declaration_path = source / "graph_engine_toy_a" / "product-declaration.json"
        declaration_path.write_text(
            declaration_path.read_text(encoding="utf-8").replace(
                '"entrypoints": {"hello": "root"}',
                '"entrypoints": {"hello": "root", "alternate": "root"}',
            ),
            encoding="utf-8",
        )
        provider_path = source / "graph_engine_toy_a" / "product.py"
        provider_path.write_text(
            provider_path.read_text(encoding="utf-8")
            .replace(
                'entrypoints={"hello": "root"}',
                'entrypoints={"hello": "root", "alternate": "root"}',
            )
            .replace(
                '"entrypoints": {"hello": "root"}',
                '"entrypoints": {"hello": "root", "alternate": "root"}',
            ),
            encoding="utf-8",
        )
    if interrupt:
        declaration_path = source / "graph_engine_toy_a" / "product-declaration.json"
        declaration = json.loads(declaration_path.read_bytes())
        workflow = declaration["manifest"]["workflow"]
        workflow["graphs"]["root"] = {
            "edges": [{"from": "pause", "to": "done"}],
            "max_activations": 2,
            "nodes": {
                "done": {"kind": "end"},
                "pause": {
                    "actions": ["continue"],
                    "kind": "interrupt",
                    "reason": "choose",
                },
            },
            "start": "pause",
        }
        declaration_path.write_bytes(canonical_json_bytes(declaration))
        (source / "graph_engine_toy_a" / "product.py").write_text(
            "from __future__ import annotations\n"
            "import json\n"
            "from pathlib import Path\n"
            "from graph_engine.composition import ProductManifest\n"
            "class ToyAProduct:\n"
            "    @staticmethod\n"
            "    def manifest() -> ProductManifest:\n"
            '        document = json.loads(Path(__file__).with_name("product-declaration.json").read_bytes())\n'
            '        return ProductManifest.model_validate(document["manifest"])\n',
            encoding="utf-8",
        )
    source_files = tuple(
        sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    )
    monkeypatch.syspath_prepend(str(source))
    for module_name in tuple(sys.modules):
        if module_name == "graph_engine_toy_a" or module_name.startswith("graph_engine_toy_a."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    common: dict[str, object] = {
        "distribution": "graph-engine-toy-a",
        "entrypoint_name": "toy-a",
        "source_root": source,
        "source_files": source_files,
    }
    request = ResolutionRequest(
        product=EditableWheelProductSource(
            **common,
            declaration_path="graph_engine_toy_a/product-declaration.json",
        ),
        plugins=(
            EditableWheelPluginSource(
                **common,
                declaration_path="graph_engine_toy_a/plugin-declaration.json",
            ),
        ),
    )
    return RegistryPlatform().resolve(request)


def _toy_b_composition(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FrozenComposition:
    source = root / "source"
    repository = Path(__file__).parents[4]
    shutil.copytree(
        repository / "examples" / "graph-engine-toy-b",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    source_files = tuple(
        sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    )
    monkeypatch.syspath_prepend(str(source))
    for module_name in tuple(sys.modules):
        if module_name == "graph_engine_toy_b" or module_name.startswith("graph_engine_toy_b."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    return RegistryPlatform().resolve(
        ResolutionRequest(
            product=EditableWheelProductSource(
                distribution="graph-engine-toy-b",
                entrypoint_name="toy-b",
                source_root=source,
                source_files=source_files,
                declaration_path="graph_engine_toy_b/product-declaration.json",
            ),
            plugins=(
                EditableWheelPluginSource(
                    distribution="graph-engine-toy-b",
                    entrypoint_name="toy-b",
                    source_root=source,
                    source_files=source_files,
                    declaration_path="graph_engine_toy_b/plugin-declaration.json",
                ),
            ),
        )
    )


def _ledger_bytes(root: Path, invocation_id: str) -> bytes:
    ledger = root / "invocations" / invocation_id / "ledger"
    if not ledger.exists():
        return b""
    return b"".join(path.read_bytes() for path in sorted(ledger.glob("[0-9]*.json")))


def _replace_start_intent(
    invocation: Path,
    composition: FrozenComposition,
    entrypoint: str,
) -> None:
    intent_path = invocation / _START_INTENT_NAME
    intent_path.unlink()
    intent_path.write_bytes(
        canonical_json_bytes(_start_intent_document(composition, entrypoint))
    )
    intent_path.chmod(0o400)


def test_install_read_and_authenticate_preserve_exact_immutable_lock_bytes(tmp_path: Path) -> None:
    expected = _lock()
    invocation_fd = _directory_descriptor(tmp_path)
    try:
        install_invocation_lock_at(invocation_fd, expected)

        assert read_invocation_lock_at(invocation_fd) == expected.canonical_bytes
        authenticate_invocation_lock(invocation_fd, expected)
        installed = os.stat(_LOCK_NAME, dir_fd=invocation_fd, follow_symlinks=False)
        assert stat.S_ISREG(installed.st_mode)
        assert installed.st_nlink == 1
        assert installed.st_mode & 0o222 == 0
    finally:
        os.close(invocation_fd)


def test_exact_reinstall_is_idempotent_without_replacing_the_lock_inode(tmp_path: Path) -> None:
    expected = _lock()
    invocation_fd = _directory_descriptor(tmp_path)
    try:
        install_invocation_lock_at(invocation_fd, expected)
        before = os.stat(_LOCK_NAME, dir_fd=invocation_fd, follow_symlinks=False)

        install_invocation_lock_at(invocation_fd, expected)

        after = os.stat(_LOCK_NAME, dir_fd=invocation_fd, follow_symlinks=False)
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    finally:
        os.close(invocation_fd)


@pytest.mark.parametrize(
    ("phase", "installed"),
    [
        ("before_file_fsync", False),
        ("after_file_fsync", False),
        ("before_rename", False),
        ("after_rename", True),
        ("before_directory_fsync", True),
        ("after_directory_fsync", True),
    ],
)
def test_install_fault_cuts_are_exactly_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    installed: bool,
) -> None:
    expected = _lock()
    invocation_fd = _directory_descriptor(tmp_path)

    def cut(actual: str) -> None:
        if actual == phase:
            raise OSError(f"simulated {phase} cut")

    try:
        monkeypatch.setattr(invocation_lock_runtime, "_invocation_lock_boundary", cut)
        with pytest.raises(OSError, match=phase):
            install_invocation_lock_at(invocation_fd, expected)
        assert (tmp_path / _LOCK_NAME).exists() is installed

        monkeypatch.setattr(invocation_lock_runtime, "_invocation_lock_boundary", lambda _phase: None)
        install_invocation_lock_at(invocation_fd, expected)
        authenticate_invocation_lock(invocation_fd, expected)
    finally:
        os.close(invocation_fd)


@pytest.mark.parametrize(
    ("phase", "installed"),
    [
        ("before_file_fsync", False),
        ("after_file_fsync", False),
        ("before_rename", False),
        ("after_rename", True),
        ("before_directory_fsync", True),
        ("after_directory_fsync", True),
    ],
)
def test_start_intent_fault_cuts_are_exactly_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    installed: bool,
) -> None:
    expected = _lock()
    invocation_fd = _directory_descriptor(tmp_path)

    def cut(actual: str) -> None:
        if actual == phase:
            raise OSError(f"simulated intent {phase} cut")

    try:
        monkeypatch.setattr(invocation_lock_runtime, "_invocation_start_intent_boundary", cut)
        with pytest.raises(OSError, match=f"intent {phase}"):
            _install_start_intent_at(
                invocation_fd,
                lock_digest=expected.digest,
                entrypoint="hello",
            )
        assert (tmp_path / _START_INTENT_NAME).exists() is installed

        monkeypatch.setattr(
            invocation_lock_runtime,
            "_invocation_start_intent_boundary",
            lambda _phase: None,
        )
        _install_start_intent_at(
            invocation_fd,
            lock_digest=expected.digest,
            entrypoint="hello",
        )
        intent = authenticate_invocation_start_intent(
            invocation_fd,
            lock_digest=expected.digest,
            entrypoint="hello",
        )
        assert intent.digest == hashlib.sha256(intent.canonical_bytes).hexdigest()
    finally:
        os.close(invocation_fd)


def test_missing_corrupt_symlinked_and_multiply_linked_locks_fail_closed(tmp_path: Path) -> None:
    expected = _lock()
    invocation_fd = _directory_descriptor(tmp_path)
    try:
        with pytest.raises(InvocationDrift, match="missing"):
            authenticate_invocation_lock(invocation_fd, expected)

        (tmp_path / _LOCK_NAME).write_bytes(b"{}")
        (tmp_path / _LOCK_NAME).chmod(0o400)
        with pytest.raises(InvocationDrift, match="differs"):
            authenticate_invocation_lock(invocation_fd, expected)
        (tmp_path / _LOCK_NAME).unlink()

        outside = tmp_path.parent / f"{tmp_path.name}-outside-lock"
        outside.write_bytes(expected.canonical_bytes)
        (tmp_path / _LOCK_NAME).symlink_to(outside)
        with pytest.raises(InvocationDrift, match="regular|symlink|stable"):
            authenticate_invocation_lock(invocation_fd, expected)
        (tmp_path / _LOCK_NAME).unlink()

        install_invocation_lock_at(invocation_fd, expected)
        os.link(tmp_path / _LOCK_NAME, tmp_path / "second-link")
        with pytest.raises(InvocationDrift, match="link|stable"):
            authenticate_invocation_lock(invocation_fd, expected)
    finally:
        os.close(invocation_fd)


def test_lock_read_failure_is_not_masked_by_descriptor_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _lock()
    (tmp_path / _LOCK_NAME).write_bytes(expected.canonical_bytes)
    (tmp_path / _LOCK_NAME).chmod(0o400)
    invocation_fd = _directory_descriptor(tmp_path)
    real_open = os.open
    real_read = os.read
    real_close = os.close
    lock_descriptor: int | None = None

    def track_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal lock_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if path == _LOCK_NAME:
            lock_descriptor = descriptor
        return descriptor

    def fail_read(descriptor: int, count: int) -> bytes:
        if descriptor == lock_descriptor:
            raise OSError("primary lock read failure")
        return real_read(descriptor, count)

    def fail_close(descriptor: int) -> None:
        if descriptor == lock_descriptor:
            real_close(descriptor)
            raise OSError("secondary descriptor close failure")
        real_close(descriptor)

    try:
        with monkeypatch.context() as faults:
            faults.setattr(invocation_lock_runtime.os, "open", track_open)
            faults.setattr(invocation_lock_runtime.os, "read", fail_read)
            faults.setattr(invocation_lock_runtime.os, "close", fail_close)
            with pytest.raises(InvocationDrift, match="read") as captured:
                authenticate_invocation_lock(invocation_fd, expected)
        assert any("close" in note for note in getattr(captured.value, "__notes__", ()))
    finally:
        os.close(invocation_fd)


def test_lock_install_failure_is_not_masked_by_directory_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _lock()
    invocation_fd = _directory_descriptor(tmp_path)
    real_open = os.open
    real_close = os.close
    directory_lock_fd: int | None = None

    def track_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal directory_lock_fd
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if path == ".":
            directory_lock_fd = descriptor
        return descriptor

    def fail_close(descriptor: int) -> None:
        if descriptor == directory_lock_fd:
            real_close(descriptor)
            raise OSError("secondary directory close failure")
        real_close(descriptor)

    def fail_install(phase: str) -> None:
        if phase == "before_file_fsync":
            raise OSError("primary install failure")

    try:
        with monkeypatch.context() as faults:
            faults.setattr(invocation_lock_runtime.os, "open", track_open)
            faults.setattr(invocation_lock_runtime.os, "close", fail_close)
            faults.setattr(invocation_lock_runtime, "_invocation_lock_boundary", fail_install)
            with pytest.raises(OSError, match="primary install failure") as captured:
                install_invocation_lock_at(invocation_fd, expected)
        assert any("close" in note for note in getattr(captured.value, "__notes__", ()))
    finally:
        os.close(invocation_fd)


def test_engine_persists_lock_before_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"

    with Engine(root) as engine:
        with engine.start(composition, entrypoint="hello", invocation_id="run-1", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()) as handle:
            assert handle.lock_digest == composition.lock_digest

    invocation = root / "invocations" / "run-1"
    assert (invocation / _LOCK_NAME).read_bytes() == composition.lock.canonical_bytes
    assert (invocation / _START_INTENT_NAME).read_bytes() == canonical_json_bytes(
        _start_intent_document(composition, "hello")
    )
    first = Ledger(invocation / "ledger").read_all()[0].event
    assert isinstance(first, InvocationStarted)
    assert first.lock_digest == composition.lock_digest


def test_repeated_start_recovers_the_exact_lock_after_a_prebootstrap_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"

    def cut(phase: str) -> None:
        if phase == "before_ledger_bootstrap":
            raise OSError("simulated pre-bootstrap cut")

    with Engine(root) as engine:
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", cut)
        with pytest.raises(OSError, match="pre-bootstrap"):
            engine.start(composition, entrypoint="hello", invocation_id="recoverable", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        invocation = root / "invocations" / "recoverable"
        assert (invocation / _LOCK_NAME).read_bytes() == composition.lock.canonical_bytes
        assert _ledger_bytes(root, "recoverable") == b""

        monkeypatch.setattr(engine_runtime, "_initialization_boundary", lambda _phase: None)
        with engine.start(composition, entrypoint="hello", invocation_id="recoverable", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()) as handle:
            assert handle.lock_digest == composition.lock_digest

    events = Ledger(root / "invocations" / "recoverable" / "ledger").read_all()
    assert sum(isinstance(item.event, InvocationStarted) for item in events) == 1


def test_repeated_start_reconciles_an_authoritative_bootstrap_after_the_append_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"

    def cut(phase: str) -> None:
        if phase == "after_ledger_bootstrap":
            raise OSError("simulated post-bootstrap cut")

    with Engine(root) as engine:
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", cut)
        with pytest.raises(OSError, match="post-bootstrap"):
            engine.start(composition, entrypoint="hello", invocation_id="reconcile-bootstrap", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        assert _ledger_bytes(root, "reconcile-bootstrap")

        monkeypatch.setattr(engine_runtime, "_initialization_boundary", lambda _phase: None)
        engine.start(
            composition,
            entrypoint="hello",
            invocation_id="reconcile-bootstrap",
        seed=empty_invocation_seed(), authorization=empty_runtime_authorization(),
        ).close()

    events = Ledger(root / "invocations" / "reconcile-bootstrap" / "ledger").read_all()
    assert sum(isinstance(item.event, InvocationStarted) for item in events) == 1


@pytest.mark.parametrize(
    ("phase", "published"),
    [
        ("before_invocation_rename", False),
        ("after_invocation_rename", True),
        ("before_root_fsync", True),
        ("after_root_fsync", True),
    ],
)
def test_invocation_publication_fault_cuts_are_exactly_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    published: bool,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    invocation = root / "invocations" / "publication-cut"

    def cut(actual: str) -> None:
        if actual == phase:
            raise OSError(f"simulated {phase} cut")

    with Engine(root) as engine:
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", cut)
        with pytest.raises(OSError, match=phase):
            engine.start(composition, entrypoint="hello", invocation_id="publication-cut", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        assert invocation.exists() is published
        if published:
            assert (invocation / _LOCK_NAME).read_bytes() == composition.lock.canonical_bytes
            assert (invocation / _START_INTENT_NAME).is_file()
            assert _ledger_bytes(root, "publication-cut") == b""

        monkeypatch.setattr(engine_runtime, "_initialization_boundary", lambda _phase: None)
        engine.start(composition, entrypoint="hello", invocation_id="publication-cut", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()

    events = Ledger(invocation / "ledger").read_all()
    assert sum(isinstance(item.event, InvocationStarted) for item in events) == 1


def test_invocation_publication_never_replaces_a_racing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    raced = root / "invocations" / "publication-race"
    raced_inode: tuple[int, int] | None = None

    def inject_destination(phase: str) -> None:
        nonlocal raced_inode
        if phase == "before_invocation_rename":
            raced.mkdir()
            opened = raced.stat()
            raced_inode = (opened.st_dev, opened.st_ino)

    with Engine(root) as engine:
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", inject_destination)
        with pytest.raises(InvocationDrift, match="lock|missing"):
            engine.start(composition, entrypoint="hello", invocation_id="publication-race", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert raced_inode is not None
    current = raced.stat()
    assert (current.st_dev, current.st_ino) == raced_inode
    assert tuple(raced.iterdir()) == ()


def test_recovery_fsyncs_a_published_invocation_before_claim_or_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"

    def cut_after_rename(phase: str) -> None:
        if phase == "after_invocation_rename":
            raise OSError("simulated post-rename cut")

    with Engine(root) as engine:
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", cut_after_rename)
        with pytest.raises(OSError, match="post-rename"):
            engine.start(composition, entrypoint="hello", invocation_id="durable-recovery", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

        namespace_fsyncs = 0
        real_fsync = os.fsync
        real_claim = Engine._acquire_runner_claim

        def count_fsync(descriptor: int) -> None:
            nonlocal namespace_fsyncs
            if descriptor == engine._invocations_fd:
                namespace_fsyncs += 1
            real_fsync(descriptor)

        def require_durable_claim(self: Engine, invocation_fd: int) -> int:
            assert namespace_fsyncs > 0
            return real_claim(self, invocation_fd)

        monkeypatch.setattr(engine_runtime, "_initialization_boundary", lambda _phase: None)
        monkeypatch.setattr(engine_runtime.os, "fsync", count_fsync)
        monkeypatch.setattr(Engine, "_acquire_runner_claim", require_durable_claim)
        engine.start(composition, entrypoint="hello", invocation_id="durable-recovery", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()

    assert namespace_fsyncs >= 1


def test_namespace_lock_must_have_one_stable_link_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    with Engine(root) as engine:
        namespace_lock = root / "invocations" / ".namespace.lock"
        namespace_lock.touch(mode=0o600)
        os.link(namespace_lock, root / "namespace-lock-alias")

        with pytest.raises(EngineError, match="namespace lock|stable"):
            engine.start(composition, entrypoint="hello", invocation_id="unsafe-lock", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert not (root / "invocations" / "unsafe-lock").exists()


def test_start_reauthenticates_exact_intent_immediately_before_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(
        tmp_path / "composition",
        monkeypatch,
        second_entrypoint=True,
    )
    root = tmp_path / "engine"
    invocation = root / "invocations" / "intent-race"

    def replace_intent(phase: str) -> None:
        if phase != "before_ledger_bootstrap":
            return
        _replace_start_intent(invocation, composition, "alternate")

    with Engine(root) as engine:
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", replace_intent)
        with pytest.raises(InvocationDrift, match="entrypoint|intent"):
            engine.start(composition, entrypoint="hello", invocation_id="intent-race", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert _ledger_bytes(root, "intent-race") == b""


def test_handle_actions_reauthenticate_the_exact_selected_entrypoint_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(
        tmp_path / "composition",
        monkeypatch,
        second_entrypoint=True,
    )
    root = tmp_path / "engine"
    with Engine(root) as engine:
        handle = engine.start(composition, entrypoint="hello", invocation_id="handle-intent", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        intent_path = handle.invocation_root / _START_INTENT_NAME
        intent_path.unlink()
        intent_path.write_bytes(
            canonical_json_bytes(_start_intent_document(composition, "alternate"))
        )
        intent_path.chmod(0o400)
        before = _ledger_bytes(root, "handle-intent")
        claims = 0

        def reject_claim(self: Engine, invocation_fd: int) -> int:
            del self, invocation_fd
            nonlocal claims
            claims += 1
            raise AssertionError("runner claim must not be attempted")

        monkeypatch.setattr(Engine, "_acquire_runner_claim", reject_claim)
        with pytest.raises(InvocationDrift, match="entrypoint|intent"):
            engine.run_until_blocked(handle)
        handle.close()

    assert claims == 0
    assert _ledger_bytes(root, "handle-intent") == before


def test_handle_actions_reject_ledger_entrypoint_drift_before_claim_or_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(
        tmp_path / "composition",
        monkeypatch,
        second_entrypoint=True,
    )
    root = tmp_path / "engine"
    with Engine(root) as engine:
        handle = engine.start(composition, entrypoint="hello", invocation_id="ledger-entrypoint", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        first_batch = next(iter(sorted((handle.invocation_root / "ledger").glob("[0-9]*.json"))))
        raw = json.loads(first_batch.read_bytes())
        started = InvocationStarted.model_validate(raw[0]["event"])
        raw[0] = EventEnvelope.from_event(
            1,
            started.model_copy(update={"entrypoint": "alternate"}),
        ).model_dump(mode="json")
        first_batch.write_bytes(canonical_json_bytes(raw))
        before = _ledger_bytes(root, "ledger-entrypoint")
        claims = 0
        appends = 0

        def reject_claim(self: Engine, invocation_fd: int) -> int:
            del self, invocation_fd
            nonlocal claims
            claims += 1
            raise AssertionError("runner claim must not be attempted")

        def reject_append(self: Engine, *args: object, **kwargs: object) -> None:
            del self, args, kwargs
            nonlocal appends
            appends += 1
            raise AssertionError("append must not be attempted")

        monkeypatch.setattr(Engine, "_acquire_runner_claim", reject_claim)
        monkeypatch.setattr(Engine, "_append_authenticated", reject_append)
        with pytest.raises(InvocationDrift, match="entrypoint|intent|bootstrap"):
            engine.run_until_blocked(handle)
        handle.close()

    assert claims == 0
    assert appends == 0
    assert _ledger_bytes(root, "ledger-entrypoint") == before


def test_bootstrap_cleanup_preserves_primary_and_closes_store_and_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    claim_fd: int | None = None
    store_closed = False
    claim_closed = False
    real_acquire = Engine._acquire_runner_claim
    real_store_close = SnapshotStore.close
    real_close = os.close

    def capture_claim(self: Engine, invocation_fd: int) -> int:
        nonlocal claim_fd
        claim_fd = real_acquire(self, invocation_fd)
        return claim_fd

    def fail_store_close(store: SnapshotStore) -> None:
        nonlocal store_closed
        real_store_close(store)
        store_closed = True
        raise OSError("secondary store close failure")

    def fail_claim_close(descriptor: int) -> None:
        nonlocal claim_closed
        if descriptor == claim_fd:
            real_close(descriptor)
            claim_closed = True
            raise OSError("secondary claim close failure")
        real_close(descriptor)

    def fail_bootstrap(phase: str) -> None:
        if phase == "before_ledger_bootstrap":
            raise RuntimeError("primary bootstrap failure")

    with Engine(root) as engine, monkeypatch.context() as faults:
        faults.setattr(Engine, "_acquire_runner_claim", capture_claim)
        faults.setattr(SnapshotStore, "close", fail_store_close)
        faults.setattr(engine_runtime.os, "close", fail_claim_close)
        faults.setattr(engine_runtime, "_initialization_boundary", fail_bootstrap)
        with pytest.raises(RuntimeError, match="primary bootstrap failure") as captured:
            engine.start(composition, entrypoint="hello", invocation_id="cleanup-fault", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert store_closed
    assert claim_closed
    notes = getattr(captured.value, "__notes__", ())
    assert any("store" in note for note in notes)
    assert any("claim" in note for note in notes)


def test_open_cleanup_preserves_primary_and_closes_store_and_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    with Engine(root) as bootstrap:
        bootstrap.start(composition, entrypoint="hello", invocation_id="open-cleanup", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()
    claim_fd: int | None = None
    store_closed = False
    claim_closed = False
    real_acquire = Engine._acquire_runner_claim
    real_store_close = SnapshotStore.close
    real_close = os.close

    def capture_claim(self: Engine, invocation_fd: int) -> int:
        nonlocal claim_fd
        claim_fd = real_acquire(self, invocation_fd)
        return claim_fd

    def fail_recovery(store: SnapshotStore, authoritative_tree_id: str | None) -> None:
        del store, authoritative_tree_id
        raise RuntimeError("primary open recovery failure")

    def fail_store_close(store: SnapshotStore) -> None:
        nonlocal store_closed
        real_store_close(store)
        store_closed = True
        raise OSError("secondary open store close failure")

    def fail_claim_close(descriptor: int) -> None:
        nonlocal claim_closed
        if descriptor == claim_fd:
            real_close(descriptor)
            claim_closed = True
            raise OSError("secondary open claim close failure")
        real_close(descriptor)

    with Engine(root) as engine, monkeypatch.context() as faults:
        faults.setattr(Engine, "_acquire_runner_claim", capture_claim)
        faults.setattr(SnapshotStore, "recover_head_transaction", fail_recovery)
        faults.setattr(SnapshotStore, "close", fail_store_close)
        faults.setattr(engine_runtime.os, "close", fail_claim_close)
        with pytest.raises(RuntimeError, match="primary open recovery failure") as captured:
            engine.open("open-cleanup", composition, authorization=empty_runtime_authorization())

    assert store_closed
    assert claim_closed
    notes = getattr(captured.value, "__notes__", ())
    assert any("store" in note for note in notes)
    assert any("claim" in note for note in notes)


def test_lock_only_recovery_rejects_a_different_selected_entrypoint_without_claim_or_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(
        tmp_path / "composition",
        monkeypatch,
        second_entrypoint=True,
    )
    root = tmp_path / "engine"

    def cut(phase: str) -> None:
        if phase == "before_ledger_bootstrap":
            raise OSError("simulated pre-bootstrap cut")

    with Engine(root) as engine:
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", cut)
        with pytest.raises(OSError, match="pre-bootstrap"):
            engine.start(composition, entrypoint="hello", invocation_id="entrypoint-drift", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    claims = 0
    appends = 0
    real_claim = Engine._acquire_runner_claim
    real_append = Engine._append_authenticated

    def count_claim(self: Engine, invocation_fd: int) -> int:
        nonlocal claims
        claims += 1
        return real_claim(self, invocation_fd)

    def count_append(self: Engine, *args: object, **kwargs: object) -> None:
        nonlocal appends
        appends += 1
        real_append(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(engine_runtime, "_initialization_boundary", lambda _phase: None)
    monkeypatch.setattr(Engine, "_acquire_runner_claim", count_claim)
    monkeypatch.setattr(Engine, "_append_authenticated", count_append)
    with Engine(root) as engine, pytest.raises(InvocationDrift, match="entrypoint|intent"):
        engine.start(composition, entrypoint="alternate", invocation_id="entrypoint-drift", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert claims == 0
    assert appends == 0
    assert _ledger_bytes(root, "entrypoint-drift") == b""


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink", "hardlink"])
def test_start_intent_drift_fails_before_claim_or_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    with Engine(root) as engine:
        engine.start(composition, entrypoint="hello", invocation_id="intent-drift", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()
    invocation = root / "invocations" / "intent-drift"
    intent_path = invocation / _START_INTENT_NAME
    if damage == "missing":
        intent_path.unlink()
    elif damage == "corrupt":
        intent_path.chmod(0o600)
        intent_path.write_bytes(b"{}")
        intent_path.chmod(0o400)
    elif damage == "symlink":
        intent_path.unlink()
        outside = tmp_path / "outside-intent"
        outside.write_bytes(b"{}")
        intent_path.symlink_to(outside)
    else:
        os.link(intent_path, invocation / "second-intent-link")

    before = _ledger_bytes(root, "intent-drift")
    claims = 0
    appends = 0

    def reject_claim(self: Engine, invocation_fd: int) -> int:
        del self, invocation_fd
        nonlocal claims
        claims += 1
        raise AssertionError("runner claim must not be attempted")

    def reject_append(self: Engine, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        nonlocal appends
        appends += 1
        raise AssertionError("append must not be attempted")

    monkeypatch.setattr(Engine, "_acquire_runner_claim", reject_claim)
    monkeypatch.setattr(Engine, "_append_authenticated", reject_append)
    with Engine(root) as engine, pytest.raises(InvocationDrift):
        engine.start(composition, entrypoint="hello", invocation_id="intent-drift", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert claims == 0
    assert appends == 0
    assert _ledger_bytes(root, "intent-drift") == before


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unequal"])
def test_engine_open_rejects_lock_drift_before_claim_or_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    with Engine(root) as engine:
        engine.start(composition, entrypoint="hello", invocation_id="drift", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()
    lock_path = root / "invocations" / "drift" / _LOCK_NAME
    if damage == "missing":
        lock_path.unlink()
    else:
        replacement = (
            b"{}"
            if damage == "corrupt"
            else _toy_b_composition(tmp_path / "unequal-composition", monkeypatch).lock.canonical_bytes
        )
        lock_path.chmod(0o600)
        lock_path.write_bytes(replacement)
        lock_path.chmod(0o400)
    before = _ledger_bytes(root, "drift")
    claims = 0
    appends = 0
    real_claim = Engine._acquire_runner_claim
    real_append = Engine._append_authenticated

    def count_claim(self: Engine, invocation_fd: int) -> int:
        nonlocal claims
        claims += 1
        return real_claim(self, invocation_fd)

    def count_append(self: Engine, *args: object, **kwargs: object) -> None:
        nonlocal appends
        appends += 1
        real_append(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Engine, "_acquire_runner_claim", count_claim)
    monkeypatch.setattr(Engine, "_append_authenticated", count_append)
    with Engine(root) as engine, pytest.raises(InvocationDrift):
        engine.open("drift", composition, authorization=empty_runtime_authorization())

    assert claims == 0
    assert appends == 0
    assert _ledger_bytes(root, "drift") == before


def test_repeated_start_preserves_primary_failure_when_invocation_close_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    with Engine(root) as engine:
        engine.start(composition, entrypoint="hello", invocation_id="close-masking", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()
        real_open_invocation = Engine._open_invocation
        real_close = os.close
        invocation_fd: int | None = None
        failed_close = False

        def track_invocation(self: Engine, invocation_id: str) -> int:
            nonlocal invocation_fd
            invocation_fd = real_open_invocation(self, invocation_id)
            return invocation_fd

        def fail_completion(self: Engine, *args: object, **kwargs: object) -> object:
            del self, args, kwargs
            raise RuntimeError("primary repeated-start failure")

        def fail_close(descriptor: int) -> None:
            nonlocal failed_close
            if descriptor == invocation_fd and not failed_close:
                failed_close = True
                real_close(descriptor)
                raise OSError("secondary invocation close failure")
            real_close(descriptor)

        monkeypatch.setattr(Engine, "_open_invocation", track_invocation)
        monkeypatch.setattr(Engine, "_complete_start_at", fail_completion)
        monkeypatch.setattr(engine_runtime.os, "close", fail_close)
        with pytest.raises(RuntimeError, match="primary repeated-start failure") as captured:
            engine.start(composition, entrypoint="hello", invocation_id="close-masking", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert failed_close
    assert any("close" in note for note in getattr(captured.value, "__notes__", ()))


def test_engine_open_rejects_bootstrap_lock_digest_mismatch_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    with Engine(root) as engine:
        engine.start(composition, entrypoint="hello", invocation_id="bootstrap-drift", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()
    ledger_root = root / "invocations" / "bootstrap-drift" / "ledger"
    first_batch = next(iter(sorted(ledger_root.glob("[0-9]*.json"))))
    raw = json.loads(first_batch.read_bytes())
    started = InvocationStarted.model_validate(raw[0]["event"])
    raw[0] = EventEnvelope.from_event(
        1,
        started.model_copy(update={"lock_digest": "f" * 64}),
    ).model_dump(mode="json")
    from graph_engine.canonical import canonical_json_bytes

    first_batch.write_bytes(canonical_json_bytes(raw))
    claims = 0
    real_claim = Engine._acquire_runner_claim

    def count_claim(self: Engine, invocation_fd: int) -> int:
        nonlocal claims
        claims += 1
        return real_claim(self, invocation_fd)

    monkeypatch.setattr(Engine, "_acquire_runner_claim", count_claim)
    with Engine(root) as engine, pytest.raises(InvocationDrift, match="bootstrap|digest"):
        engine.open("bootstrap-drift", composition, authorization=empty_runtime_authorization())
    assert claims == 0


def test_open_reauthenticates_identity_inside_expired_lease_reclaim_before_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(
        tmp_path / "composition",
        monkeypatch,
        second_entrypoint=True,
    )
    root = tmp_path / "engine"
    with Engine(root, clock=FakeClock(10.0)) as bootstrap:
        handle = bootstrap.start(
            composition,
            entrypoint="hello",
            invocation_id="reclaim-intent",
        seed=empty_invocation_seed(), authorization=empty_runtime_authorization(),
        )
        handle.close()
    invocation = root / "invocations" / "reclaim-intent"
    ledger = Ledger(invocation / "ledger")
    envelopes = ledger.read_all()
    plan = plan_next(composition.workflow, fold_events(envelopes))
    ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
    task = plan.tasks[0]
    ledger.append_batch(
        (
            TaskAttemptStarted(
                activation_id=task.activation_id,
                attempt=task.attempt,
                lease_expires_at="20",
            ),
            TaskLeaseAcquired(
                task_id=task.task_id,
                activation_id=task.activation_id,
                attempt=task.attempt,
                owner_id="crashed-owner",
                acquired_at=10.0,
                heartbeat_at=10.0,
                expires_at=20.0,
            ),
        ),
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    before = _ledger_bytes(root, "reclaim-intent")
    intent_path = invocation / _START_INTENT_NAME
    real_reclaim = Scheduler.reclaim_expired

    def drift_then_reclaim(
        scheduler: Scheduler,
        leases: object = None,
    ) -> tuple[str, ...]:
        intent_path.unlink()
        intent_path.write_bytes(
            canonical_json_bytes(_start_intent_document(composition, "alternate"))
        )
        intent_path.chmod(0o400)
        return real_reclaim(scheduler, leases)  # type: ignore[arg-type]

    monkeypatch.setattr(Scheduler, "reclaim_expired", drift_then_reclaim)
    with (
        Engine(root, clock=FakeClock(21.0)) as engine,
        pytest.raises(
            InvocationDrift,
            match="entrypoint|intent|bootstrap",
        ),
    ):
        engine.open("reclaim-intent", composition, authorization=empty_runtime_authorization())

    assert _ledger_bytes(root, "reclaim-intent") == before


def test_start_rejects_a_replaced_final_invocation_directory_before_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    invocation = root / "invocations" / "replaced-start"
    displaced = root / "invocations" / "replaced-start.displaced"

    def replace_final_name(phase: str) -> None:
        if phase != "after_invocation_rename":
            return
        invocation.rename(displaced)
        invocation.mkdir(mode=0o700)

    with Engine(root) as engine:
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", replace_final_name)
        with pytest.raises(InvocationDrift, match="anchor|directory|identity"):
            engine.start(composition, entrypoint="hello", invocation_id="replaced-start", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert _ledger_bytes(root, "replaced-start") == b""
    assert not (displaced / "ledger").exists()


def test_open_rejects_a_final_directory_replaced_after_open_without_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    with Engine(root) as bootstrap:
        bootstrap.start(composition, entrypoint="hello", invocation_id="replaced-open", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()
    invocation = root / "invocations" / "replaced-open"
    displaced = root / "invocations" / "replaced-open.displaced"
    before = _ledger_bytes(root, "replaced-open")
    claims = 0
    real_open = Engine._open_invocation
    real_claim = Engine._acquire_runner_claim

    def open_then_replace(self: Engine, invocation_id: str) -> int:
        descriptor = real_open(self, invocation_id)
        invocation.rename(displaced)
        invocation.mkdir(mode=0o700)
        return descriptor

    def count_claim(self: Engine, invocation_fd: int) -> int:
        nonlocal claims
        claims += 1
        return real_claim(self, invocation_fd)

    monkeypatch.setattr(Engine, "_open_invocation", open_then_replace)
    monkeypatch.setattr(Engine, "_acquire_runner_claim", count_claim)
    with Engine(root) as engine, pytest.raises(InvocationDrift, match="anchor|directory|identity"):
        engine.open("replaced-open", composition, authorization=empty_runtime_authorization())

    assert claims == 0
    assert _ledger_bytes(root, "replaced-open") == b""
    assert _ledger_bytes(root, "replaced-open.displaced") == before


@pytest.mark.parametrize(
    "phase",
    ["after_root_fsync", "before_ledger_bootstrap", "after_ledger_bootstrap", "start_return"],
)
def test_start_reanchors_the_final_invocation_name_at_every_acknowledgement_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    invocation = root / "invocations" / "replaced-at-boundary"
    displaced = root / "invocations" / "replaced-at-boundary.displaced"
    replaced = False

    def replace_final_name(boundary: str) -> None:
        nonlocal replaced
        if boundary != phase or replaced:
            return
        replaced = True
        invocation.rename(displaced)
        invocation.mkdir(mode=0o700)

    with Engine(root) as engine:
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", replace_final_name)
        monkeypatch.setattr(engine_runtime, "_transition_boundary", replace_final_name)
        with pytest.raises(InvocationDrift, match="anchor|directory|identity"):
            engine.start(
                composition,
                entrypoint="hello",
                invocation_id="replaced-at-boundary",
            seed=empty_invocation_seed(), authorization=empty_runtime_authorization(),
            )

    assert replaced
    assert _ledger_bytes(root, "replaced-at-boundary") == b""


def test_open_reanchors_the_final_invocation_name_immediately_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    with Engine(root) as bootstrap:
        bootstrap.start(composition, entrypoint="hello", invocation_id="open-return", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()
    invocation = root / "invocations" / "open-return"
    displaced = root / "invocations" / "open-return.displaced"
    before = _ledger_bytes(root, "open-return")

    def replace_before_return(boundary: str) -> None:
        if boundary != "open_return" or displaced.exists():
            return
        invocation.rename(displaced)
        invocation.mkdir(mode=0o700)

    monkeypatch.setattr(engine_runtime, "_transition_boundary", replace_before_return)
    with Engine(root) as engine, pytest.raises(InvocationDrift, match="anchor|directory|identity"):
        engine.open("open-return", composition, authorization=empty_runtime_authorization())

    assert _ledger_bytes(root, "open-return") == b""
    assert _ledger_bytes(root, "open-return.displaced") == before


def test_terminal_open_guards_noop_head_recovery_before_checkpoint_or_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(
        tmp_path / "composition",
        monkeypatch,
        second_entrypoint=True,
    )
    root = tmp_path / "engine"
    with Engine(root) as bootstrap:
        handle = bootstrap.start(composition, entrypoint="hello", invocation_id="terminal-head", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        assert bootstrap.run_until_blocked(handle).status in {"succeeded", "failed", "stopped"}
        handle.close()
    invocation = root / "invocations" / "terminal-head"
    head = invocation / "workspace" / "HEAD.json"
    before_head = head.read_bytes()
    before_ledger = _ledger_bytes(root, "terminal-head")

    def drift_before_noop_recovery(boundary: str) -> None:
        if boundary == "head_recovery":
            _replace_start_intent(invocation, composition, "alternate")

    monkeypatch.setattr(engine_runtime, "_transition_boundary", drift_before_noop_recovery)
    with Engine(root) as engine, pytest.raises(InvocationDrift, match="entrypoint|intent"):
        engine.open("terminal-head", composition, authorization=empty_runtime_authorization())

    assert head.read_bytes() == before_head
    assert not (invocation / "workspace" / ".HEAD-transaction.json").exists()
    assert _ledger_bytes(root, "terminal-head") == before_ledger


def test_planner_append_reauthenticates_after_fault_seam_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(
        tmp_path / "composition",
        monkeypatch,
        second_entrypoint=True,
    )
    root = tmp_path / "engine"
    invocation = root / "invocations" / "planner-guard"
    with Engine(root) as engine:
        handle = engine.start(composition, entrypoint="hello", invocation_id="planner-guard", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        before = _ledger_bytes(root, "planner-guard")

        def drift(context: str) -> None:
            if context == "planner_append":
                _replace_start_intent(invocation, composition, "alternate")

        monkeypatch.setattr(engine_runtime, "_transition_boundary", drift, raising=False)
        try:
            with pytest.raises(InvocationDrift, match="entrypoint|intent"):
                engine.run_until_blocked(handle)
        finally:
            handle.close()

    assert _ledger_bytes(root, "planner-guard") == before


def test_resume_append_reauthenticates_after_fault_seam_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(
        tmp_path / "composition",
        monkeypatch,
        second_entrypoint=True,
        interrupt=True,
    )
    root = tmp_path / "engine"
    invocation = root / "invocations" / "resume-guard"
    with Engine(root) as engine:
        handle = engine.start(composition, entrypoint="hello", invocation_id="resume-guard", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        assert engine.run_until_blocked(handle).status == "interrupted"
        before = _ledger_bytes(root, "resume-guard")

        def drift(context: str) -> None:
            if context == "resume_append":
                _replace_start_intent(invocation, composition, "alternate")

        monkeypatch.setattr(engine_runtime, "_transition_boundary", drift, raising=False)
        try:
            with pytest.raises(InvocationDrift, match="entrypoint|intent"):
                engine.resume(handle, action="continue", payload={})
        finally:
            handle.close()

    assert _ledger_bytes(root, "resume-guard") == before


def test_engine_has_one_authenticated_authoritative_append_boundary() -> None:
    tree = ast.parse(inspect.getsource(engine_runtime.Engine))
    method_calls: dict[str, list[str]] = {}
    for method in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        calls = [
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name)
        ]
        method_calls[method.name] = calls

    assert "_append" not in method_calls
    assert {method for method, calls in method_calls.items() if "append_validated_batch" in calls} == {
        "_append_authenticated"
    }
    assert method_calls["_complete_start_at"].count("_append_authenticated") == 1
    assert method_calls["_run_until_blocked_with_store"].count("_append_authenticated") == 1
    assert method_calls["_resume_claimed"].count("_append_authenticated") == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires process-crash fork semantics")
def test_repeated_start_repairs_visible_bootstrap_after_final_install_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    process_id = os.fork()
    if process_id == 0:

        def crash_after_final_install(phase: str) -> None:
            if phase == "final_installed":
                os._exit(91)

        ledger_runtime._append_boundary = crash_after_final_install
        child_engine = Engine(root)
        child_engine.start(composition, entrypoint="hello", invocation_id="durable-bootstrap", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        os._exit(0)

    _child, status = os.waitpid(process_id, 0)
    assert os.waitstatus_to_exitcode(status) == 91
    ledger = Ledger(root / "invocations" / "durable-bootstrap" / "ledger")
    assert sum(isinstance(envelope.event, InvocationStarted) for envelope in ledger.read_all()) == 1
    durability_barriers = 0
    real_ensure_durable = Ledger.ensure_durable

    def track_durability_barrier(self: Ledger) -> None:
        nonlocal durability_barriers
        durability_barriers += 1
        real_ensure_durable(self)

    monkeypatch.setattr(Ledger, "ensure_durable", track_durability_barrier)
    with Engine(root) as engine:
        engine.start(
            composition,
            entrypoint="hello",
            invocation_id="durable-bootstrap",
        seed=empty_invocation_seed(), authorization=empty_runtime_authorization(),
        ).close()

    assert durability_barriers >= 1
    assert sum(isinstance(envelope.event, InvocationStarted) for envelope in ledger.read_all()) == 1


def test_repeated_start_durability_failure_is_indeterminate_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    with Engine(root) as bootstrap:
        bootstrap.start(composition, entrypoint="hello", invocation_id="durability-failure", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()
    before = _ledger_bytes(root, "durability-failure")
    claims = 0

    def fail_durability(_ledger: Ledger) -> None:
        raise OSError("ledger directory fsync failed")

    def reject_claim(self: Engine, invocation_fd: int) -> int:
        del self, invocation_fd
        nonlocal claims
        claims += 1
        raise AssertionError("runner claim must not be attempted")

    monkeypatch.setattr(Ledger, "ensure_durable", fail_durability)
    monkeypatch.setattr(Engine, "_acquire_runner_claim", reject_claim)
    with Engine(root) as engine, pytest.raises(EnginePublicationIndeterminate, match="durability"):
        engine.start(
            composition,
            entrypoint="hello",
            invocation_id="durability-failure",
        seed=empty_invocation_seed(), authorization=empty_runtime_authorization(),
        )

    assert claims == 0
    assert _ledger_bytes(root, "durability-failure") == before


def test_existing_invocation_authenticates_drift_before_requested_entrypoint_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _toy_a_composition(
        tmp_path / "original",
        monkeypatch,
        second_entrypoint=True,
    )
    root = tmp_path / "engine"
    with Engine(root) as bootstrap:
        bootstrap.start(original, entrypoint="alternate", invocation_id="ordering", seed=empty_invocation_seed(), authorization=empty_runtime_authorization()).close()
    drifted = _toy_a_composition(tmp_path / "drifted", monkeypatch)
    before = _ledger_bytes(root, "ordering")
    claims = 0

    def reject_claim(self: Engine, invocation_fd: int) -> int:
        del self, invocation_fd
        nonlocal claims
        claims += 1
        raise AssertionError("runner claim must not be attempted")

    monkeypatch.setattr(Engine, "_acquire_runner_claim", reject_claim)
    with Engine(root) as engine, pytest.raises(InvocationDrift):
        engine.start(drifted, entrypoint="alternate", invocation_id="ordering", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert claims == 0
    assert _ledger_bytes(root, "ordering") == before


def test_staging_cleanup_preserves_primary_and_attempts_close_remove_and_parent_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _toy_a_composition(tmp_path / "composition", monkeypatch)
    root = tmp_path / "engine"
    primary = RuntimeError("primary staging publication failure")
    staging_fd: int | None = None
    close_attempted = False
    remove_attempted = False
    parent_fsync_attempted = False
    real_open_directory = engine_runtime._open_directory_at
    real_remove = engine_runtime._remove_entry_at
    real_close = os.close
    real_fsync = os.fsync

    def capture_staging(parent_fd: int, name: str, kind: str) -> int:
        nonlocal staging_fd
        descriptor = real_open_directory(parent_fd, name, kind)
        if kind == "invocation initialization staging":
            staging_fd = descriptor
        return descriptor

    def fail_publication(phase: str) -> None:
        if phase == "before_invocation_rename":
            raise primary

    def fail_staging_close(descriptor: int) -> None:
        nonlocal close_attempted
        if descriptor == staging_fd and not close_attempted:
            close_attempted = True
            real_close(descriptor)
            raise OSError("secondary staging close failure")
        real_close(descriptor)

    def fail_staging_remove(parent_fd: int, name: str) -> None:
        nonlocal remove_attempted
        real_remove(parent_fd, name)
        if name.startswith(".cleanup-staging.invocation-init-"):
            remove_attempted = True
            raise OSError("secondary staging removal failure")

    with Engine(root) as engine:

        def fail_cleanup_fsync(descriptor: int) -> None:
            nonlocal parent_fsync_attempted
            if remove_attempted and descriptor == engine._invocations_fd:
                parent_fsync_attempted = True
                raise OSError("secondary parent fsync failure")
            real_fsync(descriptor)

        monkeypatch.setattr(engine_runtime, "_open_directory_at", capture_staging)
        monkeypatch.setattr(engine_runtime, "_initialization_boundary", fail_publication)
        monkeypatch.setattr(engine_runtime, "_remove_entry_at", fail_staging_remove)
        monkeypatch.setattr(engine_runtime.os, "close", fail_staging_close)
        monkeypatch.setattr(engine_runtime.os, "fsync", fail_cleanup_fsync)
        with pytest.raises(RuntimeError) as captured:
            engine.start(composition, entrypoint="hello", invocation_id="cleanup-staging", seed=empty_invocation_seed(), authorization=empty_runtime_authorization())

    assert captured.value is primary
    assert close_attempted
    assert remove_attempted
    assert parent_fsync_attempted
    notes = getattr(primary, "__notes__", ())
    assert any("close" in note for note in notes)
    assert any("remov" in note for note in notes)
    assert any("fsync" in note for note in notes)


@pytest.mark.parametrize(
    ("operation", "claimed_method", "kwargs"),
    [
        ("run_until_blocked", "_run_until_blocked_claimed", {}),
        ("resume", "_resume_claimed", {"action": "continue", "payload": {}}),
    ],
)
def test_direct_actions_preserve_primary_when_claim_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    claimed_method: str,
    kwargs: dict[str, object],
) -> None:
    composition = _toy_a_composition(
        tmp_path / "composition",
        monkeypatch,
        interrupt=True,
    )
    root = tmp_path / "engine"
    primary = RuntimeError(f"primary {operation} failure")
    claim_fd: int | None = None
    close_attempted = False
    real_claim = Engine._acquire_runner_claim
    real_close = os.close

    with Engine(root) as engine:
        handle = engine.start(composition, entrypoint="hello", invocation_id=operation, seed=empty_invocation_seed(), authorization=empty_runtime_authorization())
        if operation == "resume":
            assert engine.run_until_blocked(handle).status == "interrupted"

        def capture_claim(self: Engine, invocation_fd: int) -> int:
            nonlocal claim_fd
            claim_fd = real_claim(self, invocation_fd)
            return claim_fd

        def fail_claimed(*args: object, **call_kwargs: object) -> object:
            del args, call_kwargs
            raise primary

        def fail_claim_close(descriptor: int) -> None:
            nonlocal close_attempted
            if descriptor == claim_fd and not close_attempted:
                close_attempted = True
                real_close(descriptor)
                raise OSError("secondary claim close failure")
            real_close(descriptor)

        monkeypatch.setattr(Engine, "_acquire_runner_claim", capture_claim)
        monkeypatch.setattr(Engine, claimed_method, fail_claimed)
        monkeypatch.setattr(engine_runtime.os, "close", fail_claim_close)
        try:
            with pytest.raises(RuntimeError) as captured:
                getattr(engine, operation)(handle, **kwargs)
        finally:
            handle.close()

    assert captured.value is primary
    assert close_attempted
    assert any("claim" in note for note in getattr(primary, "__notes__", ()))


def test_cleanup_without_a_primary_raises_the_first_secondary_and_notes_the_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_fd = os.open(tmp_path / "first", os.O_WRONLY | os.O_CREAT, 0o600)
    second_fd = os.open(tmp_path / "second", os.O_WRONLY | os.O_CREAT, 0o600)
    real_close = os.close

    def fail_closes(descriptor: int) -> None:
        real_close(descriptor)
        if descriptor == first_fd:
            raise OSError("first cleanup failure")
        if descriptor == second_fd:
            raise OSError("second cleanup failure")

    def fail_action() -> None:
        raise OSError("third cleanup failure")

    monkeypatch.setattr(engine_runtime.os, "close", fail_closes)
    with pytest.raises(OSError, match="first cleanup failure") as captured:
        engine_runtime._cleanup_runtime_resources(
            None,
            store=None,
            descriptors=((first_fd, "first descriptor"), (second_fd, "second descriptor")),
            actions=((fail_action, "final action"),),
        )

    notes = getattr(captured.value, "__notes__", ())
    assert any("second descriptor" in note and "second cleanup" in note for note in notes)
    assert any("final action" in note and "third cleanup" in note for note in notes)
