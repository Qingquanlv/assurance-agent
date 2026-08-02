from pathlib import Path

import pytest

from assurance_agent.workflow.graph.compiler import CompileError, compile_workflow
from assurance_agent.workflow.graph.schema_v2 import (
    InterruptDef,
    ManualRevisionDef,
    NodeDef,
    ParamDef,
    SchemaV2Error,
    load_workflow_v2,
    parse_workflow_v2,
)


def test_object_param_schema_is_supported() -> None:
    definition = ParamDef.model_validate({"type": "object", "default": {"schema_version": "1"}})
    assert definition.type == "object"
    assert definition.default == {"schema_version": "1"}


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


def test_manual_revision_accepts_exact_change_plan_paths() -> None:
    interrupt = InterruptDef.model_validate(
        {
            "reason": "fuzz plan requires a manual revision",
            "checkpoint": "fuzz-plan-gate",
            "bind": "audited_gate_read",
            "actions": ["fix_and_proceed", "accept_risk", "stop"],
            "manual_revision": {
                "action": "fix_and_proceed",
                "paths": [
                    "change:plans/fuzz-plan.md",
                    "change:plans/fuzz-codegen-plan.md",
                ],
            },
        }
    )

    assert interrupt.manual_revision is not None
    assert interrupt.manual_revision.action == "fix_and_proceed"
    assert interrupt.manual_revision.paths == [
        "change:plans/fuzz-plan.md",
        "change:plans/fuzz-codegen-plan.md",
    ]


@pytest.mark.parametrize(
    ("paths", "match"),
    [
        (
            ["change:plans/fuzz-plan.md", "change:plans/fuzz-plan.md"],
            "duplicate",
        ),
        (["change:plans/*.md"], "glob"),
        (["change:plans/fuzz-*.md"], "glob"),
        (["change:plans/${plan}.md"], "template"),
        (["change:plans/{plan}.md"], "template"),
        (["change:plans/"], "directory"),
        (["change:plans/subdir/"], "directory"),
        (["repo:plans/fuzz-plan.md"], "change:"),
        (["plans/fuzz-plan.md"], "change:"),
        (["change:review/fuzz-plan-review.json"], "plans/"),
        (["change:plans/../secrets.txt"], r"\.\.|plans/"),
    ],
)
def test_manual_revision_rejects_non_exact_plan_paths(paths: list[str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ManualRevisionDef.model_validate({"action": "fix_and_proceed", "paths": paths})


def test_manual_revision_action_must_be_declared_on_interrupt() -> None:
    with pytest.raises(ValueError, match="manual_revision.action"):
        InterruptDef.model_validate(
            {
                "reason": "fuzz plan requires a manual revision",
                "checkpoint": "fuzz-plan-gate",
                "bind": "audited_gate_read",
                "actions": ["accept_risk", "stop"],
                "manual_revision": {
                    "action": "fix_and_proceed",
                    "paths": ["change:plans/fuzz-plan.md"],
                },
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
