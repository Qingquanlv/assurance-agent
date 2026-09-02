from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

import pytest

from assurance_product.models import ENTRYPOINT_RUNTIME_CUTOVER, PRODUCT_ENTRYPOINTS, RuntimeKind
from assurance_product.runtime_selection import (
    ENTRYPOINT_AGENT_CONTRACT_IDS,
    select_runtime,
    use_test_runtime_selector,
)

from tests.product.shadow_harness import PARITY_RECORDS, required_parity_scenarios

_WAVE_A_FLIP = frozenset(
    {
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    }
)
_AGENT_USING = frozenset(PRODUCT_ENTRYPOINTS - _WAVE_A_FLIP)

_INTAKE_ADVANCE = (
    "packages/capabilities/assurance-intake/tests/test_graph_join_any.py"
    "::test_current_trigger[assurance.intake.workflow.graph.entry/advance-join]"
)
_GENERATION_JOINS = tuple(
    "packages/capabilities/assurance-generation/tests/test_graph_join_any.py"
    f"::test_current_trigger[assurance.generation.workflow.graph.generation-{family}/plan-round-join]"
    for family in ("api", "e2e", "fuzz", "performance")
)
_PRODUCT_FAILED = (
    "tests/product/test_product_join_any.py"
    "::test_current_trigger[assurance.product.workflow.graph.product-execute/failed-join]"
)
_PRODUCT_COVERAGE = (
    "tests/product/test_product_join_any.py"
    "::test_current_trigger[assurance.product.workflow.graph.product-execute/coverage-needed]"
)
_PARITY_ROOT = (
    "tests/product/test_langgraph_shadow_parity.py"
    "::test_every_public_root_has_valid_failure_and_interrupt_parity"
)
_VALIDATOR_PARITY = (
    "tests/product/test_validator_shadow_parity.py"
    "::test_accepted_candidate_validates_once_and_promotes_on_both_runtimes",
    "tests/product/test_validator_shadow_parity.py"
    "::test_rejected_candidate_validates_once_and_never_prepares_or_promotes",
)
_EVALUATE_EVIDENCE = (
    "tests/product/test_langgraph_shadow_parity.py::test_standalone_evaluate_effect_settlement_parity",
    "tests/product/test_langgraph_shadow_parity.py::test_evaluate_inside_apply_uses_the_same_delivery_effect",
)
_CRASH_ROWS = (
    "tests/product/test_runtime_selection_security.py::test_initializing_record_is_not_resumable",
    "tests/product/test_runtime_selection_security.py::test_reopen_ignores_current_switch_during_initializing",
)


@dataclass(frozen=True, slots=True)
class WaveRecord:
    entrypoint: str
    runtime: RuntimeKind
    wave: str
    schema_capability: str
    schema_capability_reason: str | None
    join_citations: tuple[str, ...]
    parity_citations: tuple[str, ...]
    crash_citations: tuple[str, ...]


def _agent_reason(name: str) -> str:
    return (
        f"schema-capability-red: {name} reaches an Agent contract that requires "
        "provider schema while OpenCode advertises provider_schema=False"
    )


def _joins_for(name: str) -> tuple[str, ...]:
    if name in {"intake", "case"}:
        return (_INTAKE_ADVANCE,)
    if name == "execute":
        return _GENERATION_JOINS + (_PRODUCT_FAILED, _PRODUCT_COVERAGE)
    if name == "full":
        return (_INTAKE_ADVANCE,) + _GENERATION_JOINS + (_PRODUCT_FAILED, _PRODUCT_COVERAGE)
    return ()


def _wave_for(name: str) -> str:
    if name == "execute":
        return "B"
    if name == "full":
        return "C"
    return "A"


WAVE_RECORDS: MappingProxyType[str, WaveRecord] = MappingProxyType(
    {
        name: WaveRecord(
            entrypoint=name,
            runtime="langgraph-v1" if name in _WAVE_A_FLIP else "legacy-v2",
            wave=_wave_for(name),
            schema_capability="green" if name in _WAVE_A_FLIP else "red",
            schema_capability_reason=None if name in _WAVE_A_FLIP else _agent_reason(name),
            join_citations=_joins_for(name),
            parity_citations=(
                _PARITY_ROOT,
                *(_VALIDATOR_PARITY if name in _WAVE_A_FLIP else ()),
                *(_EVALUATE_EVIDENCE if name in _WAVE_A_FLIP else ()),
            ),
            crash_citations=_CRASH_ROWS if name in _WAVE_A_FLIP else (),
        )
        for name in sorted(PRODUCT_ENTRYPOINTS)
    }
)


@pytest.fixture(autouse=True)
def _reset_runtime_selector() -> None:
    yield
    use_test_runtime_selector(None)


def test_production_cutover_flips_only_schema_green_wave_a_names() -> None:
    assert set(ENTRYPOINT_RUNTIME_CUTOVER) == set(PRODUCT_ENTRYPOINTS)
    assert len(ENTRYPOINT_RUNTIME_CUTOVER) == 14
    flipped = {name for name, kind in ENTRYPOINT_RUNTIME_CUTOVER.items() if kind == "langgraph-v1"}
    assert flipped == set(_WAVE_A_FLIP)
    for name in _AGENT_USING:
        assert ENTRYPOINT_RUNTIME_CUTOVER[name] == "legacy-v2"
        assert select_runtime(name) == "legacy-v2"
    for name in _WAVE_A_FLIP:
        assert select_runtime(name) == "langgraph-v1"


def test_test_only_selector_may_choose_langgraph_without_mutating_production() -> None:
    use_test_runtime_selector(lambda _name: "langgraph-v1")
    assert select_runtime("intake") == "langgraph-v1"
    assert ENTRYPOINT_RUNTIME_CUTOVER["intake"] == "legacy-v2"
    use_test_runtime_selector(None)
    assert select_runtime("intake") == "legacy-v2"


def test_wave_records_cover_every_name_without_a_waiver_field() -> None:
    assert set(WAVE_RECORDS) == set(PRODUCT_ENTRYPOINTS)
    assert "waiver" not in WaveRecord.__dataclass_fields__
    for name, record in WAVE_RECORDS.items():
        assert record.entrypoint == name
        assert record.runtime == ENTRYPOINT_RUNTIME_CUTOVER[name]
        assert record.schema_capability in {"green", "red"}
        if record.schema_capability == "red":
            assert record.runtime == "legacy-v2"
            assert record.schema_capability_reason is not None
            assert "schema-capability-red" in record.schema_capability_reason
            assert "waiver" not in record.schema_capability_reason
        else:
            assert record.runtime == "langgraph-v1"
            assert record.schema_capability_reason is None
            assert record.join_citations == _joins_for(name)
            assert _PARITY_ROOT in record.parity_citations
            assert record.crash_citations == _CRASH_ROWS


def test_eligible_wave_a_names_have_no_agent_contract_on_the_path() -> None:
    from assurance_improvement.graphs import delivery

    assert delivery._EVALUATE_ID.startswith("assurance.improvement.task.")
    assert delivery._EXPORT_ID.startswith("assurance.improvement.task.")
    assert delivery._APPLY_ID.startswith("assurance.improvement.task.")
    assert delivery._ROLLBACK_ID.startswith("assurance.improvement.task.")
    assert delivery._AUTO_REVIEW_ID.startswith("assurance.improvement.task.")
    assert delivery._HUMAN_REVIEW_ID.startswith("assurance.improvement.task.")
    for name in _WAVE_A_FLIP:
        assert ENTRYPOINT_AGENT_CONTRACT_IDS[name] == ()
        assert not any(
            contract_id.startswith("assurance.") and ".agent." in contract_id
            for contract_id in ENTRYPOINT_AGENT_CONTRACT_IDS[name]
        )


def test_agent_using_names_stay_legacy_because_schema_capability_is_red() -> None:
    from assurance_product.agent_contracts import all_feature_agent_contracts

    contracts = all_feature_agent_contracts()
    assert all(not hasattr(contract, "requires_provider_schema") for contract in contracts.values())
    assert len(contracts) == 33
    for name in _AGENT_USING:
        record = WAVE_RECORDS[name]
        assert record.runtime == "legacy-v2"
        assert record.schema_capability == "red"
        assert record.schema_capability_reason is not None
        assert "schema-capability-red" in record.schema_capability_reason
        assert ENTRYPOINT_AGENT_CONTRACT_IDS[name]


@pytest.mark.parametrize("entrypoint", tuple(sorted(PRODUCT_ENTRYPOINTS)))
def test_each_entrypoint_has_a_passing_shadow_parity_record(
    product_runner, tmp_path, entrypoint: str
) -> None:
    from tests.product.shadow_harness import prove_entrypoint_parity

    for scenario in required_parity_scenarios(entrypoint):
        record = prove_entrypoint_parity(
            tmp_path,
            product_runner=product_runner,
            entrypoint=entrypoint,
            scenario=scenario,
        )
        key = (entrypoint, scenario)
        assert key in PARITY_RECORDS
        stored = PARITY_RECORDS[key]
        assert stored.passed, stored.mismatches
        assert stored is record
        assert stored.legacy.runtime == "legacy-v2"
        assert stored.langgraph.runtime == "langgraph-v1"


def test_wave_a_records_cite_validator_and_evaluate_evidence() -> None:
    evaluate = WAVE_RECORDS["improvement-evaluate"]
    apply = WAVE_RECORDS["improvement-apply"]
    for citation in _VALIDATOR_PARITY:
        assert citation
    assert _EVALUATE_EVIDENCE[0] in evaluate.parity_citations
    assert (
        _EVALUATE_EVIDENCE[1] in apply.parity_citations or _EVALUATE_EVIDENCE[1] in evaluate.parity_citations
    )


def test_rollback_changes_only_future_starts(tmp_path) -> None:
    from assurance_product.change_workspace import ChangeWorkspace
    from assurance_product.runtime_selection import (
        LangGraphRuntimeRecord,
        load_selection,
        write_initializing,
    )

    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("seed\n", encoding="utf-8")
    workspace = ChangeWorkspace.prepare(project, "CH-ROLL-001")
    recorded = LangGraphRuntimeRecord(
        phase="initializing",
        invocation_id="inv-already-started",
        entrypoint="improvement-evaluate",
        root_input_digest="a" * 64,
        build_identity="b" * 64,
    )
    write_initializing(workspace, recorded)
    use_test_runtime_selector(lambda _name: "legacy-v2")
    assert select_runtime("improvement-evaluate") == "legacy-v2"
    existing = load_selection(workspace, "inv-already-started")
    assert existing is not None
    assert existing.runtime == "langgraph-v1"
    assert existing.phase == "initializing"
