from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from graph_engine import ENGINE_API_VERSION
from graph_engine.graph.schema import WorkflowDef
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import (
    EnginePorts,
    PluginDescriptor,
    PluginRuntime,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
)
from graph_engine.product import PluginRequirement, ProductManifest, ResolvedProduct, resolve_product
from graph_engine.runtime.engine import Engine, EngineError
from graph_engine.runtime.events import TaskAttemptStarted, TaskLeaseAcquired
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import fold_events
from graph_engine.runtime.planner import plan_next
from graph_engine.runtime.scheduler import FakeClock


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


def _task_product(handler: TaskHandler, *, nested: bool = False) -> ResolvedProduct:
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
        {"test.empty.run": handler},
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
        handle = engine.open("crash-test", product)
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
