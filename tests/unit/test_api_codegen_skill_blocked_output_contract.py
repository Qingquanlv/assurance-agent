"""Executable blocked-gate contract embedded in the API codegen skill."""

import json
import re

from assurance_agent import resources


def test_api_codegen_blocked_gate_emits_empty_manifest_evidence() -> None:
    """Catch STOP guidance that lets agents omit mandatory node evidence."""
    skill = resources.read_text("skills", "aa-api-codegen", "SKILL.md")
    match = re.search(
        r"### Blocked gate output\s+(.*?)\s+```json\s+(.*?)\s+```",
        skill,
        re.DOTALL,
    )

    assert match is not None, "aa-api-codegen must define blocked gate output"
    guidance, example = match.groups()
    manifest = json.loads(example)

    assert "api-codegen-summary.md" in guidance
    assert "must not modify" in guidance.lower()
    assert manifest == {
        "schema_version": "1",
        "change_id": "<change-id>",
        "layer": "api",
        "files": [],
    }
