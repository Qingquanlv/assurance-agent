"""README command table must stay in sync with the actual click command group.

Cross-checks two directions:
1. every documented top-level command is a real subcommand of `aa`;
2. every registered subcommand is documented in README.md.
Guards against the README drifting from the CLI surface.
"""

from pathlib import Path

from assurance_agent.cli import main

# The top-level command groups the README command reference documents.
DOCUMENTED_COMMANDS = {
    "init",
    "doctor",
    "config",
    "validate",
    "status",
    "gate",
    "state",
    "decide",
    "risk",
    "run",
    "trace",
    "report",
    "heal",
    "workflow",
    "skill",
    "eval",
    "retro",
    "improvement",
    "knowledge",
}


def _readme_text() -> str:
    return (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")


def test_documented_commands_are_registered() -> None:
    registered = set(main.commands)
    missing = DOCUMENTED_COMMANDS - registered
    assert missing == set(), f"README documents commands not registered on `aa`: {sorted(missing)}"


def test_registered_commands_are_documented() -> None:
    readme = _readme_text()
    undocumented = {name for name in main.commands if f"`aa {name}" not in readme}
    assert undocumented == set(), f"registered commands missing from README: {sorted(undocumented)}"


def test_readme_has_no_legacy_aws_command() -> None:
    readme = _readme_text()
    assert "`aws " not in readme, "README still shows a legacy `aws ` command invocation"
    assert ".aws/" not in readme, "README still references the legacy .aws/ config dir"
