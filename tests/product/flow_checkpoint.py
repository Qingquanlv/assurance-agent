"""Crash and resume the real full flow at the same moments the handwritten cuts used.

``after_coverage_advance`` pauses the parent after ``coverage-rework``.
``after_inspect_commit`` pauses the tail after ``quality``.
``after_application_commit`` pauses the tail after ``repair`` commits the applied repair.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphInterrupt

from assurance_product.graphs.full import build_full_flow
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.testing.graph_harness import GraphHarness, committed

from tests.product.test_full_flow import (
    _SHA,
    _RECEIPT,
    _bundles,
    _case,
    _extend,
    _front,
    _input,
    _materialize,
    _one_repair,
    _round_execution,
    _round_generation,
    _inspect_commit,
    _round_inspect,
)
from tests.product.test_product_stategraph_flow import _report

CrashPoint = Literal["after_coverage_advance", "after_inspect_commit", "after_application_commit"]

_CHECKPOINT = "goal-loop-checkpoints.sqlite"
_EVENTS = "flow-events.jsonl"
_CURSOR = "script-cursor.json"
_STATE = "flow-state.json"


@dataclass(frozen=True, slots=True)
class FlowCheckpointRun:
    state: dict[str, object]
    node_visits: tuple[str, ...]
    task_dispatches: tuple[str, ...]
    coverage_epochs: tuple[int, ...]
    attempt_keys: dict[str, tuple[str, ...]]
    artifact_paths: tuple[Path, ...]

    def dispatch_count(self, semantic_id: str) -> int:
        return self.task_dispatches.count(semantic_id)


def _scenario_script() -> dict[str, list[object]]:
    script: dict[str, list[object]] = {}
    _front(script)
    _case(script, "pass", "pass", epochs=(0, 1))
    _one_repair(script, 0, "coverage_insufficient")
    for key, values in _round_generation(1).items():
        _extend(script, key, *values)
    _extend(
        script,
        "quality.fact-baseline",
        committed(
            {"fact_baseline_ref": {"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/facts/fact-baseline.json", "digest": _SHA}],
        ),
    )
    _extend(
        script,
        "execution.execute",
        committed(
            _round_execution(1),
            _RECEIPT,
            artifacts=[{"path": "qa/results/execution/execution-cycle.json", "digest": _SHA}],
        ),
    )
    _extend(script, "quality.materialize-assessment-inputs", _materialize())
    _extend(script, "quality.inspect", _inspect_commit(_round_inspect("satisfied", 1)))
    script["intake.coverage-rework"] = [
        committed(
            {"rework_ref": {"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}},
            _RECEIPT,
            artifacts=[{"path": "qa/results/cases/case-rework-context.json", "digest": _SHA}],
        )
    ]
    report = _report(1)
    script["quality.report"] = [
        committed(
            {
                "publication": "reported",
                "report_outcome": report["report_outcome"],
                "report_refs": report["report_refs"],
                "coverage_state": "satisfied",
            },
            _RECEIPT,
        )
    ]
    return script


def _config() -> RunnableConfig:
    return {
        "recursion_limit": 8192,
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "full",
        },
    }


def _nested(compiled: object, name: str) -> Any:
    fn = compiled.nodes[name].bound.afunc  # type: ignore[attr-defined]
    for cell in fn.__closure__ or ():
        obj = cell.cell_contents
        if type(obj).__name__ == "CompiledStateGraph":
            return obj
    raise RuntimeError(name)


def _arm(compiled: object, cut: CrashPoint | None) -> None:
    if cut == "after_coverage_advance":
        compiled.interrupt_after_nodes = ["coverage-rework"]  # type: ignore[attr-defined]
        return
    tail = _nested(compiled, "tail")
    if cut == "after_inspect_commit":
        tail.interrupt_after_nodes = ["quality"]
    elif cut == "after_application_commit":
        tail.interrupt_after_nodes = ["repair"]


def _label(semantic_id: str) -> str | None:
    return {
        "intake.intake": "prepare",
        "intake.case-design": "case",
        "improvement.retro": "retro",
        "improvement.retro-build-slices": "retro",
    }.get(semantic_id)


def _load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _append_event(root: Path, *, semantic_id: str, attempt_key: str, coverage_epoch: object) -> None:
    label = _label(semantic_id)
    line = json.dumps(
        {
            "semantic_id": semantic_id,
            "attempt_key": attempt_key,
            "coverage_epoch": coverage_epoch,
            "label": label,
        },
        sort_keys=True,
    )
    with (root / _EVENTS).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def observe(root: Path) -> FlowCheckpointRun:
    events = []
    path = root / _EVENTS
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                events.append(json.loads(line))
    dispatches = tuple(str(item["semantic_id"]) for item in events)
    keys: dict[str, list[str]] = {}
    epochs: list[int] = []
    labels: list[str] = []
    for item in events:
        semantic = str(item["semantic_id"])
        keys.setdefault(semantic, []).append(str(item["attempt_key"]))
        if semantic == "intake.case-design" and isinstance(item.get("coverage_epoch"), int):
            epochs.append(item["coverage_epoch"])
        if isinstance(item.get("label"), str):
            labels.append(item["label"])
    state = _load_json(root / _STATE, {})
    if not isinstance(state, dict):
        state = {}
    return FlowCheckpointRun(
        state={str(key): value for key, value in state.items()},
        node_visits=tuple(labels),
        task_dispatches=dispatches,
        coverage_epochs=tuple(epochs),
        attempt_keys={name: tuple(values) for name, values in keys.items()},
        artifact_paths=tuple(sorted(item for item in root.rglob("*") if item.is_file())),
    )


async def _drive(root: Path, *, cut: CrashPoint | None, resume: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not resume:
        for name in (_EVENTS, _CURSOR, _STATE, _CHECKPOINT, f"{_CHECKPOINT}-shm", f"{_CHECKPOINT}-wal"):
            path = root / name
            if path.exists():
                path.unlink()
    cursor = _load_json(root / _CURSOR, {})
    if not isinstance(cursor, dict):
        cursor = {}
    script = _scenario_script()
    for key, count in cursor.items():
        if isinstance(count, int) and key in script:
            del script[key][:count]
    seen = {str(key): int(value) for key, value in cursor.items() if isinstance(value, int)}
    harness = GraphHarness()
    async with AsyncSqliteSaver.from_conn_string(str(root / _CHECKPOINT)) as saver:
        compiled = build_full_flow(_bundles(harness)).compile(
            EngineGraphBuildContext(contracts={}, checkpointer=saver, approved_source_roots=())
        )
        harness._kernel.load_script(cast(Any, script))
        original = harness._kernel.execute_or_recover

        async def spy(attempt_key: Any, contract: Any, validated_input: Any, context: Any) -> Any:
            semantic_id = str(getattr(context, "semantic_node_id"))
            seen[semantic_id] = seen.get(semantic_id, 0) + 1
            (root / _CURSOR).write_text(json.dumps(seen, sort_keys=True) + "\n", encoding="utf-8")
            _append_event(
                root,
                semantic_id=semantic_id,
                attempt_key=str(getattr(attempt_key, "digest", attempt_key)),
                coverage_epoch=getattr(validated_input, "coverage_epoch", None),
            )
            return await original(attempt_key, contract, validated_input, context)

        harness._kernel.execute_or_recover = spy  # type: ignore[method-assign]
        if not resume:
            _arm(compiled, cut)
        config = _config()
        pending: object = (
            None
            if resume
            else _input(
                budgets={
                    "review_rounds": 2,
                    "coverage_rounds": 1,
                    "healing_rounds": 1,
                    "execution_retries": 1,
                }
            )
        )
        try:
            await compiled.ainvoke(pending, config=config)
        except GraphInterrupt:
            pass
        snapshot = await compiled.aget_state(config)
        assert not snapshot.interrupts, "automatic repair unexpectedly required a human decision"
        values = getattr(snapshot, "values", {}) if snapshot is not None else {}
        if not isinstance(values, Mapping):
            values = {}
        (root / _STATE).write_text(
            json.dumps(dict(values), sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )


def run_flow_checkpoint(root: Path, *, cut: CrashPoint | None = None) -> FlowCheckpointRun:
    asyncio.run(_drive(root, cut=cut, resume=False))
    return observe(root)


def resume_flow_checkpoint(root: Path) -> FlowCheckpointRun:
    process = subprocess.run(
        [sys.executable, "-m", "tests.product.flow_checkpoint", "--root", str(root), "--resume"],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr)
    return observe(root)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != "--root" or args[2] != "--resume":
        raise SystemExit("usage: python -m tests.product.flow_checkpoint --root PATH --resume")
    asyncio.run(_drive(Path(args[1]), cut=None, resume=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
