"""aa status：只读路径、分态引导、退出码 0/20/30/40（错误 40）。

CLI 行为用 monkeypatch read_latest_graph_status 构造 GraphStatus（沿既有测试风格）；
另有一条真实 ledger 的冒烟用例锁定只读性与 schema 无关性。
注意 Click 8.4：result.output 混合 stdout+stderr——JSON 断言用 result.stdout，
错误文案断言用 result.stderr。
"""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.commands import status_cmd
from assurance_agent.workflow.graph.models import GraphStatus, InterruptProjection
from tests.helpers_aa import write_aa_config
from tests.unit.workflow.graph.test_status_read import INV, seed_completed, tree_snapshot


def make_status(**over: object) -> GraphStatus:
    base: dict[str, object] = dict(
        invocation_id=INV, entrypoint="full", status="running", checkpoint_id="cp-1",
        event_seq=3, superstep=1, running_tasks=(), pending_tasks=(),
        pending_write_sets=(), pending_interrupts=(), next_retry_at=None,
        budgets={}, terminal_reason=None,
    )
    base.update(over)
    return GraphStatus(**base)  # type: ignore[arg-type]


@pytest.fixture
def project():
    runner = CliRunner()
    ctx = runner.isolated_filesystem()
    root = Path(ctx.__enter__())
    write_aa_config(root)
    (root / "qa/changes/CH-1").mkdir(parents=True)
    yield runner
    ctx.__exit__(None, None, None)


def patch_status(monkeypatch: pytest.MonkeyPatch, status: GraphStatus | None) -> None:
    monkeypatch.setattr(status_cmd, "read_latest_graph_status", lambda *a, **k: status)


def test_missing_change_exits_40(project) -> None:
    result = project.invoke(main, ["status", "--change", "NOPE"])
    assert result.exit_code == 40
    assert "NOPE" in result.stderr


def test_invalid_config_exits_40(project) -> None:
    """损坏的 .aa/config.yaml → ConfigInvalidError，操作性错误必须 40（未捕获异常会是 1）。"""
    Path(".aa/config.yaml").write_text("broken: [", encoding="utf-8")
    result = project.invoke(main, ["status", "--change", "CH-1"])
    assert result.exit_code == 40
    assert result.stderr.strip() != ""


def test_ledger_oserror_exits_40(project) -> None:
    """events.jsonl 不可读（此处为目录）→ OSError → 40，而非未处理异常。"""
    (Path("qa/changes/CH-1") / "events.jsonl").mkdir()
    result = project.invoke(main, ["status", "--change", "CH-1"])
    assert result.exit_code == 40
    assert "status failed" in result.stderr


def test_no_invocation_unified_payload(project) -> None:
    result = project.invoke(main, ["status", "--change", "CH-1", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": None, "invocation_id": None}


def test_running_exit_0_and_plain_resume_hint(project, monkeypatch) -> None:
    patch_status(monkeypatch, make_status())
    result = project.invoke(main, ["status", "--change", "CH-1"])
    assert result.exit_code == 0
    assert "aa workflow run" in result.stdout


def test_interrupted_exit_30_with_interrupt_detail(project, monkeypatch) -> None:
    interrupt = InterruptProjection(
        interrupt_id="int-1", checkpoint_ns="ns", node_id="human-review",
        checkpoint="case-review-gate", actions=("fix_and_proceed", "stop"),
        audited_reads_sha256={},
    )
    patch_status(monkeypatch, make_status(status="interrupted", pending_interrupts=(interrupt,)))
    result = project.invoke(main, ["status", "--change", "CH-1"])
    assert result.exit_code == 30
    assert "int-1" in result.stdout
    assert "--interrupt int-1 --action" in result.stdout


def test_failed_exit_40_without_resume_command(project, monkeypatch) -> None:
    patch_status(monkeypatch, make_status(status="failed", terminal_reason="boom"))
    result = project.invoke(main, ["status", "--change", "CH-1"])
    assert result.exit_code == 40
    assert "boom" in result.stdout
    assert "aa workflow resume" not in result.stdout  # 命令不出现；诊断句可含 resume 字样
    assert "--interrupt" not in result.stdout


def test_next_json_limits_to_pending(project, monkeypatch) -> None:
    patch_status(monkeypatch, make_status(pending_tasks=("t-1",)))
    result = project.invoke(main, ["status", "--change", "CH-1", "--next", "--json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["pending_tasks"] == ["t-1"]


def test_real_ledger_smoke_read_only_and_schema_independent(project) -> None:
    """真实 ledger：不 mock 查询路径；损坏 schema + 漂移缓存下仍正常且零写入。"""
    change = Path("qa/changes/CH-1")
    seed_completed(change)
    (change / "workflow-state.yaml").write_text("broken: [", encoding="utf-8")
    # schema 漂移/损坏：旧 build_graph_runtime 路径必挂，新只读路径不读 schema。
    Path(".aa/workflow-schema.yaml").write_text("phases: {}\n", encoding="utf-8")  # v1 键，加载即拒
    before = tree_snapshot(change)
    result = project.invoke(main, ["status", "--change", "CH-1", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "completed"
    assert tree_snapshot(change) == before
