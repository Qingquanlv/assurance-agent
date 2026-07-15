from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def run_agent(agent_cmd: str, retro_dir: Path) -> int:
    """Invoke external proposal agent (writes proposals.json / retro-summary.md)."""
    argv = shlex.split(agent_cmd)
    try:
        completed = subprocess.run(argv, cwd=str(retro_dir.parent.parent.parent), check=False)
    except (OSError, ValueError):
        return 40
    return completed.returncode
