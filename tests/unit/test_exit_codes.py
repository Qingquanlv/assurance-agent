from dataclasses import dataclass

from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    exit_code_for_gate_verdict,
    exit_code_for_terminal,
)


@dataclass
class _T:
    kind: str


def test_terminal_mapping():
    assert exit_code_for_terminal(None) == EXIT_COMPLETED
    assert exit_code_for_terminal(_T("completed")) == EXIT_COMPLETED
    assert exit_code_for_terminal(_T("stopped")) == EXIT_STOPPED
    assert exit_code_for_terminal(_T("needs_human_review")) == EXIT_HUMAN_REVIEW


def test_gate_verdict_mapping():
    for v in ("pass", "enter", "exit", "skip"):
        assert exit_code_for_gate_verdict(v) == EXIT_COMPLETED
    for v in ("needs_fix", "needs_human_review", "continue"):
        assert exit_code_for_gate_verdict(v) == EXIT_HUMAN_REVIEW
    for v in ("reject", "stop"):
        assert exit_code_for_gate_verdict(v) == EXIT_ERROR
    assert exit_code_for_gate_verdict("banana") == EXIT_ERROR  # fail-closed
