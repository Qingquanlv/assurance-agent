from __future__ import annotations

from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.retro_batch import (
    RetroPipelineFailure,
    RetroRunStatus,
)
from assurance_agent.retro.supervisor import (
    RetroInvocation,
    run_retro_supervised,
)


class _StructuredFailure(Exception):
    error_kind = "forbidden_write"


def _invocation(root: Path, retro_id: str = "retro-1") -> RetroInvocation:
    return RetroInvocation(
        project_root=root,
        shell_change_id=f"RETRO-RUN-{retro_id}",
        retro_id=retro_id,
        params={"retro_id": retro_id, "change_ids": ["CH-1"]},
    )


def test_structured_runner_failure_materializes_fallback_independent_of_message(
    tmp_path: Path,
) -> None:
    candidate_ids = []
    for index, message in enumerate(("old wording", "completely changed wording")):
        root = tmp_path / str(index)
        root.mkdir()

        def fail(_invocation, message=message):
            raise _StructuredFailure(message)

        result = run_retro_supervised(_invocation(root), graph_runner=fail)
        assert result.result == "completed_with_gaps"
        proposal = root / "qa/retro/retro-1/proposal-candidates.json"
        import json

        candidate_ids.append(json.loads(proposal.read_text())["candidates"][0]["candidate_id"])
    assert candidate_ids[0] == candidate_ids[1]


def test_preflight_failure_uses_same_fallback_path(tmp_path: Path) -> None:
    failure = RetroPipelineFailure.model_validate(
        {
            "failure_id": "FAIL-PREFLIGHT",
            "retro_id": "retro-1",
            "batch_id": "batch-1",
            "stage": "batch_contract",
            "error_kind": "batch_scope_invalid",
            "message_fingerprint": "sha256:manifest",
            "occurred_at": "2026-07-28T00:00:00Z",
        }
    )
    called = False

    def should_not_run(_invocation):
        nonlocal called
        called = True

    result = run_retro_supervised(
        _invocation(tmp_path), graph_runner=should_not_run, preflight_failure=failure
    )
    assert not called
    assert result.status is not None
    assert result.status.failure_ids == ("FAIL-PREFLIGHT",)


def test_final_status_is_not_rewritten_by_post_retro_failure(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa/retro/retro-1"
    retro_dir.mkdir(parents=True)
    status = RetroRunStatus(retro_id="retro-1", result="completed")
    status_path = retro_dir / "retro-status.json"
    status_path.write_bytes(canonical_json_bytes(status))
    before = status_path.read_bytes()

    def fail_after_status(_invocation):
        raise _StructuredFailure("auto-review selector failed")

    result = run_retro_supervised(_invocation(tmp_path), graph_runner=fail_after_status)

    assert result.result == "completed"
    assert status_path.read_bytes() == before
    assert not (retro_dir / "pipeline-failure.json").exists()
