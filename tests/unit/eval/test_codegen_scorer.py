"""Dark-ship current-chain / selected-test write scoring (Task 18)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.generated_files import ApiGeneratedFilesV1, GeneratedFileEntryV1
from assurance_agent.eval.scorers import get_scorer
from assurance_agent.eval.scorers import current_codegen as cc
from assurance_agent.eval.types import DatasetSample
from assurance_agent.verification.generated_entries import MappedTestEntry, extract_layer_mapping
from assurance_agent.workflow.graph.precommit import (
    GENERATED_FILES_CANDIDATE_V1,
    CandidateValidationReceiptV1,
    validator_semantics_digest,
)
from assurance_agent.workflow.graph.task_inputs import TaskInputSnapshotEntryV1, TaskInputSnapshotV1
from assurance_agent.workflow.graph.task_inputs import _entries_input_sha256  # noqa: PLC2701
from assurance_agent.workflow.graph.workspace import WriteEntry, WriteSet
from tests.unit.eval.attempt_fixtures import make_attempt


def _digest(data: bytes) -> str:
    """Prefixed digest used by manifest/snapshot fields."""
    return sha256_bytes(data)


def _bare_digest(data: bytes) -> str:
    """Bare 64-hex digest used by CandidateValidationReceipt output_digests."""
    return hashlib.sha256(data).hexdigest()


def _cases(*case_ids: str, case_type: str = "API") -> list[dict[str, object]]:
    return [
        {
            "added": [
                {
                    "case_id": case_id,
                    "type": case_type,
                    "automation": {"required": True},
                }
                for case_id in case_ids
            ],
            "modified": [],
        }
    ]


def _api_plan(symbol: str = "test_api_001", path: str = "tests/api/test_a.py") -> str:
    return f"""# API Codegen Plan

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| API_001 | `{symbol}` | `{path}` |
"""


_API_GOOD = """
def test_api_001(client):
    response = client.get("/api/v1/api/list")
    assert response.status_code == 200
    assert response.json()["items"]
"""

_API_ASSERT_TRUE = """
def test_api_001(client):
    response = client.get("/api/v1/api/list")
    assert True
"""


def _attempt(
    *,
    layer: str = "api",
    generation: str = "gen-1",
    applicable: bool = True,
    include_preflight: bool | None = None,
    plan_attempt: bool = False,
    foreign_generation_for: str | None = None,
    with_write: bool = True,
    source: str = _API_GOOD,
    na_ok: bool = False,
    policy_ok: bool = True,
    sibling_write: bool = False,
    delete_only: bool = False,
) -> cc.CurrentLayerBinding:
    if include_preflight is None:
        include_preflight = layer in {"fuzz", "performance"}
    roles: list[str] = ["applicability"]
    if include_preflight:
        roles.append("preflight")
    if applicable:
        roles.extend(["reviewer", "plan_gate", "precheck", "codegen"])
    attempts: list[cc.AttemptBinding] = []
    if layer == "performance":
        summary_key = "codegen/performance-codegen-summary.md"
        manifest_key = "codegen/performance-generated-files.json"
        private = "tests/perf"
        target = "tests/perf/locustfile.py"
        symbol = "list_apis"
        case_id = "PERF_001"
        case_type = "Performance"
        plan = """# Perf

## Task Mapping

| Case ID | Task Method | Target File |
|---------|-------------|-------------|
| PERF_001 | `list_apis` | `tests/perf/locustfile.py` |
"""
        source = """
from locust import HttpUser, task

class ApiUser(HttpUser):
    @task
    def list_apis(self):
        self.client.get("/api/v1/api/list")
"""
    elif layer == "fuzz":
        summary_key = "codegen/fuzz-codegen-summary.md"
        manifest_key = "codegen/fuzz-generated-files.json"
        private = "tests/fuzz"
        target = "tests/fuzz/test_fuzz.py"
        symbol = "test_fuzz_001"
        case_id = "FUZZ_001"
        case_type = "Fuzz"
        plan = """# Fuzz

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| FUZZ_001 | `test_fuzz_001` | `tests/fuzz/test_fuzz.py` |

## Schema Acquisition

| Case ID | Strategy | Import Path |
|---------|----------|-------------|
| FUZZ_001 | `from_asgi` | `app.main:app` |
"""
        source = """
import schemathesis

schema = schemathesis.openapi.from_asgi("app.main:app")

@schema.parametrize()
def test_fuzz_001(case):
    case.call_and_validate()
"""
    elif layer == "e2e":
        summary_key = "codegen/e2e-codegen-summary.md"
        manifest_key = "codegen/e2e-generated-files.json"
        private = "tests/e2e"
        target = "tests/e2e/test_e2e.py"
        symbol = "test_e2e_001"
        case_id = "E2E_001"
        case_type = "E2E"
        plan = """# E2E

## Test Function Mapping

| Case ID | Test Function | Target File |
|---------|---------------|-------------|
| E2E_001 | `test_e2e_001` | `tests/e2e/test_e2e.py` |
"""
        source = """
from playwright.sync_api import expect

def test_e2e_001(page):
    page.goto("/login")
    page.fill("#user", "admin")
    page.click("button[type=submit]")
    expect(page.locator("h1")).to_be_visible()
"""
    else:
        summary_key = "codegen/api-codegen-summary.md"
        manifest_key = "codegen/api-generated-files.json"
        private = "tests/api"
        target = "tests/api/test_a.py"
        symbol = "test_api_001"
        case_id = "API_001"
        case_type = "API"
        plan = _api_plan()

    tree_src, tree_tgt = "tree-a", "tree-b"
    for role in roles:
        gen = generation
        if foreign_generation_for == role:
            gen = "gen-other"
        outputs = {}
        if role == "codegen":
            outputs = {
                summary_key: _bare_digest(b"# summary\n"),
                manifest_key: _bare_digest(b'{"schema_version":"1"}'),
            }
        attempts.append(
            cc.AttemptBinding(
                role=role,  # type: ignore[arg-type]
                attempt_id=f"{role}-attempt",
                task_id=f"{role}-task",
                generation_id=gen,
                source_tree_id=tree_src,
                target_tree_id=tree_tgt,
                output_digests=outputs,
            )
        )

    write_attr = None
    if applicable and with_write:
        write_attr = _api_write_attribution(
            plan=plan,
            source=source,
            target=target,
            symbol=symbol,
            case_id=case_id,
            case_type=case_type,
            layer=layer,
            private=private,
            summary_key=summary_key,
            manifest_key=manifest_key,
            sibling_write=sibling_write,
            delete_only=delete_only,
        )
        # Align codegen output digests with attribution.
        attempts = [
            replace(
                item,
                output_digests={
                    summary_key: write_attr.summary_digest,
                    manifest_key: write_attr.manifest_digest,
                },
            )
            if item.role == "codegen"
            else item
            for item in attempts
        ]

    return cc.CurrentLayerBinding(
        layer=layer,  # type: ignore[arg-type]
        applicable=applicable,
        root_invocation_id="root-1",
        assurance_invocation_id="assurance-1",
        branch_invocation_id="branch-1",
        cycle_generation_id=generation,
        attempts=tuple(attempts),
        na_document_ok=na_ok,
        plan_attempt_present=plan_attempt,
        write_attribution=write_attr,
        policy_integrity_ok=policy_ok,
    )


def _api_write_attribution(
    *,
    plan: str,
    source: str,
    target: str,
    symbol: str,
    case_id: str,
    case_type: str,
    layer: str,
    private: str,
    summary_key: str,
    manifest_key: str,
    sibling_write: bool = False,
    delete_only: bool = False,
) -> cc.WriteAttribution:
    mapping = extract_layer_mapping(layer=layer, plan_text=plan, cases=_cases(case_id, case_type=case_type))
    source_bytes = source.encode("utf-8")
    after = _digest(source_bytes)
    before = _digest(b"old\n") if not delete_only else after
    entries: list[WriteEntry] = []
    if delete_only:
        entries.append(
            WriteEntry(
                logical_path=f"repo:{target}",
                operation="delete",
                before_sha256=before,
                after_sha256=None,
                blob_sha256=None,
            )
        )
    else:
        entries.append(
            WriteEntry(
                logical_path=f"repo:{target}",
                operation="add",
                before_sha256=None,
                after_sha256=after,
                blob_sha256=after,
            )
        )
    if sibling_write:
        entries.append(
            WriteEntry(
                logical_path="repo:tests/e2e/test_other.py",
                operation="add",
                before_sha256=None,
                after_sha256=_digest(b"x"),
                blob_sha256=_digest(b"x"),
            )
        )
    write_set = WriteSet(
        write_set_id="b" * 64,
        task_id="codegen-task",
        base_tree_id="c" * 64,
        entries=tuple(sorted(entries, key=lambda e: e.logical_path)),
        outputs_sha256={},
    )
    summary_digest = _bare_digest(b"# summary\n")
    manifest_model = ApiGeneratedFilesV1(
        schema_version="1",
        change_id="eval-sample-001",
        layer="api",
        files=[
            GeneratedFileEntryV1(
                repo_path=target,
                disposition="generated",
                role="test_entry",
                case_ids=[case_id],
                content_sha256=after,
            )
        ]
        if layer == "api"
        else [],
    )
    # For non-api layers use the generic shape via model subclass fields through dump/load.
    if layer != "api":
        from assurance_agent.verification.generated_files import get_generated_files_model

        model_cls = get_generated_files_model(layer)
        manifest_model = model_cls.model_validate(
            {
                "schema_version": "1",
                "change_id": "eval-sample-001",
                "layer": layer,
                "files": [
                    {
                        "repo_path": target,
                        "disposition": "generated",
                        "role": "test_entry",
                        "case_ids": [case_id],
                        "content_sha256": after,
                    }
                ],
            }
        )
    manifest_digest = _bare_digest(canonical_json_bytes(manifest_model))
    plan_digest = _digest(plan.encode("utf-8"))
    case_payload = {
        "added": [{"case_id": case_id, "type": case_type, "automation": {"required": True}}],
        "modified": [],
    }
    case_bytes = yaml.safe_dump(case_payload).encode("utf-8")
    case_digest = _digest(case_bytes)
    snap_entries = [
        TaskInputSnapshotEntryV1(
            physical_relpath="plans/plan.md",
            repo_relpath="plans/plan.md",
            logical_aliases=["change:plans/plan.md"],
            matched_claims=["change:plans/**"],
            origins=["contract_read"],
            kind="file",
            mode=0o644,
            sha256=plan_digest,
            symlink_target=None,
        ),
        TaskInputSnapshotEntryV1(
            physical_relpath="cases/case.yaml",
            repo_relpath="cases/case.yaml",
            logical_aliases=["change:cases/case.yaml"],
            matched_claims=["change:cases/**"],
            origins=["contract_read"],
            kind="file",
            mode=0o644,
            sha256=case_digest,
            symlink_target=None,
        ),
    ]
    snap_entries = sorted(snap_entries, key=lambda item: item.physical_relpath)
    snapshot = TaskInputSnapshotV1(
        schema_version="1",
        invocation_id="inv-1",
        task_id="codegen-task",
        attempt_id="codegen-attempt",
        base_tree_id="c" * 64,
        materialized_tree_id="c" * 64,
        input_sha256=_entries_input_sha256(snap_entries),
        runtime_context_sha256=None,
        contract_digest="sha256:" + "1" * 64,
        claims_digest="sha256:" + "2" * 64,
        entries=snap_entries,
    )
    snapshot_id = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
    receipt = CandidateValidationReceiptV1(
        schema_version="1",
        validator_id=GENERATED_FILES_CANDIDATE_V1,
        validator_semantics_digest=validator_semantics_digest(GENERATED_FILES_CANDIDATE_V1),
        root_invocation_id="root-1",
        invocation_id="inv-1",
        task_id="codegen-task",
        attempt_id="codegen-attempt",
        input_snapshot_id=snapshot_id,
        output_digests={
            summary_key: summary_digest,
            manifest_key: manifest_digest,
        },
        write_set_id=write_set.write_set_id,
        decision_payload_sha256="sha256:" + "9" * 64,
    )
    siblings = frozenset({"tests/e2e"}) if sibling_write else frozenset()
    return cc.WriteAttribution(
        write_set=write_set,
        receipt=receipt,
        input_snapshot=snapshot,
        input_snapshot_id=snapshot_id,
        runtime_context_sha256=snapshot.runtime_context_sha256,
        manifest=manifest_model,
        summary_digest=summary_digest,
        manifest_digest=manifest_digest,
        content_changed_paths=frozenset({target}) if not delete_only else frozenset(),
        source_by_path={target: source},
        mapping=mapping,
        private_root=private,
        selected_sibling_roots=siblings,
        forbidden_write_count=0,
    )


def test_live_codegen_scorer_still_credits_assert_true_and_exposes_hard_metrics(
    tmp_path: Path,
) -> None:
    attempt = make_attempt(tmp_path)
    tests_api = attempt / "raw-output" / "tests" / "api"
    tests_api.mkdir(parents=True)
    (tests_api / "test_ok.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    codegen = attempt / "raw-output" / "codegen"
    codegen.mkdir(parents=True)
    (codegen / "api-codegen-summary.md").write_text("# summary\n", encoding="utf-8")
    sample = DatasetSample(id="WAC-1", suite="workflow-api-codegen", input={}, expected={})
    metrics = get_scorer("workflow-api-codegen")(sample, attempt).metrics
    assert metrics["schema_valid_rate"] == 1.0
    assert metrics["codegen_summary_present_rate"] == 1.0
    # Task 22 activation: hard metrics are live and zero without export binding.
    for name in cc.HARD_METRIC_NAMES:
        assert metrics[name] == 0.0


def test_applicable_full_chain_credits_behavioral_write() -> None:
    binding = _attempt(layer="api", source=_API_GOOD)
    evidence = cc.evaluate_layer_binding(binding)
    assert evidence.chain_ok
    assert evidence.codegen_attempt_ok
    assert evidence.selected_test_write_ok
    metrics = cc.score_current_codegen_metrics([evidence])
    assert metrics["current_assurance_chain_rate"] == 1.0
    assert metrics["current_codegen_attempt_rate"] == 1.0
    assert metrics["selected_test_write_rate"] == 1.0


@pytest.mark.parametrize("layer", ["api", "e2e", "fuzz", "performance"])
def test_each_layer_applicable_chain_and_write(layer: str) -> None:
    binding = _attempt(layer=layer)
    evidence = cc.evaluate_layer_binding(binding)
    assert evidence.chain_ok
    assert evidence.selected_test_write_ok


def test_inapplicable_requires_na_and_absence() -> None:
    ok = _attempt(layer="api", applicable=False, with_write=False, na_ok=True)
    evidence = cc.evaluate_layer_binding(ok)
    assert evidence.chain_ok
    assert not evidence.codegen_attempt_ok
    assert not evidence.selected_test_write_ok

    bad = _attempt(layer="api", applicable=False, with_write=False, na_ok=False)
    assert not cc.evaluate_layer_binding(bad).chain_ok


def test_epoch_mismatch_across_cycles_fails() -> None:
    binding = _attempt(layer="api", foreign_generation_for="reviewer")
    evidence = cc.evaluate_layer_binding(binding)
    assert not evidence.chain_ok
    assert evidence.reason_code == "epoch_generation_mismatch"


def test_plan_attempt_in_codegen_only_fails() -> None:
    binding = _attempt(layer="api", plan_attempt=True)
    assert not cc.evaluate_layer_binding(binding).chain_ok


def test_assert_true_write_scores_zero() -> None:
    binding = _attempt(layer="api", source=_API_ASSERT_TRUE)
    evidence = cc.evaluate_layer_binding(binding)
    assert evidence.chain_ok
    assert evidence.codegen_attempt_ok
    assert not evidence.selected_test_write_ok


def test_delete_only_and_sibling_writes_score_zero() -> None:
    delete_binding = _attempt(layer="api", delete_only=True)
    assert not cc.evaluate_layer_binding(delete_binding).selected_test_write_ok
    sibling = _attempt(layer="api", sibling_write=True)
    assert not cc.evaluate_layer_binding(sibling).selected_test_write_ok


def test_policy_integrity_failure_zeros_all_hard_metrics() -> None:
    binding = _attempt(layer="api", policy_ok=False)
    evidence = cc.evaluate_layer_binding(binding)
    metrics = cc.score_current_codegen_metrics([evidence])
    assert metrics == {
        "current_assurance_chain_rate": 0.0,
        "current_codegen_attempt_rate": 0.0,
        "selected_test_write_rate": 0.0,
    }


def test_aggregation_applicable_plus_inapplicable() -> None:
    api = cc.evaluate_layer_binding(_attempt(layer="api"))
    e2e_na = cc.evaluate_layer_binding(_attempt(layer="e2e", applicable=False, with_write=False, na_ok=True))
    metrics = cc.score_current_codegen_metrics([api, e2e_na])
    assert metrics["current_assurance_chain_rate"] == 1.0
    assert metrics["current_codegen_attempt_rate"] == 1.0
    assert metrics["selected_test_write_rate"] == 1.0


def test_aggregation_two_applicable_one_write() -> None:
    api = cc.evaluate_layer_binding(_attempt(layer="api"))
    e2e = cc.evaluate_layer_binding(_attempt(layer="e2e", with_write=False))
    metrics = cc.score_current_codegen_metrics([api, e2e])
    assert metrics["current_assurance_chain_rate"] == 1.0
    # Both layers still have a current codegen attempt; only one wrote a selected test.
    assert metrics["current_codegen_attempt_rate"] == 1.0
    assert metrics["selected_test_write_rate"] == 0.5


def test_aggregation_zero_applicable_returns_zero() -> None:
    e2e_na = cc.evaluate_layer_binding(_attempt(layer="e2e", applicable=False, with_write=False, na_ok=True))
    metrics = cc.score_current_codegen_metrics([e2e_na])
    assert metrics["current_assurance_chain_rate"] == 1.0
    assert metrics["current_codegen_attempt_rate"] == 0.0
    assert metrics["selected_test_write_rate"] == 0.0


def test_successful_branch_from_another_root_does_not_launder() -> None:
    # Foreign root success is represented as a binding with mismatched root ids /
    # missing current chain roles — chain must fail for the selected current root.
    foreign = _attempt(layer="api")
    foreign = replace(foreign, root_invocation_id="other-root", attempts=())
    evidence = cc.evaluate_layer_binding(foreign)
    assert not evidence.chain_ok


def test_snapshot_loader_reads_only_bound_blobs() -> None:
    plan = _api_plan()
    cases = _cases("API_001")
    plan_bytes = plan.encode("utf-8")
    case_bytes = json.dumps(cases[0]).encode("utf-8")
    entries = [
        TaskInputSnapshotEntryV1(
            physical_relpath="cases/case.yaml",
            repo_relpath="cases/case.yaml",
            logical_aliases=["change:cases/case.yaml"],
            matched_claims=["change:cases/**"],
            origins=["contract_read"],
            kind="file",
            mode=0o644,
            sha256=_digest(case_bytes),
            symlink_target=None,
        ),
        TaskInputSnapshotEntryV1(
            physical_relpath="plans/plan.md",
            repo_relpath="plans/plan.md",
            logical_aliases=["change:plans/plan.md"],
            matched_claims=["change:plans/**"],
            origins=["contract_read"],
            kind="file",
            mode=0o644,
            sha256=_digest(plan_bytes),
            symlink_target=None,
        ),
    ]
    snapshot = TaskInputSnapshotV1(
        schema_version="1",
        invocation_id="inv",
        task_id="t",
        attempt_id="a",
        base_tree_id="c" * 64,
        materialized_tree_id="c" * 64,
        input_sha256=_entries_input_sha256(entries),
        runtime_context_sha256="sha256:" + "3" * 64,
        contract_digest="sha256:" + "1" * 64,
        claims_digest="sha256:" + "2" * 64,
        entries=entries,
    )
    blobs = {
        _digest(plan_bytes): plan_bytes,
        _digest(case_bytes): case_bytes,
    }
    loaded_plan, loaded_cases = cc.load_plan_case_bytes_from_snapshot(
        input_snapshot=snapshot,
        input_snapshot_id="snap-1",
        blobs=blobs,
        plan_logical="change:plans/plan.md",
        case_logicals=["change:cases/case.yaml"],
    )
    assert loaded_plan == plan
    relation = extract_layer_mapping(layer="api", plan_text=loaded_plan, cases=loaded_cases)
    assert relation.entries[0].symbol == "test_api_001"


def test_mapped_entry_helper_usable() -> None:
    entry = MappedTestEntry(case_id="API_001", symbol="test_api_001", target_file="tests/api/test_a.py")
    assert entry.symbol.startswith("test_")
