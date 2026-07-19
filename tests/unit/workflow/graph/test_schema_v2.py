from pathlib import Path

import pytest

from assurance_agent.workflow.graph.schema_v2 import (
    SchemaV2Error,
    load_workflow_v2,
    parse_workflow_v2,
)


def test_minimal_v2_schema_loads() -> None:
    schema = parse_workflow_v2(Path("tests/fixtures/workflow-v2-minimal.yaml").read_text(encoding="utf-8"))
    assert schema.schema_version == "2"
    assert schema.entrypoints["full"].graph == "main"
    assert list(schema.graphs["main"].nodes) == ["first"]
    assert schema.policies.retry["never"].max_attempts == 1


@pytest.mark.parametrize("version", ["1", "2.0", 2, ""])
def test_only_exact_string_version_two_is_accepted(version: object) -> None:
    text = f"schema_version: {version!r}\nname: bad\nentrypoints: {{}}\ngraphs: {{}}\n"
    with pytest.raises(SchemaV2Error, match='schema_version must be exactly "2"'):
        parse_workflow_v2(text)


def test_v1_phase_and_loop_keys_are_rejected() -> None:
    with pytest.raises(SchemaV2Error, match="phases|loops"):
        parse_workflow_v2(
            'schema_version: "2"\nname: bad\nphases: []\nloops: {}\nentrypoints: {}\ngraphs: {}\n'
        )


def test_load_workflow_v2_explicit_path_wins(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/workflow-v2-minimal.yaml").read_text(encoding="utf-8")
    project = tmp_path / "project"
    (project / ".aa").mkdir(parents=True)
    (project / ".aa" / "workflow-schema.yaml").write_text(
        fixture.replace("name: minimal", "name: aa-dir"), encoding="utf-8"
    )
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(fixture.replace("name: minimal", "name: explicit"), encoding="utf-8")

    schema = load_workflow_v2(project, explicit)

    assert schema.name == "explicit"
