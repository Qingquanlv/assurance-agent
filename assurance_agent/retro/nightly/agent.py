from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def build_retro_proposal_prompt(retro_id: str) -> str:
    """Prompt for the aa-retro candidate agent, in the `build_phase_prompt`
    `skill(name=...)` convention (see `workflow/driver/phase_prompt.py`).
    """
    return (
        "Call skill(name='aa-retro'). "
        f"Read only qa/retro/{retro_id}/context.json. "
        f"Write qa/retro/{retro_id}/proposal-candidates.json and qa/retro/{retro_id}/retro-summary.md. "
        "Set schema_version='2' and pin context_sha256. Do not read any other Retro run, "
        "qa/issues, raw archive, qa/improvements, memory, data knowledge, or project source files."
    )


def run_agent(agent_cmd: str, retro_dir: Path) -> int:
    """Invoke external candidate agent (writes proposal-candidates.json / retro-summary.md).

    `agent_cmd` is the agent binary + flags only (no prompt) — e.g.
    `cursor-agent --print --output-format stream-json --workspace <dir> --trust`.
    The proposal prompt is appended here as the trailing positional argument;
    without it, `--print` mode agents block waiting for a prompt that never
    arrives (observed hang: process alive, zero CPU progress, empty output).
    """
    argv = shlex.split(agent_cmd)
    argv.append(build_retro_proposal_prompt(retro_dir.name))
    try:
        completed = subprocess.run(argv, cwd=str(retro_dir.parent.parent.parent), check=False)
    except (OSError, ValueError):
        return 40
    return completed.returncode
