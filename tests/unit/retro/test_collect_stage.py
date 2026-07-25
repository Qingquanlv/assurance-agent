from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers_aa import write_aa_config
from tests.unit.retro.archive_fixtures import make_archived_change

from assurance_agent.retro.collect_stage import RetroCollectResult, run_retro_collect


def test_run_retro_collect_no_candidates_signal_zero(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "archive").mkdir(parents=True)
    result = run_retro_collect(tmp_path, retro_id="retro-test", is_terminal=lambda _d, _c: True)
    assert result.signal_count == 0
    assert result.context is None
    # still writes a minimal context.json so graph outputs can freeze
    ctx_path = tmp_path / "qa" / "retro" / "retro-test" / "context.json"
    assert ctx_path.is_file()
    ctx = json.loads(ctx_path.read_text())
    assert ctx["signal_count"] == 0
    assert ctx["retro_id"] == "retro-test"


def test_run_retro_collect_with_signals_writes_context(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    make_archived_change(tmp_path, "CH-1", failures=[{"classification": "assertion"}])
    result = run_retro_collect(tmp_path, retro_id="retro-test", is_terminal=lambda _d, _c: True)
    assert result.signal_count > 0
    assert result.context is not None
    ctx_path = tmp_path / "qa" / "retro" / "retro-test" / "context.json"
    assert ctx_path.is_file()
    ctx = json.loads(ctx_path.read_text())
    assert ctx["signal_count"] > 0


def test_run_retro_collect_returns_dataclass(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "archive").mkdir(parents=True)
    result = run_retro_collect(tmp_path, retro_id="retro-test", is_terminal=lambda _d, _c: True)
    assert isinstance(result, RetroCollectResult)
    assert result.retro_id == "retro-test"
    assert result.retro_dir == tmp_path / "qa" / "retro" / "retro-test"


def test_run_retro_collect_zero_signals_context_populated(tmp_path: Path) -> None:
    """Candidates exist but produce zero signals — context is populated, signal_count=0."""
    write_aa_config(tmp_path)
    make_archived_change(tmp_path, "CH-1", failures=[], gate_pushbacks=0, apply_status="none")
    change_root = tmp_path / "qa" / "archive" / "CH-1"
    (change_root / "healing" / "api-apply-summary.json").unlink()
    (change_root / "events.jsonl").write_text(
        '{"type": "workflow_started", "change_id": "CH-1"}\n', encoding="utf-8"
    )
    result = run_retro_collect(tmp_path, retro_id="retro-test", is_terminal=lambda _d, _c: True)
    assert result.signal_count == 0
    assert result.context is not None  # context was built from candidates
    ctx_path = tmp_path / "qa" / "retro" / "retro-test" / "context.json"
    assert ctx_path.is_file()


def test_run_retro_collect_marks_candidates_consumed(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    make_archived_change(tmp_path, "CH-1", failures=[{"classification": "assertion"}])
    run_retro_collect(tmp_path, retro_id="retro-test", is_terminal=lambda _d, _c: True)
    state_path = tmp_path / "qa" / "retro" / "_state.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text())
    assert "CH-1" in state["consumed_changes"]


def test_run_retro_collect_write_root_receives_every_write(tmp_path: Path) -> None:
    """Graph callers read host evidence but write into their task workspace.

    A task-private tree has the coordinator files pruned and sibling change dirs
    removed, so scanning it finds no candidates at all; writes conversely must stay
    in the workspace to be frozen into a write-set.
    """
    host = tmp_path / "host"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    make_archived_change(host, "CH-1", failures=[{"classification": "assertion"}])

    result = run_retro_collect(
        host,
        retro_id="retro-test",
        write_root=workspace,
        is_terminal=lambda _d, _c: True,
    )

    assert result.signal_count > 0
    assert result.retro_dir == workspace / "qa" / "retro" / "retro-test"
    assert (workspace / "qa" / "retro" / "retro-test" / "context.json").is_file()
    assert (workspace / "qa" / "retro" / "_state.json").is_file()
    assert not (host / "qa" / "retro").exists()


def test_run_retro_collect_zero_signals_finalizes_watermark(tmp_path: Path) -> None:
    """Consumed-but-signal-free candidates must finalize the stage.

    Otherwise the watermark never advances and every later retro re-reads the same
    changes; on the graph path nothing else would ever call it, because the accept
    node only runs when there are signals.
    """
    write_aa_config(tmp_path)
    root = make_archived_change(tmp_path, "CH-1", failures=[], apply_status="none")
    (root / "healing" / "api-apply-summary.json").unlink()
    (root / "events.jsonl").write_text(
        '{"seq": 1, "type": "workflow_started", "change_id": "CH-1"}\n', encoding="utf-8"
    )

    result = run_retro_collect(tmp_path, retro_id="retro-test", is_terminal=lambda _d, _c: True)

    assert result.signal_count == 0
    state = json.loads((tmp_path / "qa" / "retro" / "_state.json").read_text())
    assert state["last_retro_id"] == "retro-test"
    assert state["consumed_changes"]["CH-1"]["terminal"] is True


def test_run_retro_collect_context_builder_exception_propagates(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    make_archived_change(tmp_path, "CH-1", failures=[{"classification": "assertion"}])

    def bad_builder(*args: object, **kwargs: object) -> None:
        raise RuntimeError("aggregation failed")

    with pytest.raises(RuntimeError, match="aggregation failed"):
        run_retro_collect(
            tmp_path,
            retro_id="retro-test",
            context_builder=bad_builder,  # type: ignore[arg-type]
            is_terminal=lambda _d, _c: True,
        )
