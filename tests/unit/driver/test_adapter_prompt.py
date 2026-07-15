from assurance_agent.exceptions import AaError
from assurance_agent.workflow.driver.adapter import (
    Adapter,
    DriverError,
    PhaseRequest,
    PhaseResult,
)
from assurance_agent.workflow.driver.phase_prompt import build_phase_prompt


def test_phase_request_defaults() -> None:
    req = PhaseRequest(change_id="CH-1", phase_id="explore", prompt="do it")
    assert req.skill is None
    assert req.agent is None
    assert req.prompt == "do it"


def test_phase_result_error_optional() -> None:
    ok = PhaseResult(ok=True, output="done")
    assert ok.error is None
    bad = PhaseResult(ok=False, output="", error="boom")
    assert bad.error == "boom"


def test_driver_error_is_aa_error() -> None:
    assert issubclass(DriverError, AaError)


def test_adapter_protocol_is_runtime_checkable() -> None:
    class Ok:
        def run_phase(self, request: PhaseRequest) -> PhaseResult:
            return PhaseResult(ok=True, output="")

    class NotAdapter:
        pass

    assert isinstance(Ok(), Adapter)
    assert not isinstance(NotAdapter(), Adapter)


def test_build_phase_prompt_binds_skill_and_change() -> None:
    prompt = build_phase_prompt("aa-explore", "explore", "CH-1")
    assert "skill(name='aa-explore')" in prompt
    assert "change_id='CH-1'" in prompt
    assert "qa/changes/CH-1/" in prompt
    assert "phase explore" in prompt
    assert "Do NOT modify workflow-state.yaml" in prompt


def test_build_phase_prompt_fix_proposal_binding_only_for_that_phase() -> None:
    fix = build_phase_prompt("aa-fix-proposal", "fix-proposal", "CH-1")
    assert "source_batch_id" in fix
    assert "source_analysis_sha256" in fix
    other = build_phase_prompt("aa-explore", "explore", "CH-1")
    assert "source_analysis_sha256" not in other


def test_build_phase_prompt_has_no_legacy_aws_reference() -> None:
    prompt = build_phase_prompt("aa-api-codegen", "api-codegen", "CH-1")
    assert "aws" not in prompt.lower()
    assert "aa gate/status" in prompt
