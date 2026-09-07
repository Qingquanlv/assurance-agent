from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast


def accepted_verified_execution_input(project: Path, *, change_id: str = "CH-USER-001"):
    """Install one complete accepted API generation closure for execution tests."""

    from assurance_execution.contracts.agent import ExecutionPrepareInputV1, VerifiedExecutionPrepareV1
    from assurance_execution.contracts.verification import FrozenUserInputsV1
    from assurance_generation.contracts.codegen import CodegenMapping
    from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1, CasePlanContextV1
    from assurance_generation.contracts.mapping import ClosedMappingEntryV1, ClosedMappingV1
    from assurance_generation.contracts.workflow import GenerationCycleResultV1
    from assurance_generation.operations.execution_plan import compile_case_plan
    from assurance_intake.contracts.loop_history import build_loop_round_history
    from assurance_intake.contracts.verification import AssertionSourcesV1
    from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
    from graph_engine.attempts import BusinessActivation
    from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
    from tests.acg_plan_fixture import install_plan
    from tests.verification_support import read_fixture

    def write(relative: str, content: bytes) -> EvidenceArtifactRefV1:
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(content).hexdigest())

    resolved, raw_plan_ref = install_plan(
        project,
        change_id,
        capability_leafs=("entities.item.create",),
        verification_policy={
            "validation_profile": "api_db.v1",
            "resource_id": "assurance.product.configuration.verification-policy",
            "digest": "f" * 64,
        },
    )
    plan_ref = EvidenceArtifactRefV1.model_validate(raw_plan_ref)
    requirement_ref = write(f"qa/changes/{change_id}/requirement.md", b"requirement")
    machine_case = cast(dict[str, Any], read_fixture("user-case.json"))
    machine_case["spec_digest"] = resolved.requirement_digest
    reviewed_case_document = {
        "schema_version": machine_case["schema_version"],
        "case_id": "TC_USER_CREATE_001",
        "title": "verified user creation",
        "status": "active",
        "priority": "P1",
        "severity": "major",
        "type": "API",
        "module": "users",
        "requirement_id": "REQ-USER-1",
        "feature_name": "user-management",
        "test_condition_id": "COND-USER-1",
        "design_technique": "use_case",
        "objective": "verify user creation",
        "summary": "create and observe one user",
        "preconditions": [],
        "test_data": [],
        "steps": ["create the user"],
        "assertions": machine_case["assertions"],
        "postconditions": [],
        "edge_cases": [],
        "related_cases": [],
        "risk": {
            "level": "high",
            "likelihood": 3,
            "impact": 4,
            "rationale": "verified path",
        },
        "automation": {"required": True, "framework": "pytest", "status": "planned"},
        "regression": {
            "candidate": True,
            "tier": "smoke",
            "rationale": "verified path",
            "selection_reason": ["critical_user_journey"],
            "maintenance_rule": "keep_until_feature_deprecated",
        },
        "trace": {"entities.item.create": {"covered": True}},
        "revision": machine_case["revision"],
        "spec_digest": machine_case["spec_digest"],
        "inputs": machine_case["inputs"],
    }
    case_ref = write(
        f"qa/changes/{change_id}/cases/system/user/case.yaml",
        canonical_json_bytes(
            cast(
                JSONValue,
                {
                    "schema_version": "1.0",
                    "added": [reviewed_case_document],
                    "modified": [],
                    "removed": [],
                },
            )
        ),
    )
    source_document = cast(dict[str, Any], read_fixture("user-sources.json"))
    source_document["spec_digest"] = resolved.requirement_digest
    cast(dict[str, Any], source_document["sources"][0])["content_ref"] = requirement_ref.model_dump(
        mode="json"
    )
    assertion_source_ref = write(
        f"qa/changes/{change_id}/cases/system/user/assertion-sources.json",
        canonical_json_bytes(cast(JSONValue, source_document)) + b"\n",
    )
    matrix_ref = write(
        f"qa/changes/{change_id}/trace/minimum-coverage-matrix.json",
        canonical_json_bytes(
            cast(
                JSONValue,
                [
                    {
                        "mrc_id": "MRC-API-001",
                        "key": "entities.item.create",
                        "required": True,
                        "covered_by_cases": ["TC_USER_CREATE_001"],
                        "status": "covered",
                        "category": "api",
                        "layer": "api",
                    }
                ],
            )
        )
        + b"\n",
    )
    product_source_ref = write("src/app.py", b"def create_user():\n    return None\n")
    review_ref = write(
        f"qa/changes/{change_id}/review/case-review.json",
        canonical_json_bytes(
            cast(
                JSONValue,
                {
                    "schema_version": "1.0",
                    "review_type": "case",
                    "change_id": change_id,
                    "decision": "pass",
                    "findings": [],
                    "auto_fix_plan": [],
                    "next_action": "continue",
                    "auto_fix_allowed": False,
                    "human_review_required": False,
                    "risk_level": "low",
                    "minimum_coverage": {
                        "total_required": 1,
                        "covered": 1,
                        "skipped_by_scope": 0,
                        "missing": [],
                    },
                    "source_verification": {
                        "independent": True,
                        "reviewed_source_files": ["src/app.py"],
                        "verified_claims": [
                            {"claim": "user creation is implemented", "evidence_files": ["src/app.py"]}
                        ],
                    },
                },
            )
        )
        + b"\n",
    )
    preparation_refs = tuple(
        sorted(
            (plan_ref, requirement_ref, assertion_source_ref, matrix_ref, product_source_ref),
            key=lambda item: item.path,
        )
    )
    reviewed_model = ReviewedCaseV1(
        change_id=change_id,
        coverage_epoch=2,
        plan_digest=resolved.plan_digest,
        plan_ref=plan_ref,
        preparation_refs=preparation_refs,
        case_refs=(case_ref,),
        review_ref=review_ref,
    )
    reviewed = reviewed_model.model_dump(mode="json")
    write(
        f"qa/changes/{change_id}/cases/reviewed-case.json",
        canonical_json_bytes(cast(JSONValue, reviewed)) + b"\n",
    )
    review_inputs = tuple(
        sorted((*reviewed_model.preparation_refs, *reviewed_model.case_refs), key=lambda item: item.path)
    )
    history = build_loop_round_history(
        change_id=change_id,
        coverage_epoch=2,
        loop_kind="case_review",
        family=None,
        round_index=0,
        outcome="pass",
        review_input_digest=canonical_digest(
            cast(JSONValue, [item.model_dump(mode="json") for item in review_inputs])
        ),
        source_refs=tuple(sorted((*review_inputs, review_ref), key=lambda item: item.path)),
    )
    write(
        f"qa/changes/{change_id}/cases/reviews/epochs/2/rounds/0.json",
        canonical_json_bytes(cast(JSONValue, history.model_dump(mode="json"))) + b"\n",
    )
    raw_plan = cast(dict[str, Any], read_fixture("user-plan.json"))
    bindings_ref = write(
        f"qa/changes/{change_id}/plans/api-execution-bindings.json",
        canonical_json_bytes(
            cast(
                JSONValue,
                {
                    "schema_version": "1",
                    "case_id": machine_case["case_id"],
                    "bindings": raw_plan["bindings"],
                },
            )
        )
        + b"\n",
    )
    assert resolved.verification_policy is not None
    context = CasePlanContextV1.model_validate(
        {
            **cast(dict[str, Any], raw_plan["context"]),
            "change_id": change_id,
            "plan_digest": resolved.plan_digest,
            "plan_ref": plan_ref.model_dump(mode="json"),
            "reviewed_case": reviewed,
            "verification_policy_digest": resolved.verification_policy.digest,
            "technical_config_digest": bindings_ref.digest,
            "sut_digest": canonical_digest(cast(JSONValue, [product_source_ref.model_dump(mode="json")])),
        }
    )
    plan = compile_case_plan(
        machine_case,
        AssertionSourcesV1.model_validate(source_document),
        cast(dict[str, object], raw_plan["bindings"]),
        "api_db.v1",
        context=context,
    )
    machine_bytes = (
        canonical_json_bytes(
            cast(
                JSONValue,
                CaseExecutionPlanSetV1(change_id=change_id, cases=(plan,)).model_dump(mode="json"),
            )
        )
        + b"\n"
    )
    machine_ref = write(f"qa/changes/{change_id}/plans/api-case-execution-plan.json", machine_bytes)
    target = "tests/api/test_user_create.py"
    symbol = "test_tc_user_create_001__create"
    source_ref = write(
        f"qa/changes/{change_id}/generated/api/files/{target}",
        (
            "from assurance_execution.bridge import execute_case\n\n"
            f"def {symbol}():\n"
            '    execute_case("TC_USER_CREATE_001")\n'
        ).encode(),
    )
    mapping = CodegenMapping.model_validate(
        {
            "schema_version": "1",
            "layer": "api",
            "entries": [{"case_id": plan.case_id, "symbol": symbol, "target_file": target}],
            "validation_profile": "api_db.v1",
            "coverage_epoch": 2,
            "plan_digest": resolved.plan_digest,
            "plan_ref": plan_ref.model_dump(mode="json"),
            "reviewed_case": reviewed,
            "case_execution_plan_ref": machine_ref.model_dump(mode="json"),
            "case_execution_plan_digest": machine_ref.digest,
            "case_spec_digests": {plan.case_id: plan.spec_digest},
        }
    )
    manifest_document = {
        "schema_version": "1",
        "change_id": change_id,
        "layer": "api",
        "files": [
            {
                "repo_path": target,
                "disposition": "generated",
                "role": "test_entry",
                "case_ids": [plan.case_id],
            }
        ],
        "mapping": mapping.model_dump(mode="json"),
        "required_capabilities": ["entities.item.create"],
    }
    manifest_ref = write(
        f"qa/changes/{change_id}/codegen/api-generated-files.json",
        canonical_json_bytes(cast(JSONValue, manifest_document)) + b"\n",
    )
    closed = ClosedMappingV1(
        selected=(f"{target}::{symbol}",),
        mappings=(
            ClosedMappingEntryV1(
                test=f"{target}::{symbol}",
                case_id=plan.case_id,
                capability="entities.item.create",
                layer="api",
            ),
        ),
    )
    closed_bytes = canonical_json_bytes(cast(JSONValue, closed.model_dump(mode="json"))) + b"\n"
    closed_ref = write(f"qa/changes/{change_id}/generation/epochs/2/mapping.json", closed_bytes)
    generation = GenerationCycleResultV1(
        change_id=change_id,
        coverage_epoch=2,
        reviewed_case=context.reviewed_case,
        plan_digest=resolved.plan_digest,
        plan_ref=plan_ref,
        mapping_ref=closed_ref,
        source_refs=tuple(sorted((manifest_ref, source_ref), key=lambda item: item.path)),
        plan_refs=tuple(sorted((bindings_ref, machine_ref), key=lambda item: item.path)),
        case_execution_plan_ref=machine_ref,
        case_execution_plan_digest=machine_ref.digest,
    )
    database = project / "verified.sqlite3"
    database.touch()
    verification = VerifiedExecutionPrepareV1(
        validation_profile="api_db.v1",
        case_execution_plan_ref=machine_ref,
        nodeid=f"{target}::{symbol}",
        business_activation=BusinessActivation.for_trigger("coverage.2.execute"),
        sut_instance_id="sut-1",
        sut_base_url="http://127.0.0.1:32123",
        managed_sqlite_path=str(database),
        observer_sqlite_path=str(database),
        user_inputs=FrozenUserInputsV1(username="probe", email="probe@example.test"),
        managed_sut_prepare_receipt_ref=EvidenceArtifactRefV1(
            path=f"qa/changes/{change_id}/execution/prepare.json", digest="a" * 64
        ),
        managed_sut_start_receipt_ref=EvidenceArtifactRefV1(
            path=f"qa/changes/{change_id}/execution/start.json", digest="b" * 64
        ),
        managed_sut_authority_handle="sut.authority",
    )
    return ExecutionPrepareInputV1(
        validation_profile="api_db.v1",
        verification_config_digest="c" * 64,
        change_id=change_id,
        plan_digest=resolved.plan_digest,
        plan_ref=plan_ref,
        selected_test_families=("api",),
        capability_leafs=("entities.item.create",),
        coverage_epoch=2,
        generation_result=generation,
        verification=verification,
    )


__all__ = ["accepted_verified_execution_input"]
