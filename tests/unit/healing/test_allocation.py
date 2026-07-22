from pathlib import Path

from tests.helpers_aa import write_aa_config

from assurance_agent.workflow.core.events import read_events
from assurance_agent.workflow.healing.allocation import commit_healing_allocation_ledger
from assurance_agent.workflow.healing.safety import derive_guard_context


def test_commit_healing_allocation_ledger_enables_record_apply_guard(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    (change_dir / "healing").mkdir(parents=True)
    (change_dir / "healing" / "fix-proposal.json").write_text('{"proposals": []}', encoding="utf-8")
    (change_dir / "healing" / "entry-baseline.json").write_text(
        '{"schema_version":"1.0","episode_id":"ep1","entry_batch_id":"b1"}\n',
        encoding="utf-8",
    )

    commit_healing_allocation_ledger(
        change_dir,
        episode_id="ep1",
        attempt_id="ha-ep1-1",
        attempt_number=1,
        operation_id="op1",
        source_batch_id="b1",
        baseline_sha256="abc",
        entry_batch_id="b1",
    )

    types = [e["type"] for e in read_events(change_dir)]
    assert types == ["healing_entry_baseline_pinned", "healing_attempt_allocated"]

    ctx = derive_guard_context(tmp_path, "CH-1")
    assert ctx.source_batch_id == "b1"
    assert ctx.attempt_key is not None
