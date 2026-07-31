import textwrap
from pathlib import Path

import pytest
import yaml

from assurance_agent.workflow.graph.compiler import (
    CompileError,
    compile_workflow,
    resolve_params,
)
from assurance_agent.workflow.graph.contracts import ExecutionContract, ExecutionContractCatalog
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2

_DEFAULT_HEADER = """\
params:
  run_mode: {type: enum, values: [full], default: full}
  max_fix: {type: int, default: 3}
entrypoints:
  full: {graph: main}
policies:
  retry: {never: {max_attempts: 1}}
  timeout: {local: {run_seconds: 60, heartbeat_seconds: 10}}
"""


def compile_text(text: str):
    return compile_workflow(parse_workflow_v2(text))


def _wf(graph_body: str, *, header: str = _DEFAULT_HEADER, footer: str = "gates: {}\n") -> str:
    return (
        'schema_version: "2"\nname: t\n'
        + header
        + "graphs:\n"
        + textwrap.indent(textwrap.dedent(graph_body), "  ")
        + footer
    )


def _mutate_fixture(mutation) -> str:
    raw = yaml.safe_load(Path("tests/fixtures/workflow-v2-minimal.yaml").read_text(encoding="utf-8"))
    mutation(raw)
    return yaml.safe_dump(raw, sort_keys=False)


def test_compiles_minimal_fixture_with_stable_digest() -> None:
    text = Path("tests/fixtures/workflow-v2-minimal.yaml").read_text(encoding="utf-8")
    first = compile_text(text)
    second = compile_text(text)
    assert first.digest == second.digest
    assert first.entrypoints["full"].graph_id == "main"
    assert first.graphs["main"].declaration_order == ("first",)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d["entrypoints"].update({"bad": {"graph": "missing"}}), "unknown graph"),
        (lambda d: d["graphs"]["main"]["edges"].append({"from": "missing", "to": "END"}), "unknown node"),
        (lambda d: d["graphs"]["main"]["nodes"].update({"dead": {"uses": "operation:no-op"}}), "unreachable"),
    ],
)
def test_rejects_invalid_references(mutation, message: str) -> None:
    raw = yaml.safe_load(Path("tests/fixtures/workflow-v2-minimal.yaml").read_text())
    mutation(raw)
    with pytest.raises(CompileError, match=message):
        compile_text(yaml.safe_dump(raw, sort_keys=False))


def test_compiles_bounded_cycle_with_budget() -> None:
    compiled = compile_text(
        _wf(
            """
            main:
              max_supersteps: 10
              budgets:
                fix_attempts: {limit: "params.max_fix"}
              nodes:
                review: {uses: operation:review}
                fix:
                  uses: operation:fix
                  budget: {consume: fix_attempts, "on": committed, exhausted_to: done}
                done: {uses: operation:finish}
              edges:
                - {from: START, to: review}
                - {from: review, to: fix}
                - {from: fix, to: review}
                - {from: done, to: END}
            """
        )
    )
    graph = compiled.graphs["main"]
    assert ("review", "fix") in graph.sccs
    assert graph.nodes["review"].topology_rank < graph.nodes["done"].topology_rank
    assert graph.nodes["fix"].topology_rank < graph.nodes["done"].topology_rank


def test_rejects_subgraph_recursion() -> None:
    with pytest.raises(CompileError, match="recursion"):
        compile_text(
            _wf(
                """
                a:
                  max_supersteps: 5
                  nodes:
                    call-b: {uses: graph:b}
                  edges:
                    - {from: START, to: call-b}
                    - {from: call-b, to: END}
                b:
                  max_supersteps: 5
                  nodes:
                    call-a: {uses: graph:a}
                  edges:
                    - {from: START, to: call-a}
                    - {from: call-a, to: END}
                """,
                header=_DEFAULT_HEADER.replace("full: {graph: main}", "full: {graph: a}"),
            )
        )


def test_rejects_cycle_without_finite_budget_consumer() -> None:
    with pytest.raises(CompileError, match="no bounded budget consumer"):
        compile_text(
            _wf(
                """
                main:
                  max_supersteps: 5
                  nodes:
                    a: {uses: operation:a}
                    b: {uses: operation:b}
                  edges:
                    - {from: START, to: a}
                    - {from: a, to: b}
                    - {from: b, to: a}
                    - {from: a, to: END}
                """
            )
        )


def test_rejects_cycle_when_exhausted_to_stays_inside() -> None:
    with pytest.raises(CompileError, match="exhausted_to"):
        compile_text(
            _wf(
                """
                main:
                  max_supersteps: 10
                  budgets:
                    fix_attempts: {limit: 3}
                  nodes:
                    review: {uses: operation:review}
                    fix:
                      uses: operation:fix
                      budget: {consume: fix_attempts, "on": committed, exhausted_to: review}
                  edges:
                    - {from: START, to: review}
                    - {from: review, to: fix}
                    - {from: fix, to: review}
                    - {from: fix, to: END}
                """
            )
        )


def test_rejects_non_exhaustive_interrupt_action_route() -> None:
    with pytest.raises(CompileError, match="no resume route"):
        compile_text(
            _wf(
                """
                main:
                  max_supersteps: 5
                  budgets:
                    fix_attempts: {limit: 3}
                  nodes:
                    work:
                      uses: operation:work
                      budget: {consume: fix_attempts, "on": committed, exhausted_to: END}
                    human:
                      uses: builtin:interrupt
                      interrupt:
                        reason: needs a human
                        checkpoint: c1
                        bind: audited_gate_read
                        actions: [fix_and_proceed, accept_risk, stop]
                  edges:
                    - {from: START, to: work}
                    - {from: work, to: human}
                  routes:
                    - from: human
                      select: "resume.action"
                      cases:
                        fix_and_proceed: work
                        stop: STOP
                      default: STOP
                """
            )
        )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("retry: nope", "unknown retry policy"),
        ("timeout: nope", "unknown timeout policy"),
    ],
)
def test_rejects_bad_policy_refs(field: str, message: str) -> None:
    body = """
        main:
          max_supersteps: 5
          nodes:
            a:
              uses: operation:a
              FIELD
          edges:
            - {from: START, to: a}
            - {from: a, to: END}
        """.replace("FIELD", field)
    with pytest.raises(CompileError, match=message):
        compile_text(_wf(body))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d["graphs"]["main"]["nodes"]["first"].update({"gate": "nope"}), "unknown gate"),
        (
            lambda d: d["graphs"]["main"]["edges"].append(
                {"from": "first", "to": "END", "when": "gate('nope').verdict == 'pass'"}
            ),
            "unknown gate",
        ),
    ],
)
def test_rejects_bad_gate_refs(mutation, message: str) -> None:
    with pytest.raises(CompileError, match=message):
        compile_text(_mutate_fixture(mutation))


def test_rejects_replace_multi_writer() -> None:
    with pytest.raises(CompileError, match="replace"):
        compile_text(
            _wf(
                """
                main:
                  max_supersteps: 5
                  state:
                    summary: {type: object, default: {}, reducer: replace}
                  nodes:
                    a: {uses: operation:a, state_writes: {summary: "result.a"}}
                    b: {uses: operation:b, state_writes: {summary: "result.b"}}
                  edges:
                    - {from: START, to: a}
                    - {from: START, to: b}
                    - {from: a, to: END}
                    - {from: b, to: END}
                """
            )
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda d: d["graphs"]["main"]["nodes"].update({"bad id": {"uses": "operation:x"}}),
            "unsafe node id",
        ),
        (lambda d: d["graphs"]["main"]["nodes"].update({"END": {"uses": "operation:x"}}), "reserved"),
        (
            lambda d: d["graphs"]["main"]["nodes"]["first"].update({"outputs": ["change:../escape.json"]}),
            "unsafe path",
        ),
        (
            lambda d: d["graphs"]["main"]["nodes"]["first"].update({"outputs": ["plain.json"]}),
            "rooted",
        ),
    ],
)
def test_rejects_unsafe_identifiers(mutation, message: str) -> None:
    with pytest.raises(CompileError, match=message):
        compile_text(_mutate_fixture(mutation))


def test_rejects_unknown_node_in_expression() -> None:
    def mutation(raw) -> None:
        raw["graphs"]["main"]["nodes"]["first"]["when"] = "node('ghost').value == true"

    with pytest.raises(CompileError, match="unknown node"):
        compile_text(_mutate_fixture(mutation))


def test_named_gate_owned_only_by_builtin_gate_can_read_graph_nodes() -> None:
    compiled = compile_text(
        _wf(
            """
            main:
              max_supersteps: 5
              nodes:
                producer: {uses: operation:produce}
                check:
                  uses: builtin:gate
                  with: {gate: producer-gate}
              edges:
                - {from: START, to: producer}
                - {from: producer, to: check}
                - {from: check, to: END}
            """,
            footer="""
gates:
  producer-gate:
    stop_when: "node('producer').status == 'failed'"
    pass_when: "true"
""",
        )
    )

    assert compiled.schema.gates["producer-gate"].id == "producer-gate"


def test_gate_shared_by_attached_and_builtin_owners_requires_a_common_node() -> None:
    with pytest.raises(CompileError, match="unknown node.*producer-only"):
        compile_text(
            _wf(
                """
                main:
                  max_supersteps: 5
                  nodes:
                    producer-only: {uses: operation:produce}
                    attached: {uses: operation:review, gate: shared-gate}
                  edges:
                    - {from: START, to: producer-only}
                    - {from: producer-only, to: attached}
                    - {from: attached, to: END}
                builtin-owner:
                  max_supersteps: 5
                  nodes:
                    check:
                      uses: builtin:gate
                      with: {gate: shared-gate}
                  edges:
                    - {from: START, to: check}
                    - {from: check, to: END}
                """,
                footer="""
gates:
  shared-gate:
    stop_when: "node('producer-only').status == 'failed'"
    pass_when: "true"
""",
            )
        )


def test_rejects_duplicate_artifact_symbols() -> None:
    with pytest.raises(CompileError, match="duplicate artifact symbol"):
        compile_text(
            _wf(
                """
                main:
                  max_supersteps: 5
                  nodes:
                    a: {uses: operation:a, outputs: [change:x/result.json]}
                    b: {uses: operation:b, outputs: [change:y/result.json]}
                  edges:
                    - {from: START, to: a}
                    - {from: START, to: b}
                    - {from: a, to: END}
                    - {from: b, to: END}
                """
            )
        )


def test_artifact_symbols_derive_from_json_output_stems() -> None:
    compiled = compile_text(
        _wf(
            """
            main:
              max_supersteps: 5
              nodes:
                explore:
                  uses: operation:explore
                  outputs: [change:explore/advisory.json, change:explore/notes.md]
                report: {uses: operation:report}
              edges:
                - {from: START, to: explore}
                - {from: explore, to: report}
                - {from: report, to: END}
            """
        )
    )
    assert compiled.graphs["main"].artifact_symbols == {"advisory": "change:explore/advisory.json"}


def test_resolve_params_validates_overrides_and_cross_constraints() -> None:
    schema = parse_workflow_v2(
        _wf(
            """
            main:
              max_supersteps: 5
              nodes:
                a: {uses: operation:a}
              edges:
                - {from: START, to: a}
                - {from: a, to: END}
            """,
            header=(
                "params:\n"
                "  run_mode: {type: enum, values: [full, api-only, e2e-only], default: full}\n"
                "  test_types: {type: list, values: [api, e2e], min_items: 1, unique: true, default: [api]}\n"
                "entrypoints:\n"
                "  full: {graph: main}\n"
            ),
        )
    )
    with pytest.raises(CompileError, match="unknown params"):
        resolve_params(schema, {"nope": 1})
    with pytest.raises(CompileError, match="api-only requires"):
        resolve_params(schema, {"run_mode": "api-only", "test_types": ["e2e"]})
    resolved = resolve_params(schema, {"run_mode": "api-only", "test_types": ["api"]})
    assert resolved["run_mode"] == "api-only"


def test_resolve_params_validates_object_defaults_and_overrides() -> None:
    schema = parse_workflow_v2(
        _wf(
            """
            main:
              max_supersteps: 5
              nodes:
                a: {uses: operation:a}
              edges:
                - {from: START, to: a}
                - {from: a, to: END}
            """,
            header=(
                "params:\n"
                '  batch_scope: {type: object, default: {schema_version: "1"}}\n'
                "entrypoints:\n"
                "  full: {graph: main}\n"
            ),
        )
    )

    assert resolve_params(schema, {})["batch_scope"] == {"schema_version": "1"}
    assert resolve_params(schema, {"batch_scope": {"batch_id": "B-1"}})["batch_scope"] == {"batch_id": "B-1"}
    for invalid in ([], "not-an-object", 1, {"bad": object()}):
        with pytest.raises(CompileError, match="object|JSON"):
            resolve_params(schema, {"batch_scope": invalid})


_RECOVERY_GRAPH = """
main:
  max_supersteps: 5
  nodes:
    inspect:
      uses: skill:aa-issue-analyzer
      recover:
        errors: [timeout, transport, rate_limit, invalid_output]
        via: record-analysis-failure
        continue_to: inspect-complete
    record-analysis-failure: {uses: skill:aa-issue-analyzer}
    inspect-complete: {uses: skill:aa-issue-analyzer}
  edges:
    - {from: START, to: inspect}
    - {from: inspect-complete, to: END}
"""


def test_recovery_topology_makes_fallback_and_continuation_reachable() -> None:
    compiled = compile_text(_wf(_RECOVERY_GRAPH))

    assert compiled.graphs["main"].nodes["inspect"].definition.recover is not None


@pytest.mark.parametrize(
    ("recovery", "message"),
    [
        (
            {"errors": ["timeout"], "via": "missing", "continue_to": "inspect-complete"},
            "recovery via unknown node",
        ),
        (
            {"errors": ["timeout"], "via": "missing", "continue_to": "STOP"},
            "recovery via unknown node",
        ),
        (
            {"errors": ["timeout"], "via": "record-analysis-failure", "continue_to": "missing"},
            "recovery continue_to unknown node",
        ),
        (
            {"errors": ["timeout"], "via": "inspect", "continue_to": "inspect-complete"},
            "recovery via must differ",
        ),
    ],
)
def test_recovery_references_are_validated(recovery: dict[str, object], message: str) -> None:
    text = _wf(_RECOVERY_GRAPH)
    raw = yaml.safe_load(text)
    raw["graphs"]["main"]["nodes"]["inspect"]["recover"] = recovery

    with pytest.raises(CompileError, match=message):
        compile_text(yaml.safe_dump(raw, sort_keys=False))


@pytest.mark.parametrize(
    ("edge", "message"),
    [
        ({"from": "START", "to": "record-analysis-failure"}, "ordinary incoming"),
        ({"from": "record-analysis-failure", "to": "inspect-complete"}, "ordinary outgoing"),
    ],
)
def test_recovery_node_cannot_have_ordinary_edges(edge: dict[str, str], message: str) -> None:
    text = _wf(_RECOVERY_GRAPH)
    raw = yaml.safe_load(text)
    raw["graphs"]["main"]["edges"].append(edge)

    with pytest.raises(CompileError, match=message):
        compile_text(yaml.safe_dump(raw, sort_keys=False))


def test_recovery_node_cannot_have_ordinary_routes() -> None:
    text = _wf(_RECOVERY_GRAPH)
    raw = yaml.safe_load(text)
    raw["graphs"]["main"]["routes"] = [
        {
            "from": "record-analysis-failure",
            "select": "state.next",
            "cases": {"complete": "inspect-complete"},
        }
    ]

    with pytest.raises(CompileError, match="ordinary outgoing"):
        compile_text(yaml.safe_dump(raw, sort_keys=False))


def test_ordinary_route_cannot_target_recovery_node() -> None:
    text = _wf(_RECOVERY_GRAPH)
    raw = yaml.safe_load(text)
    raw["graphs"]["main"]["routes"] = [
        {
            "from": "inspect",
            "select": "state.next",
            "cases": {"recover": "record-analysis-failure"},
        }
    ]

    with pytest.raises(CompileError, match="ordinary incoming"):
        compile_text(yaml.safe_dump(raw, sort_keys=False))


@pytest.mark.parametrize("error", ["forbidden_write", "contract", "internal"])
def test_recovery_errors_need_not_be_retryable_by_the_execution_contract(error: str) -> None:
    catalog = ExecutionContractCatalog(
        contracts={
            "skill:aa-issue-analyzer": ExecutionContract(
                target="skill:aa-issue-analyzer",
                handler="agent",
                retryable_errors=("timeout", "transport", "rate_limit", "invalid_output"),
                side_effect_free=True,
            )
        }
    )
    text = _wf(_RECOVERY_GRAPH)
    raw = yaml.safe_load(text)
    raw["graphs"]["main"]["nodes"]["inspect"]["recover"]["errors"] = [error]

    compile_workflow(parse_workflow_v2(yaml.safe_dump(raw, sort_keys=False)), catalog)


def test_subgraph_recovery_has_stable_digest() -> None:
    text = _wf(
        """
        main:
          max_supersteps: 5
          nodes:
            call-inspection: {uses: graph:inspection}
          edges:
            - {from: START, to: call-inspection}
            - {from: call-inspection, to: END}
        inspection:
          max_supersteps: 5
          nodes:
            inspect:
              uses: operation:inspect
              recover:
                errors: [timeout]
                via: record-analysis-failure
                continue_to: inspect-complete
            record-analysis-failure: {uses: operation:record}
            inspect-complete: {uses: operation:complete}
          edges:
            - {from: START, to: inspect}
            - {from: inspect-complete, to: END}
        """
    )

    assert compile_text(text).digest == compile_text(text).digest


def test_packaged_schema_compile_invokes_replay_assurance_guard() -> None:
    from assurance_agent.workflow.graph.contracts import load_execution_contracts
    from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2

    schema = load_workflow_v2(Path.cwd())
    compiled = compile_workflow(schema, load_execution_contracts(Path.cwd()))
    assert compiled.digest
    assert "api-plan-cycle" in compiled.graphs
