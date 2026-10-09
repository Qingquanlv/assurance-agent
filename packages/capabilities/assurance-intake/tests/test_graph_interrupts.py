"""Case human-review uses the flow gate payload and resumes into the declared routes."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables.config import RunnableConfig
from langgraph.types import Command

from graph_engine.attempts.models.contracts import TaskAttemptContract
from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.testing import GraphHarness, committed
from graph_engine.testing.graph_harness import _prepare_anchored_backend

from assurance_intake.feature import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.graphs.factory import build_intake_graphs as _build_intake_graphs

from graph_engine.testing.feature_bundle import compile_bundle


def build_intake_graphs(*args, **kwargs):
    return compile_bundle(_build_intake_graphs(*args, **kwargs))


_SHA = "a" * 64
_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)


def _contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {
        **{contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()},
        **{contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()},
    }


def _reviewed_case() -> dict[str, object]:
    plan_ref = {"path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json", "digest": _SHA}
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 0,
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "preparation_refs": [{"path": "qa/requirement.md", "digest": _SHA}, plan_ref],
        "case_refs": [{"path": "qa/cases/menus/case.yaml", "digest": _SHA}],
        "review_ref": {"path": "qa/results/review/case-review.json", "digest": _SHA},
        "selection_ref": {"path": "qa/results/cases/epochs/0/selection.json", "digest": _SHA},
    }


def _input() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/cases", "qa/proposal.md", "qa/results"],
        "budgets": {"review_rounds": 2},
        "plan_digest": _SHA,
        "plan_ref": {"path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json", "digest": _SHA},
        "selected_test_families": ["api"],
        "case_delta_paths": ["qa/cases/menus/case.yaml"],
        "preparation_refs": [{"path": "qa/requirement.md", "digest": _SHA}],
        "coverage_epoch": 0,
    }


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "inv-1",
            "assurance_revision_id": "a" * 64,
            "assurance_product_lock_digest": "b" * 64,
            "assurance_root_input_digest": "c" * 64,
            "assurance_fencing_token": 1,
            "assurance_entrypoint": "intake",
        }
    }


async def _pause(script: Any) -> tuple[Any, Any, dict[str, object]]:
    harness = GraphHarness()
    backend = harness.anchored_memory_checkpointer()
    await _prepare_anchored_backend(backend)
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    harness._kernel.load_script(script)
    graph = bundle.case
    graph.checkpointer = backend
    paused = await graph.ainvoke(_input(), config=_config())
    assert isinstance(paused, dict)
    payload = paused["__interrupt__"][0].value
    assert isinstance(payload, dict)
    return harness, graph, payload


async def test_needs_human_interrupt_uses_the_flow_gate_payload() -> None:
    harness, _graph, payload = await _pause(
        {
            "intake.case-design": [committed({"artifacts": []}, _RECEIPT)],
            "intake.case-review": [
                committed(
                    {"public_outcome": "needs_human", "reviewed_case": _reviewed_case()},
                    _RECEIPT,
                )
            ],
        }
    )
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls] == [
        "intake.case-design",
        "intake.case-review",
    ]
    assert payload["interrupt_id"] == "case.human-review"
    assert payload["reason"] == "human-review"
    assert payload["actions"] == ["approve", "reject", "request_rework"]
    assert "failure" not in payload
    assert "rounds_used" not in payload
    assert "rounds_budget" not in payload
    assert "node_id" not in payload
    assert payload["rounds"]


async def test_approve_after_needs_fix_human_review_is_reviewed() -> None:
    _harness, graph, payload = await _pause(
        {
            "intake.case-design": [committed({"artifacts": []}, _RECEIPT)],
            "intake.case-review": [
                committed(
                    {"public_outcome": "needs_human", "reviewed_case": _reviewed_case()},
                    _RECEIPT,
                )
            ],
        }
    )
    assert payload["reason"] == "human-review"
    resumed = await graph.ainvoke(Command(resume={"action": "approve"}), config=_config())
    assert isinstance(resumed, dict)
    assert resumed["status"] == "reviewed"
    assert resumed["flow_outcome"] == "reviewed"
    assert resumed["reviewed_refs"] == _reviewed_case()["preparation_refs"]
    assert "reviewed_case" not in resumed
    assert "receipt" not in resumed
    assert "case_receipt" not in resumed


async def test_human_reject_ends_rejected() -> None:
    _harness, graph, _payload = await _pause(
        {
            "intake.case-design": [committed({"artifacts": []}, _RECEIPT)],
            "intake.case-review": [
                committed(
                    {"public_outcome": "needs_human", "reviewed_case": _reviewed_case()},
                    _RECEIPT,
                )
            ],
        }
    )
    resumed = await graph.ainvoke(Command(resume={"action": "reject"}), config=_config())
    assert isinstance(resumed, dict)
    assert resumed["status"] == "rejected"
    assert resumed["reviewed_refs"] == _reviewed_case()["preparation_refs"]
    assert "reviewed_case" not in resumed


async def test_request_rework_returns_to_case_design() -> None:
    harness, graph, _payload = await _pause(
        {
            "intake.case-design": [
                committed({"artifacts": []}, _RECEIPT),
                committed({"artifacts": []}, _RECEIPT),
            ],
            "intake.case-review": [
                committed({"public_outcome": "needs_human", "reviewed_case": _reviewed_case()}, _RECEIPT),
                committed({"public_outcome": "pass", "reviewed_case": _reviewed_case()}, _RECEIPT),
            ],
        }
    )
    resumed = await graph.ainvoke(Command(resume={"action": "request_rework"}), config=_config())
    assert isinstance(resumed, dict)
    assert resumed["status"] == "reviewed"
    assert [call.semantic_node_id for call in harness._kernel.semantic_calls] == [
        "intake.case-design",
        "intake.case-review",
        "intake.case-design",
        "intake.case-review",
    ]
