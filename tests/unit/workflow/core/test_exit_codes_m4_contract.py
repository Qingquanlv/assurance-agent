import pytest

from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    EXIT_USAGE,
    exit_code_for_gate_verdict,
    exit_code_for_terminal,
)


class FakeTerminal:
    def __init__(self, kind: str) -> None:
        self.kind = kind


def test_constant_values() -> None:
    assert (EXIT_COMPLETED, EXIT_STOPPED, EXIT_HUMAN_REVIEW, EXIT_ERROR, EXIT_USAGE) == (0, 20, 30, 40, 2)


@pytest.mark.parametrize("verdict", ["pass", "enter", "exit", "skip"])
def test_gate_verdict_zero(verdict: str) -> None:
    assert exit_code_for_gate_verdict(verdict) == EXIT_COMPLETED


@pytest.mark.parametrize("verdict", ["needs_fix", "needs_human_review", "continue"])
def test_gate_verdict_human_review(verdict: str) -> None:
    assert exit_code_for_gate_verdict(verdict) == EXIT_HUMAN_REVIEW


@pytest.mark.parametrize("verdict", ["reject", "stop"])
def test_gate_verdict_error(verdict: str) -> None:
    assert exit_code_for_gate_verdict(verdict) == EXIT_ERROR


def test_terminal_none_and_completed_zero() -> None:
    assert exit_code_for_terminal(None) == EXIT_COMPLETED
    assert exit_code_for_terminal(FakeTerminal("completed")) == EXIT_COMPLETED


def test_terminal_stopped_twenty() -> None:
    assert exit_code_for_terminal(FakeTerminal("stopped")) == EXIT_STOPPED


def test_terminal_needs_human_review_thirty() -> None:
    assert exit_code_for_terminal(FakeTerminal("needs_human_review")) == EXIT_HUMAN_REVIEW
