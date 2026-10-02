from __future__ import annotations

from typing import Annotated

from langgraph.graph import END, START, StateGraph

from graph_engine.artifacts import ArtifactRef
from graph_engine.stategraph.ledger import (
    AttemptLedgerState,
    NamedWrite,
    fill_artifact_ledger,
    ledger_refs,
    merge_artifact_ledger,
)
from graph_engine.stategraph.publish import bind_produced_artifacts

_CASE = NamedWrite(name="case", root="qa/cases", many=True)
_PROPOSAL = NamedWrite(name="proposal", root="qa/proposal.md")


def test_merge_artifact_ledger_lets_the_later_list_win() -> None:
    left = {"intake.case": [{"path": "qa/cases/menus/case.yaml", "digest": "a" * 64}]}
    right = {"intake.case": [{"path": "qa/cases/menus/case.yaml", "digest": "b" * 64}]}
    assert merge_artifact_ledger(None, left) == left
    assert merge_artifact_ledger(left, right)["intake.case"][0]["digest"] == "b" * 64
    assert merge_artifact_ledger(left, {}) == left
    bare = {"intake.plan": {"path": "qa/proposal.md", "digest": "c" * 64}}
    assert merge_artifact_ledger(None, bare)["intake.plan"] == [
        {"path": "qa/proposal.md", "digest": "c" * 64}
    ]


def test_fill_artifact_ledger_uses_committed_refs_for_a_directory() -> None:
    committed = (
        ArtifactRef(path="qa/cases/menus/case.yaml", digest="a" * 64),
        ArtifactRef(path="qa/proposal.md", digest="c" * 64),
        ArtifactRef(path="qa/cases/orders/case.yaml", digest="d" * 64),
    )
    ledger = fill_artifact_ledger("intake", (_CASE, _PROPOSAL), committed)
    assert ledger_refs(ledger, "intake.case") == [
        {"path": "qa/cases/menus/case.yaml", "digest": "a" * 64},
        {"path": "qa/cases/orders/case.yaml", "digest": "d" * 64},
    ]
    assert ledger["intake.proposal"] == [{"path": "qa/proposal.md", "digest": "c" * 64}]
    assert "intake.case:qa/cases/menus/case.yaml" not in ledger


def test_unnamed_committed_files_are_not_handed_off() -> None:
    committed = (ArtifactRef(path="qa/.qa.yaml", digest="a" * 64),)
    assert fill_artifact_ledger("intake", (_CASE,), committed) == {}


def test_bind_produced_artifacts_ignores_agent_claimed_paths_and_publish_ledger() -> None:
    def publish(state: dict[str, object], output: object, receipt: object, *, committed: object = ()) -> dict:
        del state, output, receipt, committed
        return {
            "status": "kept",
            "artifact_ledger": {"intake.case": [{"path": "qa/cases/other/case.yaml", "digest": "e" * 64}]},
        }

    bound = bind_produced_artifacts(publish, namespace="intake", writes=(_CASE,))
    update = bound(
        {},
        {"artifacts": [{"path": "qa/cases/other/case.yaml", "digest": "e" * 64}]},
        None,
        committed=(ArtifactRef(path="qa/cases/menus/case.yaml", digest="a" * 64),),
    )
    assert update["status"] == "kept"
    assert ledger_refs(update["artifact_ledger"], "intake.case") == [
        {"path": "qa/cases/menus/case.yaml", "digest": "a" * 64}
    ]


def test_attempt_ledger_state_reducer_merges_across_nodes() -> None:
    class State(AttemptLedgerState, total=False):
        artifact_ledger: Annotated[dict[str, list[dict[str, str]]], merge_artifact_ledger]

    def first(state: State) -> State:
        del state
        return {
            "artifact_ledger": fill_artifact_ledger(
                "intake",
                (_CASE,),
                (ArtifactRef(path="qa/cases/menus/case.yaml", digest="a" * 64),),
            )
        }

    def second(state: State) -> State:
        assert ledger_refs(state["artifact_ledger"], "intake.case")
        return {
            "artifact_ledger": {
                "intake.plan": [{"path": "qa/proposal.md", "digest": "c" * 64}],
            }
        }

    graph = StateGraph(State)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    result = graph.compile().invoke({})
    assert ledger_refs(result["artifact_ledger"], "intake.case")[0]["digest"] == "a" * 64
    assert result["artifact_ledger"]["intake.plan"][0]["path"] == "qa/proposal.md"
