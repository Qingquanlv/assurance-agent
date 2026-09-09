from __future__ import annotations

import json
from pathlib import Path

from tests.product.cli_support import SECRET_ENV
from tests.product.test_user_oracle_full_workflow import BENCHMARK, REPO, load


def _installed_user_sources(tmp_path: Path, installed_sources):
    import shutil

    from assurance_product.binding_builder import build_deployment_wheel
    from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition
    from graph_engine.composition import ConfigTreePluginSource, WheelPluginSource
    from tests.product.composition_harness import _extract_wheel, evict_generated_binding_modules

    del installed_sources
    runner = load("run_item")
    project = tmp_path / "project"
    runner._prepare_user_project(repo=REPO, project_dir=project, fault="none")
    config_tree = tmp_path / "config-tree"
    shutil.copytree(REPO / "tests/product/fixtures/project-config", config_tree)
    runner._prepare_project_config_tree(config_tree, project)
    item = dict(
        runner._manifest_item(
            json.loads((BENCHMARK / "manifest.json").read_text(encoding="utf-8")),
            "opencode-user-api-db",
            "opencode",
        )
    )
    output = tmp_path / "runtime"
    output.mkdir()
    runner._configure_user_host(repo=REPO, project=project, output=output, item=item, fault="none")
    deployment = tmp_path / "deployment.yaml"
    runner._write_deployment_manifest(deployment, item, project_scope=str(project), adapter="opencode")
    document = json.loads(deployment.read_text(encoding="utf-8"))
    document["secret_handles"] = [item["secret_handle"]]
    deployment.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    built = build_deployment_wheel(deployment, tmp_path / "binding-wheel")
    evict_generated_binding_modules()
    _extract_wheel(built.wheel, tmp_path / "extract-user-binding")
    composition = resolve_assurance_composition(
        AssuranceCompositionRequest(
            product_entrypoint="assurance-opencode",
            deployment_source=WheelPluginSource(
                distribution=built.distribution,
                entrypoint_name="deployment",
                declaration_path=built.declaration_path,
            ),
            configuration_tree=ConfigTreePluginSource(path=config_tree.resolve()),
        )
    )
    return project, item, composition, config_tree


def _write_full_input(path: Path, composition, *, change_id: str) -> None:
    from assurance_product.models import ProductInputV1
    from assurance_product.verification_execution import verification_configuration

    def ref(resource_id: str) -> dict[str, str]:
        entry = composition.registries.resources.entries[resource_id]
        return {"resource_id": resource_id, "sha256": entry.sha256}

    catalog_ref = ref("assurance.product.configuration.capability-catalog")
    catalog = json.loads(
        composition.registries.resources.entries[catalog_ref["resource_id"]].content.decode("utf-8")
    )
    profile, digest = verification_configuration(composition)
    payload = ProductInputV1.model_validate(
        {
            "schema_version": "1",
            "change_id": change_id,
            "requirement": (
                REPO / "benchmark/vue-fastapi-admin/benchmark/requirements/user-create-oracle.md"
            ).read_text().strip(),
            "run_mode": "case",
            "candidate_test_families": ["api"],
            "case_delta_paths": [f"qa/changes/{change_id}/cases/system/user/case.yaml"],
            "capability_leafs": catalog["typed_leafs"],
            "capability_catalog": catalog_ref,
            "product_policy": ref("assurance.product.configuration.product-policy"),
            "data_knowledge": ref("assurance.product.configuration.data-knowledge"),
            "allowed_artifact_paths": ["qa/archive", "qa/cases", "qa/changes", "tests"],
            "budgets": {
                "review_rounds": 4,
                "coverage_rounds": 2,
                "healing_rounds": 2,
                "execution_retries": 2,
            },
            "validation_profile": profile.validation_profile,
            "verification_config_digest": digest,
            "verification_policy": ref("assurance.product.configuration.verification-policy"),
        }
    )
    path.write_text(json.dumps(payload.model_dump(mode="json")), encoding="utf-8")


def _assembly_paths(project: Path, change_id: str) -> dict[str, Path]:
    root = project / "qa" / "changes" / change_id
    return {
        "requirement": root / "requirement.md",
        "exploration": root / "explore" / "exploration.json",
        "case": root / "cases" / "system" / "user" / "case.yaml",
        "sources": root / "cases" / "system" / "user" / "assertion-sources.json",
        "review": root / "review" / "case-review.json",
        "bindings": root / "plans" / "api-execution-bindings.json",
        "machine_plan": root / "plans" / "api-case-execution-plan.json",
        "codegen": root / "codegen" / "api-generated-files.json",
    }


def _host_secret_values(item: dict) -> list[str]:
    args = list(item.get("host_secret_args") or [])
    return [args[index + 1] for index, flag in enumerate(args) if flag == "--secret"]


def _write_logical(write_root: Path, relative: str, content: str | bytes) -> None:
    path = write_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)


def _agent_success(payload: object):
    from agent_runtime_contracts import AgentRunResult
    from agent_runtime_contracts.schema import canonical_digest
    from graph_engine.plugin_api import TaskOutcome

    result = AgentRunResult(
        result_payload=payload,
        result_digest=canonical_digest(payload),
        evidence_digest=canonical_digest({"adapter": "scripted"}),
        adapter_id="opencode",
        adapter_version="0.1.0",
    )
    return TaskOutcome.succeeded(result.model_dump(mode="json"))


def _requirement_text() -> str:
    return (
        REPO / "benchmark/vue-fastapi-admin/benchmark/requirements/user-create-oracle.md"
    ).read_text(encoding="utf-8").strip()


def _user_case_entry() -> dict:
    from tests.verification_support import read_fixture

    machine = read_fixture("user-case.json")
    return {
        "case_id": "TC_USER_CREATE_001",
        "title": "create one user and observe the persisted row",
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
        "assertions": machine["assertions"],
        "postconditions": [],
        "edge_cases": [],
        "related_cases": [],
        "risk": {
            "level": "high",
            "likelihood": 3,
            "impact": 4,
            "rationale": "verified user create path",
        },
        "automation": {"required": True, "framework": "pytest", "status": "planned"},
        "regression": {
            "candidate": True,
            "tier": "smoke",
            "rationale": "verified path",
            "selection_reason": ["critical_user_journey"],
            "maintenance_rule": "keep_until_feature_deprecated",
        },
        "trace": {"entities.user": {"covered": True}},
    }


def _scripted_opencode_execute(request, context):
    import hashlib
    import json

    import yaml
    from agent_runtime_opencode.discovery import agent_run_from_request
    from tests.verification_support import read_fixture

    agent_run = agent_run_from_request(request)
    schema_id = agent_run.result_contract.schema_id
    change_id = agent_run.workspace.scope_id
    write_root = Path(context.write_root)
    allowed = tuple(agent_run.workspace.allowed_outputs)
    requirement = _requirement_text()

    if schema_id.endswith("intake.v1"):
        files = {
            f"qa/changes/{change_id}/requirement.md": requirement + "\n",
            f"qa/changes/{change_id}/.qa.yaml": yaml.safe_dump(
                {"schema_version": "1", "change_id": change_id}, sort_keys=True
            ),
        }
        for relative, content in files.items():
            _write_logical(write_root, relative, content)
        return _agent_success({"output_files": list(allowed)})

    if schema_id.endswith("explore.v1"):
        exploration = {
            "schema_version": "1",
            "change_id": change_id,
            "context_ref": "explore/context.json",
            "generated_at": "1970-01-01T00:00:00Z",
            "executive_summary": "User create should be verified as an API-only path.",
            "watchlist": [],
            "evidence_inventory": {"available": [], "missing": [], "not_inspected": []},
            "source_code_evidence": [
                {
                    "id": "user-controller",
                    "source": "source_code",
                    "type": "controller",
                    "description": "User create controller",
                    "parse_confidence_cap": "medium",
                    "module": "app.controllers.user",
                }
            ],
            "case_design_guidance": {
                "priority_hints": [],
                "suggested_scenarios": [],
                "regression_focus": [],
            },
            "minimum_required_coverage": {"api": ["entities.user"]},
            "open_questions_for_case_design": [],
            "test_strategy": {
                "scope": {"in_scope": ["user create"], "out_of_scope": ["user update"]},
                "data_focus": ["username", "email"],
                "depth": "core",
                "layer_recommendation": [
                    {
                        "layer": "API",
                        "recommended": True,
                        "rationale": "HTTP create plus SQLite observation",
                        "evidence_ids": ["user-controller"],
                    },
                    {
                        "layer": "E2E",
                        "recommended": False,
                        "rationale": "API-only profile",
                        "evidence_ids": ["user-controller"],
                    },
                    {
                        "layer": "Fuzz",
                        "recommended": False,
                        "rationale": "API-only profile",
                        "evidence_ids": ["user-controller"],
                    },
                    {
                        "layer": "Performance",
                        "recommended": False,
                        "rationale": "API-only profile",
                        "evidence_ids": ["user-controller"],
                    },
                ],
                "approach": "one create action with independent DB observation",
            },
        }
        _write_logical(
            write_root,
            f"qa/changes/{change_id}/explore/exploration.json",
            json.dumps(exploration, indent=2) + "\n",
        )
        return _agent_success({"output_files": list(allowed)})

    if schema_id.endswith("case-design.v1"):
        case_path = f"qa/changes/{change_id}/cases/system/user/case.yaml"
        sources_path = f"qa/changes/{change_id}/cases/system/user/assertion-sources.json"
        requirement_path = f"qa/changes/{change_id}/requirement.md"
        project_root = Path(context.project_root)
        requirement_bytes = (project_root / requirement_path).read_bytes()
        requirement_digest = hashlib.sha256(requirement_bytes).hexdigest()
        plans = list((project_root / "qa" / "changes" / change_id / "plan").rglob(
            "resolved-assurance-plan.json"
        ))
        if not plans:
            raise AssertionError("case-design requires the promoted resolved assurance plan")
        plan = json.loads(plans[0].read_text(encoding="utf-8"))
        machine = read_fixture("user-case.json")
        entry = _user_case_entry()
        entry["schema_version"] = machine["schema_version"]
        entry["revision"] = "1"
        entry["spec_digest"] = plan["requirement_digest"]
        entry["inputs"] = machine["inputs"]
        case_doc = {
            "schema_version": "1.0",
            "added": [entry],
            "modified": [],
            "removed": [],
        }
        sources = read_fixture("user-sources.json")
        sources["revision"] = "1"
        sources["spec_digest"] = plan["requirement_digest"]
        sources["sources"][0]["content_ref"] = {
            "path": requirement_path,
            "digest": requirement_digest,
        }
        files = {
            case_path: yaml.safe_dump(case_doc, sort_keys=False),
            sources_path: json.dumps(sources, indent=2) + "\n",
            f"qa/changes/{change_id}/.qa.yaml": yaml.safe_dump(
                {"schema_version": "1", "change_id": change_id}, sort_keys=True
            ),
            f"qa/changes/{change_id}/proposal.md": "# User create case\n",
            f"qa/changes/{change_id}/trace/minimum-coverage-matrix.json": json.dumps(
                [
                    {
                        "mrc_id": "MRC-USER-CREATE",
                        "key": "user.create",
                        "required": True,
                        "covered_by_cases": ["TC_USER_CREATE_001"],
                        "status": "covered",
                        "skip_reason": None,
                        "category": "api",
                        "layer": "api",
                    }
                ]
            )
            + "\n",
        }
        for relative, content in files.items():
            _write_logical(write_root, relative, content)
        return _agent_success({"output_files": list(allowed)})

    if schema_id.endswith("case-review.v1"):
        review = {
            "schema_version": "1.0",
            "review_type": "case",
            "change_id": change_id,
            "decision": "pass",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "proceed to plan",
            "auto_fix_allowed": False,
            "human_review_required": False,
            "risk_level": "medium",
            "minimum_coverage": {
                "total_required": 1,
                "covered": 1,
                "skipped_by_scope": 0,
                "missing": [],
            },
            "source_verification": {
                "independent": True,
                "reviewed_source_files": ["app/controllers/user.py"],
                "verified_claims": [
                    {
                        "claim": "UserController.create_user persists a User row",
                        "evidence_files": ["app/controllers/user.py"],
                    }
                ],
            },
        }
        _write_logical(
            write_root,
            f"qa/changes/{change_id}/review/case-review.json",
            json.dumps(review, indent=2) + "\n",
        )
        _write_logical(
            write_root,
            f"qa/changes/{change_id}/review/case-review-summary.md",
            "Case review passed.\n",
        )
        return _agent_success(review)

    if schema_id.endswith("plan.v1") and "plan-review" not in schema_id:
        plan_files = list(allowed)
        fixture = read_fixture("user-plan.json")
        for relative in plan_files:
            if relative.endswith("api-execution-bindings.json"):
                content = json.dumps(
                    {
                        "schema_version": fixture["schema_version"],
                        "case_id": fixture["case_id"],
                        "bindings": fixture["bindings"],
                    },
                    indent=2,
                )
            elif relative.endswith("api-codegen-mapping.json"):
                content = json.dumps(
                    {
                        "schema_version": "1",
                        "layer": "api",
                        "entries": [
                            {
                                "case_id": "TC_USER_CREATE_001",
                                "symbol": "test_tc_user_create_001__create",
                                "target_file": "tests/api/test_user_create.py",
                            }
                        ],
                    },
                    indent=2,
                )
            else:
                content = "plan\n"
            _write_logical(write_root, relative, content)
        plan = {
            "schema_version": "1",
            "family": "api",
            "change_id": change_id,
            "case_ids": ["TC_USER_CREATE_001"],
            "required_capabilities": ["entities.user"],
            "coverage": [
                {
                    "case_id": "TC_USER_CREATE_001",
                    "operation": "create",
                    "risk": "high",
                    "required_capabilities": ["entities.user"],
                }
            ],
            "output_files": list(allowed),
        }
        return _agent_success(plan)

    if schema_id.endswith("plan-review.v1"):
        review = {
            "schema_version": "1.0",
            "review_type": "api-plan",
            "change_id": change_id,
            "decision": "pass",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "proceed to codegen",
            "auto_fix_allowed": False,
            "human_review_required": False,
            "codegen_readiness": "ready",
            "risk_level": "medium",
            "required_capabilities": ["entities.user"],
        }
        _write_logical(
            write_root,
            f"qa/changes/{change_id}/review/api-plan-review.json",
            json.dumps(review, indent=2) + "\n",
        )
        _write_logical(
            write_root,
            f"qa/changes/{change_id}/review/api-plan-review-summary.md",
            "API plan review passed.\n",
        )
        return _agent_success(review)

    if schema_id.endswith("codegen.v1"):
        from agent_runtime_contracts.schema import thaw_json
        from assurance_generation.contracts.admission import _expected_bridge_bytes
        from assurance_generation.contracts.codegen import CodegenMapping
        from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1

        context_payload: dict[str, object] = {}
        for part in agent_run.instructions:
            payload = thaw_json(part.json_content)
            if isinstance(payload, dict) and "verified_codegen" in payload:
                context_payload = payload
                break
        verified = context_payload.get("verified_codegen")
        if not isinstance(verified, dict):
            raise AssertionError("codegen request missing verified_codegen context")
        machine_ref = verified["case_execution_plan_ref"]
        machine = CaseExecutionPlanSetV1.model_validate_json(
            (Path(context.project_root) / machine_ref["path"]).read_bytes()
        )
        mapping = CodegenMapping.model_validate(
            {
                "schema_version": "1",
                "layer": "api",
                "entries": [
                    {
                        "case_id": "TC_USER_CREATE_001",
                        "symbol": "test_tc_user_create_001__create",
                        "target_file": "tests/api/test_user_create.py",
                    }
                ],
                "validation_profile": verified["validation_profile"],
                "coverage_epoch": verified["coverage_epoch"],
                "plan_digest": verified["plan_digest"],
                "plan_ref": verified["plan_ref"],
                "reviewed_case": verified["reviewed_case"],
                "case_execution_plan_ref": machine_ref,
                "case_execution_plan_digest": verified["case_execution_plan_digest"],
                "case_spec_digests": {item.case_id: item.spec_digest for item in machine.cases},
            }
        )
        staged = (
            f"qa/changes/{change_id}/generated/api/files/tests/api/test_user_create.py"
        )
        _write_logical(
            write_root,
            staged,
            _expected_bridge_bytes(mapping, "tests/api/test_user_create.py"),
        )
        authoring = {
            "schema_version": "1",
            "change_id": change_id,
            "layer": "api",
            "files": [
                {
                    "repo_path": "tests/api/test_user_create.py",
                    "disposition": "generated",
                    "role": "test_entry",
                    "case_ids": ["TC_USER_CREATE_001"],
                }
            ],
            "mapping": mapping.model_dump(mode="json"),
            "required_capabilities": ["entities.user"],
        }
        _write_logical(
            write_root,
            f"qa/changes/{change_id}/codegen/api-generated-files.json",
            json.dumps(authoring, indent=2) + "\n",
        )
        return _agent_success(authoring)

    raise AssertionError(f"unhandled scripted schema: {schema_id} allowed={allowed}")


def test_installed_full_assembles_empty_change_through_codegen(
    tmp_path, installed_sources, monkeypatch
):
    from agent_runtime_opencode.discovery import agent_run_from_request
    from agent_runtime_opencode.handler import OpenCodeHandler
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.cli import _authorization
    from assurance_product.product import prepare_change_workspace
    from assurance_product.runtime_bindings import _HostBackedInstalledPhase, _task_context

    project, item, composition, _config_tree = _installed_user_sources(tmp_path, installed_sources)
    change_id = "CH-USER-FULL-001"
    input_path = tmp_path / "input.json"
    _write_full_input(input_path, composition, change_id=change_id)
    requests = []
    paths = _assembly_paths(project, change_id)
    assert not paths["requirement"].exists()

    async def in_process_invoke(self, request, scope):
        return await self._handler.execute(request, _task_context(scope))

    async def scripted_execute(self, request, context):
        del self
        agent_run = agent_run_from_request(request)
        requests.append(agent_run)
        return _scripted_opencode_execute(request, context)

    monkeypatch.setattr(_HostBackedInstalledPhase, "_invoke", in_process_invoke)
    monkeypatch.setattr(OpenCodeHandler, "execute", scripted_execute)
    monkeypatch.setenv(SECRET_ENV, "local-script-token")
    authorization = _authorization([f"opencode.token=env:{SECRET_ENV}", *_host_secret_values(item)])
    workspace = prepare_change_workspace(project, change_id)
    run_error = None
    try:
        result, mapped, code = AssuranceProductApplication().run(
            project_dir=project,
            change_id=change_id,
            invocation_id=change_id,
            composition=composition,
            authorization=authorization,
            entrypoint="full",
            input_path=input_path,
            workspace=workspace,
            secrets=(),
        )
    except Exception as error:
        run_error = error
        if not requests:
            raise AssertionError(f"installed run failed before first agent request: {error!r}") from error
        result, mapped, code = None, type(error).__name__, 40
    if not requests:
        raise AssertionError(f"no agent request; mapped={mapped} code={code} result={result!r}")
    assert requests[0].workspace.write_root.startswith(f"qa/changes/{change_id}/.staging/")
    schemas = [item.result_contract.schema_id for item in requests]
    status_path = project / "qa" / "changes" / change_id / "status.json"
    status_text = status_path.read_text(encoding="utf-8") if status_path.is_file() else "no-status"
    result_text = repr(result)[:800] if result is not None else "no-result"
    change_files = sorted(
        path.relative_to(project).as_posix()
        for path in (project / "qa" / "changes" / change_id).rglob("*")
        if path.is_file()
    )
    detail = (
        f"mapped={mapped} code={code} schemas={schemas} error={run_error!r} "
        f"result={result_text} status={status_text[:800]} files={change_files[:40]}"
    )
    for name in (
        "requirement",
        "exploration",
        "case",
        "sources",
        "review",
        "bindings",
        "machine_plan",
        "codegen",
    ):
        assert paths[name].is_file(), f"missing assembled {name}: {detail}"
    assert not list(project.rglob("**/qualification*"))
    process_receipts = list(project.rglob("**/owned-process.json")) + list(
        project.rglob("**/verified-process*.json")
    )
    assert process_receipts, f"did not reach real subprocess execution: {detail}"
