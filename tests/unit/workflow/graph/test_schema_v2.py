from pathlib import Path

import pytest

from assurance_agent.workflow.graph.compiler import CompileError, compile_workflow
from assurance_agent.workflow.graph.schema_v2 import (
    InterruptDef,
    NodeDef,
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


def test_node_accepts_typed_recovery_route() -> None:
    node = NodeDef.model_validate(
        {
            "uses": "skill:aa-issue-analyzer",
            "recover": {
                "errors": ["timeout", "transport", "rate_limit", "invalid_output"],
                "via": "record-analysis-failure",
                "continue_to": "inspect-complete",
            },
        }
    )

    assert node.recover is not None
    assert node.recover.errors == ["timeout", "transport", "rate_limit", "invalid_output"]


@pytest.mark.parametrize(
    "recover",
    [
        {"errors": [], "via": "record", "continue_to": "done"},
        {"errors": ["timeout", "timeout"], "via": "record", "continue_to": "done"},
        {"errors": ["unsupported"], "via": "record", "continue_to": "done"},
    ],
)
def test_node_recovery_errors_must_be_non_empty_unique_and_supported(recover: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        NodeDef.model_validate({"uses": "skill:aa-issue-analyzer", "recover": recover})


def test_interrupt_actions_accept_audited_domain_actions() -> None:
    actions = [
        "confirm_assessment",
        "mark_not_an_issue",
        "accept_risk",
        "start_work",
        "confirm_link",
        "merge",
        "reopen",
        "submit_resolution",
    ]

    interrupt = InterruptDef.model_validate(
        {
            "reason": "review required",
            "checkpoint": "case-review",
            "bind": "audited_gate_read",
            "actions": actions,
        }
    )

    assert interrupt.actions == actions


@pytest.mark.parametrize("actions", [[], ["confirm_assessment", "confirm_assessment"], ["  "]])
def test_interrupt_actions_reject_empty_duplicate_or_blank_values(actions: list[str]) -> None:
    with pytest.raises(ValueError):
        InterruptDef.model_validate(
            {
                "reason": "review required",
                "checkpoint": "case-review",
                "bind": "audited_gate_read",
                "actions": actions,
            }
        )


def test_compiler_requires_a_route_for_each_declared_domain_interrupt_action() -> None:
    schema = parse_workflow_v2(
        """
schema_version: "2"
name: domain-interrupt
entrypoints:
  full: {graph: main}
graphs:
  main:
    max_supersteps: 5
    nodes:
      review:
        uses: builtin:interrupt
        interrupt:
          reason: review required
          checkpoint: case-review
          bind: audited_gate_read
          actions: [confirm_assessment, mark_not_an_issue]
    edges:
      - {from: START, to: review}
    routes:
      - from: review
        select: "resume.action"
        cases:
          confirm_assessment: END
gates: {}
"""
    )

    with pytest.raises(CompileError, match="mark_not_an_issue.*no resume route"):
        compile_workflow(schema)
