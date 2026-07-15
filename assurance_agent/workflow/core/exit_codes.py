"""CLI/driver 退出码常量与映射（数值对齐源版 CliExitCodes）。

completed/running=0 / stopped=20 / humanReview=30 / command-or-data-error=40。
"""
from __future__ import annotations

from typing import Protocol

EXIT_COMPLETED = 0
EXIT_STOPPED = 20
EXIT_HUMAN_REVIEW = 30
EXIT_ERROR = 40
EXIT_USAGE = 2


class TerminalLike(Protocol):
    @property
    def kind(self) -> str: ...


def exit_code_for_gate_verdict(verdict: str) -> int:
    if verdict in ("pass", "enter", "exit", "skip"):
        return EXIT_COMPLETED
    if verdict in ("needs_fix", "needs_human_review", "continue"):
        return EXIT_HUMAN_REVIEW
    # reject / stop / 未知 → fail-closed
    return EXIT_ERROR


def exit_code_for_terminal(terminal: TerminalLike | None) -> int:
    if terminal is None:
        return EXIT_COMPLETED
    if terminal.kind == "completed":
        return EXIT_COMPLETED
    if terminal.kind == "stopped":
        return EXIT_STOPPED
    if terminal.kind == "needs_human_review":
        return EXIT_HUMAN_REVIEW
    return EXIT_COMPLETED
