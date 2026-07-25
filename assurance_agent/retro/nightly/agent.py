from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def build_retro_proposal_prompt(retro_id: str) -> str:
    """Prompt for the aa-retro proposal agent, in the `build_phase_prompt`
    `skill(name=...)` convention (see `workflow/driver/phase_prompt.py`).
    """
    return (
        "Call skill(name='aa-retro'). "
        f"Read qa/retro/{retro_id}/context.json. "
        f"Write qa/retro/{retro_id}/proposals.json and qa/retro/{retro_id}/retro-summary.md. "
        "Every proposal MUST include machine fields finding_kind, apply_kind, and a structured "
        "payload (prompt_rule→payload.body; workflow_bug→IssueDraftPayload; "
        "domain_knowledge→L2 delta with mode:delta). Natural-language problem/proposed_change "
        "are not enough by themselves. "
        "Do not modify SKILL.md files, the workflow schema, .aa/memory, or project source files."
    )


def run_agent(agent_cmd: str, retro_dir: Path) -> int:
    """Invoke external proposal agent (writes proposals.json / retro-summary.md).

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
