from __future__ import annotations

import json
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

import graph_engine.runtime.engine as engine_runtime
import graph_engine.runtime.ledger as ledger_runtime
from graph_engine import ENGINE_API_VERSION
from graph_engine.graph.compiler import compile_workflow
from graph_engine.graph.schema import WorkflowDef
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import (
    EnginePorts,
    PluginDescriptor,
    PluginRuntime,
    ResourceClaims,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.product import PluginRequirement, ProductManifest, ResolvedProduct, resolve_product
from graph_engine.runtime.engine import (
    Engine,
    EngineConflictError,
    EngineError,
    EnginePublicationIndeterminate,
)
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphCompleted,
    GraphStarted,
    InterruptResumed,
    InvocationFinished,
    NodeInterrupted,
    TaskAttemptStarted,
    TaskLeaseAcquired,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import fold_events
from graph_engine.runtime.planner import plan_next
from graph_engine.runtime.scheduler import FakeClock
from graph_engine.runtime.scheduler import Scheduler
from graph_engine.runtime.workspace import SnapshotStore


@dataclass(frozen=True)
class _StaticPlugin:
    handlers: Mapping[str, TaskHandler]

    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            plugin_id="test.empty",
            plugin_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            task_handlers=tuple(sorted(self.handlers)),
            commit_validators=(),
        )

    def bind(self, ports: EnginePorts) -> PluginRuntime:
        assert ports.engine_api == ENGINE_API_VERSION
        return PluginRuntime(task_handlers=self.handlers, commit_validators={})


@dataclass(frozen=True)
class _StaticProduct:
    workflow: WorkflowDef

    def manifest(self) -> ProductManifest:
        return ProductManifest(
            product_id="test.product",
            product_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            plugins=(PluginRequirement(plugin_id="test.empty", version="1.0.0"),),
            workflow=self.workflow,
        )


def _resolved(
    workflow: dict[str, object], handlers: Mapping[str, TaskHandler] | None = None
) -> ResolvedProduct:
    product = _StaticProduct(WorkflowDef.model_validate(workflow))
    return resolve_product(product, {"test.empty": _StaticPlugin(handlers or {})})


def _rewrite_ledger(ledger_root: Path, events: tuple[object, ...]) -> None:
    envelopes = tuple(
        EventEnvelope.from_event(sequence, event)  # type: ignore[arg-type]
        for sequence, event in enumerate(events, start=1)
    )
    for batch in ledger_root.glob("[0-9]*.json"):
        batch.unlink()
    final = ledger_root / f"{1:010d}-{len(envelopes):010d}.json"
    final.write_bytes(
        canonical_json_bytes(cast(JSONValue, [envelope.model_dump(mode="json") for envelope in envelopes]))
    )


class _InProcessTestHost:
    """Deliberately unconfined test double; never a production host."""

    async def execute(
        self,
        handler: TaskHandler,
        request: TaskRequest,
        *,
        workspace_root: Path,
        heartbeat: Callable[[], None],
    ) -> TaskOutcome:
        return await handler(
            request,
            TaskContext(workspace_root=workspace_root, heartbeat=heartbeat),
        )


def _task_product(
    handler: Callable[..., Awaitable[TaskOutcome]],
    *,
    nested: bool = False,
) -> ResolvedProduct:
    root_start = "child" if nested else "work"
    root_nodes: dict[str, object]
    root_edges: list[dict[str, str]]
    graphs: dict[str, object]
    task = {
        "kind": "task",
        "capability": "test.empty.run",
        "retry": "once",
        "timeout": "short",
    }
    if nested:
        root_nodes = {
            "child": {"kind": "subgraph", "graph": "nested"},
            "end": {"kind": "end"},
        }
        root_edges = [{"from": "child", "to": "end"}]
        graphs = {
            "root": {
                "max_activations": 3,
                "start": root_start,
                "nodes": root_nodes,
                "edges": root_edges,
            },
            "nested": {
                "max_activations": 2,
                "start": "work",
                "nodes": {"work": task, "end": {"kind": "end"}},
                "edges": [{"from": "work", "to": "end"}],
            },
        }
    else:
        graphs = {
            "root": {
                "max_activations": 2,
                "start": root_start,
                "nodes": {"work": task, "end": {"kind": "end"}},
                "edges": [{"from": "work", "to": "end"}],
            }
        }
    return _resolved(
        {
            "name": "task-test",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": graphs,
        },
        {"test.empty.run": cast(TaskHandler, handler)},
    )


def _nested_task_interrupt_product(
    handler: Callable[..., Awaitable[TaskOutcome]],
) -> ResolvedProduct:
    return _resolved(
        {
            "name": "crash-matrix",
            "entrypoints": {"main": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 5}},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "child",
                    "nodes": {
                        "child": {"kind": "subgraph", "graph": "nested"},
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "child", "to": "end"}],
                },
                "nested": {
                    "max_activations": 3,
                    "start": "work",
                    "nodes": {
                        "work": {
                            "kind": "task",
                            "capability": "test.empty.run",
                            "retry": "once",
                            "timeout": "short",
                            "resources": {"writes": ["out.txt"]},
                        },
                        "review": {
                            "kind": "interrupt",
                            "reason": "matrix review",
                            "actions": ["approve"],
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [
                        {"from": "work", "to": "review"},
                        {"from": "review", "to": "end"},
                    ],
                },
            },
        },
        {"test.empty.run": cast(TaskHandler, handler)},
    )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return Engine(tmp_path)


@pytest.fixture
def resolved_subgraph_product() -> ResolvedProduct:
    return _resolved(
        {
            "name": "subgraph-test",
            "entrypoints": {"main": "parent"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "parent": {
                    "max_activations": 3,
                    "start": "child",
                    "nodes": {
                        "child": {
                            "kind": "subgraph",
                            "graph": "nested",
                            "input": {"child": "done"},
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "child", "to": "end"}],
                },
                "nested": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                },
            },
        }
    )


@pytest.fixture
def resolved_interrupt_product() -> ResolvedProduct:
    return _resolved(
        {
            "name": "interrupt-test",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "review",
                    "nodes": {
                        "review": {
                            "kind": "interrupt",
                            "reason": "human review",
                            "actions": ["approve", "reject"],
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "review", "to": "end"}],
                }
            },
        }
    )


def test_engine_has_no_default_product(tmp_path: Path) -> None:
    engine = Engine(tmp_path)
    with pytest.raises(TypeError, match="product"):
        engine.start(entrypoint="main", invocation_id="missing-product")  # type: ignore[call-arg]


def test_subgraph_completion_returns_to_parent(
    engine: Engine, resolved_subgraph_product: ResolvedProduct
) -> None:
    handle = engine.start(resolved_subgraph_product, entrypoint="main", invocation_id="inv-sub")
    result = engine.run_until_blocked(handle)
    assert result.status == "succeeded"
    assert result.output == {"child": "done"}


def test_interrupt_requires_explicit_resume(
    engine: Engine, resolved_interrupt_product: ResolvedProduct
) -> None:
    handle = engine.start(resolved_interrupt_product, entrypoint="main", invocation_id="inv-int")
    blocked = engine.run_until_blocked(handle)
    assert blocked.status == "interrupted"
    assert blocked.actions == ("approve", "reject")
    resumed = engine.resume(handle, action="approve", payload={"reviewer": "human"})
    assert engine.run_until_blocked(resumed).status == "succeeded"


def test_multilevel_subgraphs_have_canonical_instances_and_return_in_order(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "nested-test",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "child",
                    "nodes": {
                        "child": {"kind": "subgraph", "graph": "child"},
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "child", "to": "end"}],
                },
                "child": {
                    "max_activations": 2,
                    "start": "grandchild",
                    "nodes": {
                        "grandchild": {
                            "kind": "subgraph",
                            "graph": "grandchild",
                            "input": {"depth": 2},
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "grandchild", "to": "end"}],
                },
                "grandchild": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                },
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(product, entrypoint="main", invocation_id="nested")

    result = engine.run_until_blocked(handle)

    assert result.status == "succeeded"
    assert result.output == {"depth": 2}
    children = [
        graph for graph in result.projection.graph_instances if graph.parent_activation_id is not None
    ]
    assert len(children) == 2
    for child in children:
        assert child.graph_instance_id == canonical_digest(
            {"parent_activation_id": child.parent_activation_id, "graph_id": child.graph_id}
        )


def test_nested_interrupt_blocks_root_and_records_exact_location(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "nested-interrupt",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "child",
                    "nodes": {
                        "child": {"kind": "subgraph", "graph": "nested"},
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "child", "to": "end"}],
                },
                "nested": {
                    "max_activations": 2,
                    "start": "review",
                    "nodes": {
                        "review": {
                            "kind": "interrupt",
                            "reason": "nested review",
                            "actions": ["continue"],
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "review", "to": "end"}],
                },
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(product, entrypoint="main", invocation_id="nested-int")

    blocked = engine.run_until_blocked(handle)

    pending = blocked.projection.pending_interrupt
    assert blocked.status == "interrupted"
    assert blocked.reason == "nested review"
    assert pending is not None
    assert pending.graph_instance_id != "root"
    assert pending.actions == ("continue",)
    assert pending.model_dump(mode="json")["input"] == {"config": {}, "tokens": [{}]}


def test_resume_mismatch_repeat_and_payload_freezing_do_not_append(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "resume-test",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "review",
                    "nodes": {
                        "review": {
                            "kind": "interrupt",
                            "reason": "review",
                            "actions": ["approve"],
                        },
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "review", "to": "end"}],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(product, entrypoint="main", invocation_id="resume")
    engine.run_until_blocked(handle)
    ledger = Ledger(handle.invocation_root / "ledger")
    before = ledger.read_all()

    with pytest.raises(EngineError, match="not allowed"):
        engine.resume(handle, action="reject", payload=None)
    assert ledger.read_all() == before

    payload: dict[str, JSONValue] = {"reviewers": ["a"]}
    engine.resume(handle, action="approve", payload=payload)
    cast(list[str], payload["reviewers"]).append("mutated")
    after = ledger.read_all()
    resumed = next(item.event for item in after if item.event.kind == "interrupt_resumed")
    assert resumed.model_dump(mode="json")["payload"] == {"reviewers": ["a"]}
    with pytest.raises(EngineError, match="no pending"):
        engine.resume(handle, action="approve", payload=None)
    assert ledger.read_all() == after


def test_concurrent_resume_commits_only_one_atomic_sequence(
    tmp_path: Path, resolved_interrupt_product: ResolvedProduct
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(resolved_interrupt_product, entrypoint="main", invocation_id="resume-race")
    engine.run_until_blocked(handle)

    def attempt() -> object:
        try:
            return engine.resume(handle, action="approve", payload={"race": True})
        except EngineError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _: attempt(), range(2)))

    assert sum(not isinstance(outcome, EngineError) for outcome in outcomes) == 1
    kinds = [item.event.kind for item in Ledger(handle.invocation_root / "ledger").read_all()]
    assert kinds.count("interrupt_resumed") == 1
    assert kinds.count("node_completed") == 1


@pytest.mark.parametrize("invocation_id", ["../escape", "a/b", r"a\\b", ".", "..", ""])
def test_invocation_id_is_confined(tmp_path: Path, invocation_id: str) -> None:
    product = _resolved(
        {
            "name": "confinement",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    with pytest.raises(EngineError, match="invalid invocation id"):
        Engine(tmp_path).start(product, entrypoint="main", invocation_id=invocation_id)


def test_open_rejects_product_digest_mismatch_without_appending(tmp_path: Path) -> None:
    first = _resolved(
        {
            "name": "first",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    second = first.__class__(
        manifest=first.manifest.model_copy(update={"product_version": "2.0.0"}),
        descriptors=first.descriptors,
        registry=first.registry,
        workflow=first.workflow,
        digest="f" * 64,
    )
    handle = Engine(tmp_path).start(first, entrypoint="main", invocation_id="digest")
    ledger = Ledger(handle.invocation_root / "ledger")
    before = ledger.read_all()

    with pytest.raises(EngineError, match="digest mismatch"):
        Engine(tmp_path).open("digest", second)

    assert ledger.read_all() == before


def test_duplicate_start_rejects_digest_mismatch_without_replacing_invocation(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="duplicate",
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    before = ledger.read_all()
    mismatched = replace(resolved_interrupt_product, digest="f" * 64)

    with pytest.raises(EngineError, match="digest mismatch"):
        engine.start(mismatched, entrypoint="main", invocation_id="duplicate")

    assert ledger.read_all() == before
    assert handle.workspace.head_tree_id()


def test_missing_host_fails_closed_without_calling_handler(tmp_path: Path) -> None:
    called = False

    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        nonlocal called
        called = True
        return TaskOutcome.succeeded("unsafe")

    product = _task_product(handler)
    engine = Engine(tmp_path, clock=FakeClock(10.0))
    handle = engine.start(product, entrypoint="main", invocation_id="no-host")

    result = engine.run_until_blocked(handle)

    assert result.status == "failed"
    assert not called


def test_explicit_host_runs_task_and_terminal_invocation_reopens(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded({"ran": True})

    product = _task_product(handler)
    clock = FakeClock(10.0)
    engine = Engine(tmp_path, clock=clock, host=_InProcessTestHost())
    handle = engine.start(product, entrypoint="main", invocation_id="hosted")
    result = engine.run_until_blocked(handle)

    assert result.status == "succeeded"
    assert result.output == {"ran": True}
    reopened = Engine(tmp_path, clock=clock, host=_InProcessTestHost()).open("hosted", product)
    assert (
        Engine(tmp_path, clock=clock, host=_InProcessTestHost())
        .open("hosted", product)
        .workspace.head_tree_id()
        == reopened.workspace.head_tree_id()
    )


def test_child_task_failure_and_stop_propagate_to_root(tmp_path: Path) -> None:
    async def fail(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.failed("invalid_input", "bad child")

    failed_product = _task_product(fail, nested=True)
    failed_engine = Engine(tmp_path / "failed", clock=FakeClock(10), host=_InProcessTestHost())
    failed_handle = failed_engine.start(failed_product, entrypoint="main", invocation_id="child-failed")
    failed = failed_engine.run_until_blocked(failed_handle)
    assert failed.status == "failed"
    assert all(graph.status == "failed" for graph in failed.projection.graph_instances)

    async def stop(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.stopped("operator_stop")

    stopped_product = _task_product(stop, nested=True)
    stopped_engine = Engine(tmp_path / "stopped", clock=FakeClock(10), host=_InProcessTestHost())
    stopped_handle = stopped_engine.start(stopped_product, entrypoint="main", invocation_id="child-stopped")
    stopped = stopped_engine.run_until_blocked(stopped_handle)
    assert stopped.status == "stopped"
    assert stopped.reason == "operator_stop"
    assert all(graph.status == "failed" for graph in stopped.projection.graph_instances)


def test_corrupt_checkpoint_is_ignored_in_favor_of_ledger(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "checkpoint",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(product, entrypoint="main", invocation_id="checkpoint")
    (handle.invocation_root / "checkpoint.json").write_bytes(b"not-json")

    reopened = Engine(tmp_path).open("checkpoint", product)
    result = Engine(tmp_path).open("checkpoint", product)

    assert reopened.workspace.head_tree_id() == result.workspace.head_tree_id()


def test_open_reclaims_expired_lease_before_running(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("should-not-run")

    product = _task_product(handler)
    first_clock = FakeClock(10.0)
    engine = Engine(tmp_path, clock=first_clock, host=_InProcessTestHost())
    handle = engine.start(product, entrypoint="main", invocation_id="expired")
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
    ledger.append_batch(plan.events, expected_next_seq=envelopes[-1].seq + 1)
    task = plan.tasks[0]
    next_seq = ledger.read_all()[-1].seq + 1
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
        expected_next_seq=next_seq,
    )

    reopened_engine = Engine(tmp_path, clock=FakeClock(21.0), host=_InProcessTestHost())
    reopened = reopened_engine.open("expired", product)
    result = reopened_engine.run_until_blocked(reopened)

    assert result.status == "failed"
    assert any(item.event.kind == "task_attempt_failed" for item in ledger.read_all())


def test_concurrent_open_reclaim_has_one_winner_and_one_engine_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("unused")

    product = _task_product(handler)
    handle = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).start(
        product,
        entrypoint="main",
        invocation_id="reclaim-race",
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    envelopes = ledger.read_all()
    plan = plan_next(product.workflow, fold_events(envelopes))
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
                owner_id="expired-owner",
                acquired_at=10,
                heartbeat_at=10,
                expires_at=20,
            ),
        ),
        expected_next_seq=ledger.read_all()[-1].seq + 1,
    )
    barrier = threading.Barrier(2)

    def synchronize_reclaim(phase: str, events: object) -> None:
        if phase == "before" and any(
            getattr(event, "kind", None) == "task_attempt_failed"
            for event in events  # type: ignore[union-attr]
        ):
            barrier.wait(timeout=5)

    monkeypatch.setattr(ledger_runtime, "_validated_append_boundary", synchronize_reclaim)

    def open_once() -> object:
        try:
            return Engine(tmp_path, clock=FakeClock(21), host=_InProcessTestHost()).open(
                "reclaim-race",
                product,
            )
        except EngineError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _index: open_once(), range(2)))

    assert sum(isinstance(item, EngineConflictError) for item in outcomes) == 1
    assert sum(not isinstance(item, EngineError) for item in outcomes) == 1
    assert [item.event.kind for item in ledger.read_all()].count("task_attempt_failed") == 1


def test_concurrent_reopeners_execute_one_live_persisted_attempt_exclusively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    async def task_handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("once")

    class BlockingHost:
        async def execute(
            self,
            handler: TaskHandler,
            request: TaskRequest,
            *,
            workspace_root: Path,
            heartbeat: Callable[[], None],
        ) -> TaskOutcome:
            nonlocal calls
            with calls_lock:
                calls += 1
                current = calls
            if current == 1:
                entered.set()
                assert release.wait(timeout=5)
            return await handler(
                request,
                TaskContext(workspace_root=workspace_root, heartbeat=heartbeat),
            )

    product = _task_product(task_handler)
    root = tmp_path / "exclusive-recovery"
    bootstrap = Engine(root, clock=FakeClock(10), host=BlockingHost())
    initial = bootstrap.start(product, entrypoint="main", invocation_id="exclusive")

    def stop_after_attempt_started(phase: str, events: object) -> None:
        if phase == "after" and any(
            getattr(event, "kind", None) == "task_attempt_started"
            for event in events  # type: ignore[union-attr]
        ):
            raise RuntimeError("stop after persisted task start")

    with monkeypatch.context() as crash:
        crash.setattr(ledger_runtime, "_validated_append_boundary", stop_after_attempt_started)
        with pytest.raises(RuntimeError, match="persisted task start"):
            bootstrap.run_until_blocked(initial)

    host = BlockingHost()
    first_engine = Engine(root, clock=FakeClock(10), host=host)
    second_engine = Engine(root, clock=FakeClock(10), host=host)
    first_handle = first_engine.open("exclusive", product)
    second_handle = second_engine.open("exclusive", product)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_engine.run_until_blocked, first_handle)
        assert entered.wait(timeout=5)
        second = pool.submit(second_engine.run_until_blocked, second_handle)
        try:
            with pytest.raises(EngineConflictError, match="runner|claim"):
                second.result(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=5).status == "succeeded"

    assert calls == 1


class _CrashAfterAppendEngine(Engine):
    def __init__(self, root: Path, cut: int) -> None:
        super().__init__(root)
        self.cut = cut
        self.appended = 0

    def _append(self, ledger, events, existing) -> None:  # type: ignore[no-untyped-def]
        super()._append(ledger, events, existing)
        self.appended += 1
        if self.appended == self.cut:
            raise RuntimeError("simulated process crash after committed batch")


def _finish_interrupt_invocation(
    root: Path,
    product: ResolvedProduct,
    *,
    cut: int | None,
) -> tuple[str, str]:
    engine: Engine = Engine(root) if cut is None else _CrashAfterAppendEngine(root, cut)
    handle = None
    try:
        handle = engine.start(product, entrypoint="main", invocation_id="crash-test")
    except RuntimeError:
        engine = Engine(root)
        try:
            handle = engine.open("crash-test", product)
        except EngineError:
            handle = engine.start(product, entrypoint="main", invocation_id="crash-test")
    assert handle is not None
    try:
        blocked = engine.run_until_blocked(handle)
    except RuntimeError:
        engine = Engine(root)
        handle = engine.open("crash-test", product)
        blocked = engine.run_until_blocked(handle)
    if blocked.status == "interrupted":
        try:
            handle = engine.resume(handle, action="approve", payload={"durable": True})
        except RuntimeError:
            engine = Engine(root)
            handle = engine.open("crash-test", product)
            projection = fold_events(Ledger(handle.invocation_root / "ledger").read_all())
            if projection.pending_interrupt is not None:
                handle = engine.resume(handle, action="approve", payload={"durable": True})
        try:
            final = engine.run_until_blocked(handle)
        except RuntimeError:
            engine = Engine(root)
            handle = engine.open("crash-test", product)
            final = engine.run_until_blocked(handle)
    else:
        final = blocked
    assert final.status == "succeeded"
    envelopes = Ledger(handle.invocation_root / "ledger").read_all()
    ledger_digest = canonical_digest(cast(JSONValue, [item.model_dump(mode="json") for item in envelopes]))
    return ledger_digest, handle.workspace.head_tree_id()


@pytest.mark.parametrize("cut", [1, 2, 3, 4])
def test_reopen_after_each_structural_batch_has_identical_final_digests(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
    cut: int,
) -> None:
    baseline = _finish_interrupt_invocation(tmp_path / "baseline", resolved_interrupt_product, cut=None)
    recovered = _finish_interrupt_invocation(tmp_path / f"cut-{cut}", resolved_interrupt_product, cut=cut)

    assert recovered == baseline


def _finish_crash_matrix_invocation(
    root: Path,
    product: ResolvedProduct,
) -> tuple[str, str]:
    engine = Engine(root, clock=FakeClock(10), host=_InProcessTestHost())
    try:
        handle = engine.open("matrix", product)
    except EngineError:
        handle = engine.start(product, entrypoint="main", invocation_id="matrix")
    result = engine.run_until_blocked(handle)
    if result.status == "interrupted":
        handle = engine.resume(handle, action="approve", payload={"durable": True})
        result = engine.run_until_blocked(handle)
    assert result.status == "succeeded"
    envelopes = Ledger(handle.invocation_root / "ledger").read_all()
    ledger_digest = canonical_digest(cast(JSONValue, [item.model_dump(mode="json") for item in envelopes]))
    return ledger_digest, handle.workspace.head_tree_id()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires process-crash fork semantics")
@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize(
    "event_kind",
    [
        "graph_started",
        "task_attempt_started",
        "task_attempt_succeeded",
        "interrupt_resumed",
    ],
)
def test_fresh_open_after_exact_facade_crash_matrix_has_identical_digests(
    tmp_path: Path,
    phase: str,
    event_kind: str,
) -> None:
    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        (context.workspace_root / "out.txt").write_bytes(b"deterministic")
        return TaskOutcome.succeeded({"task": "done"})

    product = _nested_task_interrupt_product(handler)
    baseline = _finish_crash_matrix_invocation(tmp_path / "baseline", product)
    crash_root = tmp_path / f"{event_kind}-{phase}"
    Engine(crash_root, clock=FakeClock(10), host=_InProcessTestHost()).start(
        product,
        entrypoint="main",
        invocation_id="matrix",
    )
    process_id = os.fork()
    if process_id == 0:

        def crash_boundary(current_phase: str, events: object) -> None:
            if current_phase == phase and any(
                getattr(event, "kind", None) == event_kind
                for event in events  # type: ignore[union-attr]
            ):
                os._exit(91)

        ledger_runtime._validated_append_boundary = crash_boundary
        child_engine = Engine(crash_root, clock=FakeClock(10), host=_InProcessTestHost())
        child_handle = child_engine.open("matrix", product)
        child_result = child_engine.run_until_blocked(child_handle)
        if child_result.status == "interrupted":
            child_handle = child_engine.resume(
                child_handle,
                action="approve",
                payload={"durable": True},
            )
            child_engine.run_until_blocked(child_handle)
        os._exit(0)

    _child, status = os.waitpid(process_id, 0)
    assert os.waitstatus_to_exitcode(status) == 91
    recovered = _finish_crash_matrix_invocation(crash_root, product)

    assert recovered == baseline


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires process-crash fork semantics")
def test_open_durably_syncs_linked_success_before_clearing_head_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed = False

    async def handler(_request: TaskRequest, context: TaskContext) -> TaskOutcome:
        nonlocal armed
        (context.workspace_root / "out.txt").write_bytes(b"durable")
        armed = True
        return TaskOutcome.succeeded("done")

    product = _nested_task_interrupt_product(handler)
    root = tmp_path / "durability-cut"
    Engine(root, clock=FakeClock(10), host=_InProcessTestHost()).start(
        product,
        entrypoint="main",
        invocation_id="durability",
    )
    process_id = os.fork()
    if process_id == 0:

        def crash_after_link(name: str) -> None:
            if armed and name == "final_installed":
                os._exit(91)

        ledger_runtime._append_boundary = crash_after_link
        child_engine = Engine(root, clock=FakeClock(10), host=_InProcessTestHost())
        child_handle = child_engine.open("durability", product)
        child_engine.run_until_blocked(child_handle)
        os._exit(0)

    _child, status = os.waitpid(process_id, 0)
    assert os.waitstatus_to_exitcode(status) == 91
    journal = root / "invocations" / "durability" / "workspace" / ".HEAD-transaction.json"
    assert journal.exists()

    ledger_synced = False
    real_sync = Ledger.ensure_durable
    real_clear = SnapshotStore._clear_head_transaction

    def track_sync(self: Ledger) -> None:
        nonlocal ledger_synced
        real_sync(self)
        ledger_synced = True

    def require_sync_before_clear(self: SnapshotStore, root_fd: int) -> None:
        assert ledger_synced
        real_clear(self, root_fd)

    monkeypatch.setattr(Ledger, "ensure_durable", track_sync)
    monkeypatch.setattr(SnapshotStore, "_clear_head_transaction", require_sync_before_clear)

    reopened = Engine(root, clock=FakeClock(10), host=_InProcessTestHost()).open(
        "durability",
        product,
    )

    assert (
        reopened.workspace.head_tree_id()
        == fold_events(Ledger(reopened.invocation_root / "ledger").read_all()).head_tree_id
    )
    assert not journal.exists()


def test_open_rejects_workspace_head_without_authoritative_head_advance(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="orphan-head",
    )
    attempt = handle.workspace.create_attempt("orphan")
    (attempt.root / "orphan.txt").write_bytes(b"not authoritative")
    candidate = attempt.seal()
    handle.workspace.commit_candidate(candidate, ResourceClaims(writes=("orphan.txt",)))

    with pytest.raises(EngineError, match="HEAD.*authoritative ledger"):
        Engine(tmp_path).open("orphan-head", resolved_interrupt_product)


def test_same_digest_products_keep_exact_handler_binding_per_handle(tmp_path: Path) -> None:
    async def first(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("first")

    async def second(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("second")

    first_product = _task_product(first)
    second_product = _task_product(second)
    assert first_product.digest == second_product.digest
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    first_handle = engine.start(first_product, entrypoint="main", invocation_id="first-binding")
    second_handle = engine.start(second_product, entrypoint="main", invocation_id="second-binding")

    assert engine.run_until_blocked(first_handle).output == "first"
    assert engine.run_until_blocked(second_handle).output == "second"


def test_open_binds_only_returned_handle_to_supplied_same_digest_product(tmp_path: Path) -> None:
    async def original(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("original")

    async def reopened(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("reopened")

    original_product = _task_product(original)
    reopened_product = _task_product(reopened)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    reopened_source = engine.start(
        original_product,
        entrypoint="main",
        invocation_id="open-binding-reopened",
    )
    reopened_handle = engine.open("open-binding-reopened", reopened_product)
    original_handle = engine.start(
        original_product,
        entrypoint="main",
        invocation_id="open-binding-original",
    )
    engine.open("open-binding-original", reopened_product)

    assert engine.run_until_blocked(reopened_handle).output == "reopened"
    assert engine.run_until_blocked(original_handle).output == "original"
    assert reopened_handle.product_digest == reopened_source.product_digest


def test_open_validates_terminal_projection_against_exact_compiled_workflow(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "terminal-validation",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(product, entrypoint="main", invocation_id="terminal-validation")
    assert engine.run_until_blocked(handle).status == "succeeded"
    foreign_workflow = compile_workflow(
        WorkflowDef.model_validate(
            {
                "name": "foreign",
                "entrypoints": {"main": "foreign"},
                "retry": {},
                "timeout": {},
                "graphs": {
                    "foreign": {
                        "max_activations": 1,
                        "start": "end",
                        "nodes": {"end": {"kind": "end"}},
                        "edges": [],
                    }
                },
            }
        ),
        product.registry,
    )
    adversarial = replace(product, workflow=foreign_workflow)

    with pytest.raises(EngineError, match="workflow|graph|entrypoint"):
        Engine(tmp_path).open("terminal-validation", adversarial)


def test_open_rejects_terminal_task_completed_without_committed_attempt(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded({"forged": False})

    product = _task_product(handler)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(product, entrypoint="main", invocation_id="forged-task")
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    removed_kinds = {
        "task_attempt_started",
        "task_lease_acquired",
        "task_lease_heartbeat",
        "task_attempt_succeeded",
        "head_advanced",
    }
    events = tuple(
        envelope.event
        for envelope in Ledger(ledger_root).read_all()
        if envelope.event.kind not in removed_kinds
    )
    forged = tuple(
        EventEnvelope.from_event(sequence, event) for sequence, event in enumerate(events, start=1)
    )
    for batch in ledger_root.glob("[0-9]*.json"):
        batch.unlink()
    final = ledger_root / f"{1:010d}-{len(forged):010d}.json"
    final.write_bytes(
        canonical_json_bytes(cast(JSONValue, [envelope.model_dump(mode="json") for envelope in forged]))
    )
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="completed task|committed successful"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-task",
            product,
        )


def test_open_rejects_completed_graph_without_completed_end_activation(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "forged-empty-terminal",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "gate",
                    "nodes": {
                        "gate": {"kind": "gate", "expression": "false"},
                        "end": {"kind": "end"},
                    },
                    "edges": [{"from": "gate", "to": "end", "condition": "false"}],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(product, entrypoint="main", invocation_id="forged-empty-terminal")
    ledger_root = handle.invocation_root / "ledger"
    initial = Ledger(ledger_root).read_all()
    projection = fold_events(initial)
    planned = plan_next(product.workflow, projection)
    assert [event.kind for event in planned.events] == [
        "token_consumed",
        "node_activated",
        "node_completed",
    ]
    forged_events = (
        *(envelope.event for envelope in initial),
        *planned.events,
        GraphCompleted(graph_instance_id="root", output=None),
        InvocationFinished(invocation_id="forged-empty-terminal", status="succeeded"),
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="completed end activation"):
        Engine(tmp_path).open("forged-empty-terminal", product)


def test_open_rejects_forged_root_input_even_with_matching_start_token(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "root-input",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(product, entrypoint="main", invocation_id="forged-root-input")
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        event.model_copy(update={"input": {"forged": True}})
        if isinstance(event, GraphStarted)
        else event.model_copy(update={"payload": {"forged": True}})
        if event.kind == "token_offered" and event.source is None
        else event
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="root graph input"):
        Engine(tmp_path).open("forged-root-input", product)


def test_open_rejects_forged_terminal_structural_output(tmp_path: Path) -> None:
    product = _resolved(
        {
            "name": "structural-output",
            "entrypoints": {"main": "root"},
            "retry": {},
            "timeout": {},
            "graphs": {
                "root": {
                    "max_activations": 1,
                    "start": "end",
                    "nodes": {"end": {"kind": "end"}},
                    "edges": [],
                }
            },
        }
    )
    engine = Engine(tmp_path)
    handle = engine.start(product, entrypoint="main", invocation_id="forged-structural-output")
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        event.model_copy(update={"output": {"forged": True}})
        if event.kind in {"node_completed", "graph_completed"}
        else event
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="non-canonical output"):
        Engine(tmp_path).open("forged-structural-output", product)


@pytest.mark.parametrize("status", ["failed", "stopped"])
def test_open_rejects_terminal_status_without_compiled_causal_proof(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
    status: str,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id=f"forged-{status}",
    )
    ledger = Ledger(handle.invocation_root / "ledger")
    existing = ledger.read_all()
    ledger.append_batch(
        (
            InvocationFinished(
                invocation_id=f"forged-{status}",
                status=status,  # type: ignore[arg-type]
                terminal_reason=f"forged-{status}",
            ),
        ),
        expected_next_seq=existing[-1].seq + 1,
    )
    assert fold_events(ledger.read_all()).status == status

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path).open(f"forged-{status}", resolved_interrupt_product)


def test_open_rejects_stopped_child_without_compiled_failure_propagation(tmp_path: Path) -> None:
    async def stop(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.stopped("operator_stop")

    product = _task_product(stop, nested=True)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(product, entrypoint="main", invocation_id="forged-stop-propagation")
    assert engine.run_until_blocked(handle).status == "stopped"
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        envelope.event
        for envelope in Ledger(ledger_root).read_all()
        if envelope.event.kind not in {"graph_failed", "node_failed"}
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "stopped"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-stop-propagation",
            product,
        )


def test_open_rejects_failed_child_without_compiled_failure_propagation(tmp_path: Path) -> None:
    async def fail(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.failed("invalid_input", "bad child")

    product = _task_product(fail, nested=True)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(product, entrypoint="main", invocation_id="forged-failure-propagation")
    result = engine.run_until_blocked(handle)
    assert result.status == "failed"
    root_graph_id = next(
        graph.graph_instance_id
        for graph in result.projection.graph_instances
        if graph.parent_graph_instance_id is None
    )
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        envelope.event
        for envelope in Ledger(ledger_root).read_all()
        if not (
            (envelope.event.kind == "graph_failed" and envelope.event.graph_instance_id != root_graph_id)
            or (envelope.event.kind == "node_failed" and envelope.event.failure.kind == "internal")
        )
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "failed"

    with pytest.raises(EngineError, match="causal proof"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-failure-propagation",
            product,
        )


def test_open_rejects_noncanonical_task_id_in_terminal_ledger(tmp_path: Path) -> None:
    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        return TaskOutcome.succeeded("done")

    product = _task_product(handler)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(product, entrypoint="main", invocation_id="forged-task-id")
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    forged_events = tuple(
        event.model_copy(update={"task_id": "forged-task-id"})
        if event.kind in {"task_lease_acquired", "task_lease_heartbeat", "head_advanced"}
        else event
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="canonical task id"):
        Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost()).open(
            "forged-task-id",
            product,
        )


def test_open_rejects_forged_child_input_even_with_matching_start_token(
    tmp_path: Path,
    resolved_subgraph_product: ResolvedProduct,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_subgraph_product,
        entrypoint="main",
        invocation_id="forged-child-input",
    )
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    child_id = next(
        event.graph_instance_id
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
        if isinstance(event, GraphStarted) and event.parent_activation_id is not None
    )
    forged_events = tuple(
        event.model_copy(update={"input": {"forged": True}})
        if isinstance(event, GraphStarted) and event.graph_instance_id == child_id
        else event.model_copy(update={"payload": {"forged": True}})
        if event.kind == "token_offered" and event.graph_instance_id == child_id and event.source is None
        else event
        for event in (envelope.event for envelope in Ledger(ledger_root).read_all())
    )
    _rewrite_ledger(ledger_root, forged_events)
    assert fold_events(Ledger(ledger_root).read_all()).status == "succeeded"

    with pytest.raises(EngineError, match="parent binding"):
        Engine(tmp_path).open("forged-child-input", resolved_subgraph_product)


def test_interrupt_runtime_metadata_is_required() -> None:
    with pytest.raises(ValidationError):
        NodeInterrupted(activation_id="activation", interrupt_id="interrupt")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        InterruptResumed(interrupt_id="interrupt", action="continue")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "fields",
    [
        {"parent_graph_instance_id": "root"},
        {"parent_node_id": "child"},
        {"parent_activation_id": "activation"},
        {
            "parent_graph_instance_id": "root",
            "parent_node_id": "child",
        },
    ],
)
def test_graph_started_parent_binding_is_all_or_none(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="parent"):
        GraphStarted.model_validate({"graph_instance_id": "child", "graph_id": "nested", **fields})


def test_graph_started_root_has_no_parent_and_uses_graph_id_as_instance_id() -> None:
    with pytest.raises(ValidationError, match="root"):
        GraphStarted(graph_instance_id="not-root", graph_id="root")


def test_open_translates_reclaim_conflict_to_engine_conflict(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Engine(tmp_path).start(resolved_interrupt_product, entrypoint="main", invocation_id="open-conflict")

    def conflict(_scheduler: Scheduler) -> tuple[str, ...]:
        from graph_engine.runtime.ledger import LedgerConflictError

        raise LedgerConflictError("lost reclaim CAS")

    monkeypatch.setattr(Scheduler, "reclaim_expired", conflict)
    with pytest.raises(EngineConflictError, match="advanced"):
        Engine(tmp_path).open("open-conflict", resolved_interrupt_product)


def test_run_translates_unreadable_success_reconciliation_to_engine_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed = False
    append_failed = False

    async def handler(_request: TaskRequest, _context: TaskContext) -> TaskOutcome:
        nonlocal armed
        armed = True
        return TaskOutcome.succeeded("published")

    def fail_after_final_install(name: str) -> None:
        nonlocal append_failed
        if armed and name == "final_installed":
            append_failed = True
            raise OSError("append result unavailable")

    original_read = Ledger.read_all

    def fail_reconciliation_read(self: Ledger) -> tuple[EventEnvelope, ...]:
        if append_failed:
            raise OSError("reconciliation unavailable")
        return original_read(self)

    product = _task_product(handler)
    engine = Engine(tmp_path, clock=FakeClock(10), host=_InProcessTestHost())
    handle = engine.start(product, entrypoint="main", invocation_id="indeterminate-success")
    monkeypatch.setattr(ledger_runtime, "_append_boundary", fail_after_final_install)
    monkeypatch.setattr(Ledger, "read_all", fail_reconciliation_read)

    with pytest.raises(EnginePublicationIndeterminate, match="indeterminate"):
        engine.run_until_blocked(handle)


def test_initialization_failure_before_ledger_does_not_poison_invocation_id(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(name: str) -> None:
        if name == "workspace_ready":
            raise OSError("simulated pre-ledger crash")

    monkeypatch.setattr(engine_runtime, "_initialization_boundary", crash, raising=False)
    engine = Engine(tmp_path)
    with pytest.raises(OSError, match="pre-ledger"):
        engine.start(resolved_interrupt_product, entrypoint="main", invocation_id="retryable")
    assert not (tmp_path / "invocations" / "retryable").exists()

    monkeypatch.setattr(engine_runtime, "_initialization_boundary", lambda _name: None, raising=False)
    assert (
        engine.start(resolved_interrupt_product, entrypoint="main", invocation_id="retryable").invocation_id
        == "retryable"
    )


def test_symlinked_invocation_namespace_is_rejected_without_external_write(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "engine"
    root.mkdir()
    (root / "invocations").symlink_to(outside, target_is_directory=True)

    with pytest.raises(EngineError, match="namespace|symlink|trusted"):
        Engine(root).start(resolved_interrupt_product, entrypoint="main", invocation_id="escape")
    assert tuple(outside.iterdir()) == ()


def test_symlinked_engine_root_component_is_rejected_before_external_creation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-root"
    outside.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / "redirect").symlink_to(outside, target_is_directory=True)

    with pytest.raises(EngineError, match="trusted|root|namespace"):
        Engine(anchor / "redirect" / "engine")

    assert tuple(outside.iterdir()) == ()


def test_namespace_swap_after_handle_binding_fails_closed_without_external_write(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
) -> None:
    root = tmp_path / "engine"
    engine = Engine(root)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="pinned",
    )
    trusted = root / "invocations"
    held = root / "held-invocations"
    trusted.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    trusted.symlink_to(outside, target_is_directory=True)

    with pytest.raises(EngineError, match="namespace"):
        engine.run_until_blocked(handle)
    assert tuple(outside.iterdir()) == ()


def test_leaf_swap_after_handle_binding_is_rejected_as_stale(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="leaf",
    )
    invocation = tmp_path / "invocations" / "leaf"
    invocation.rename(tmp_path / "invocations" / "held-leaf")
    outside = tmp_path / "outside-leaf"
    outside.mkdir()
    invocation.symlink_to(outside, target_is_directory=True)

    with pytest.raises(EngineError, match="trusted|stale"):
        engine.run_until_blocked(handle)
    assert tuple(outside.iterdir()) == ()


def test_engine_and_handle_lifecycle_is_context_managed_idempotent_and_fail_closed(
    tmp_path: Path,
    resolved_subgraph_product: ResolvedProduct,
) -> None:
    engine = Engine(tmp_path)
    with engine:
        handle = engine.start(
            resolved_subgraph_product,
            entrypoint="main",
            invocation_id="lifecycle",
        )
        with handle:
            assert engine.run_until_blocked(handle).status == "succeeded"
        handle.close()
        with pytest.raises(EngineError, match="closed"):
            _ = handle.workspace
        with pytest.raises(EngineError, match="closed"):
            engine.run_until_blocked(handle)

    engine.close()
    with pytest.raises(EngineError, match="closed"):
        engine.open("lifecycle", resolved_subgraph_product)


def test_repeated_open_close_does_not_grow_invocation_descriptors(
    tmp_path: Path,
    resolved_subgraph_product: ResolvedProduct,
) -> None:
    engine = Engine(tmp_path)
    initial = engine.start(
        resolved_subgraph_product,
        entrypoint="main",
        invocation_id="descriptor-growth",
    )
    assert engine.run_until_blocked(initial).status == "succeeded"
    initial.close()
    baseline = len(os.listdir("/dev/fd"))

    for _index in range(50):
        with engine.open("descriptor-growth", resolved_subgraph_product):
            pass

    assert len(os.listdir("/dev/fd")) <= baseline + 1
    engine.close()


def test_open_rejects_interrupt_metadata_that_differs_from_compiled_definition(
    tmp_path: Path,
    resolved_interrupt_product: ResolvedProduct,
) -> None:
    engine = Engine(tmp_path)
    handle = engine.start(
        resolved_interrupt_product,
        entrypoint="main",
        invocation_id="interrupt-metadata",
    )
    assert engine.run_until_blocked(handle).status == "interrupted"
    handle = engine.resume(handle, action="approve", payload={"reviewed": True})
    assert engine.run_until_blocked(handle).status == "succeeded"
    ledger_root = handle.invocation_root / "ledger"
    for batch in sorted(ledger_root.glob("[0-9]*.json")):
        document = json.loads(batch.read_bytes())
        changed = False
        for index, raw in enumerate(document):
            if raw["event"].get("kind") == "node_interrupted":
                event = NodeInterrupted.model_validate(raw["event"], strict=False)
                document[index] = EventEnvelope.from_event(
                    raw["seq"],
                    event.model_copy(update={"reason": "forged reason"}),
                ).model_dump(mode="json")
                changed = True
        if changed:
            batch.write_bytes(canonical_json_bytes(cast(JSONValue, document)))
            break

    with pytest.raises(EngineError, match="workflow|interrupt|reason"):
        Engine(tmp_path).open("interrupt-metadata", resolved_interrupt_product)
