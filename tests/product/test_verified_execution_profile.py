from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("installed_sources")


def test_execution_semantics_use_fixed_task_facades(opencode_composition):
    from assurance_execution.contracts.attempts import TASK_ATTEMPT_CONTRACTS
    from assurance_product.agent_contracts import all_feature_agent_contracts, all_feature_task_contracts

    expected = {"assurance.execution.task.execute.v1", "assurance.execution.task.run.v1"}
    assert set(TASK_ATTEMPT_CONTRACTS) == expected
    assert set(opencode_composition.semantic_attempt_contracts) == (
        set(all_feature_agent_contracts())
        | {contract.contract_id for contract in all_feature_task_contracts().values()}
    )
    for name in expected:
        resolved = opencode_composition.semantic_attempt_contracts[name]
        assert resolved.executor.backend == "legacy_raw_agent"
        assert resolved.contract.retry.max_attempts == 1
        assert resolved.contract.timeout.seconds >= 60
        assert resolved.contract.resources.writes == (
            "qa/changes/{change_id}/.staging/execution",
            "qa/changes/{change_id}/execution",
            "qa/changes/{change_id}/execution/" + name.split(".")[-2] + "-result.json",
        )


def test_unknown_profile_rejected_by_deployment_and_input():
    from assurance_product.models import DeploymentBindingsV1, ProductInputV1

    assert "validation_profile" in DeploymentBindingsV1.model_fields
    assert "validation_profile" in ProductInputV1.model_fields
    from assurance_product.verification_execution import execution_backend

    with pytest.raises(ValueError, match="unknown validation profile"):
        execution_backend("automatic")


def test_legacy_facade_dependency_preflights_opencode(opencode_composition):
    from assurance_product.runtime_ports import _preflight_selected_root
    from graph_engine.attempts.secret_sources import empty_runtime_authorization

    from graph_engine.attempts.secret_sources import RuntimeAuthorizationError

    with pytest.raises((ValueError, RuntimeAuthorizationError), match="OpenCode|secret handle"):
        _preflight_selected_root(
            opencode_composition,
            empty_runtime_authorization(),
            ("assurance.execution.task.execute.v1",),
        )


@pytest.fixture(params=["api_db.v1", "api_db_trace.v1"])
def profiled_composition(request, installed_sources, tmp_path):
    import sys
    import yaml
    from pathlib import Path
    from assurance_product.binding_builder import build_deployment_wheel
    from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition
    from graph_engine.composition import WheelPluginSource
    from tests.product.composition_harness import _extract_wheel

    document = yaml.safe_load(Path("tests/product/fixtures/deployment/opencode.yaml").read_text())
    document["validation_profile"] = request.param
    document["verification_host"] = {"sut_source_root": "/installed/sut-source"}
    manifest = tmp_path / "deployment.yaml"
    manifest.write_text(yaml.safe_dump(document))
    wheel = build_deployment_wheel(manifest, tmp_path / "wheel")
    extracted = _extract_wheel(wheel.wheel, tmp_path / "installed")
    try:
        yield resolve_assurance_composition(
            AssuranceCompositionRequest(
                product_entrypoint="assurance-opencode",
                deployment_source=WheelPluginSource(
                    distribution=wheel.distribution,
                    entrypoint_name="deployment",
                    declaration_path=wheel.declaration_path,
                ),
                configuration_tree=installed_sources.configuration_tree,
            )
        )
    finally:
        sys.path.remove(str(extracted))
        for name in tuple(sys.modules):
            if name == wheel.import_package or name.startswith(wheel.import_package + "."):
                sys.modules.pop(name, None)


def test_profiles_boot_same_facade_schema_and_change_lock_revision(
    profiled_composition, opencode_composition
):
    from assurance_product.product import product_graph_manifest
    from assurance_product.verification_execution import FACADE_DELEGATES, verification_configuration
    from assurance_product.runtime_bindings import runtime_bindings_from_composition

    config, digest = verification_configuration(profiled_composition)
    assert config.validation_profile in {"api_db.v1", "api_db_trace.v1"}
    assert config.host.sut_source_root == "/installed/sut-source"
    assert len(digest) == 64
    assert profiled_composition.lock.digest != opencode_composition.lock.digest
    assert (
        product_graph_manifest(profiled_composition, profiled_composition.lock).revision
        != product_graph_manifest(opencode_composition, opencode_composition.lock).revision
    )
    assert set(runtime_bindings_from_composition(profiled_composition)) == set(
        runtime_bindings_from_composition(opencode_composition)
    )
    for name in FACADE_DELEGATES:
        selected = profiled_composition.semantic_attempt_contracts[name]
        assert selected.executor.backend == "verified_host"
        assert selected.executor._legacy is None
        assert (
            selected.canonical_projection()
            == opencode_composition.semantic_attempt_contracts[name].canonical_projection()
        )
    quality = profiled_composition.semantic_attempt_contracts[
        "assurance.quality.materialize-assessment-inputs"
    ]
    assert type(quality.executor).__name__ == "ProfiledAssessmentExecutor"
    assert quality.executor.backend == "verified_host"
    assert quality.executor._legacy is None
    expected_handles = (
        ()
        if config.host.managed_sut_authority_handle is None
        else (config.host.managed_sut_authority_handle,)
    )
    assert quality.executor._phase._secret_handles == expected_handles
    assert (
        quality.canonical_projection()
        == opencode_composition.semantic_attempt_contracts[
            "assurance.quality.materialize-assessment-inputs"
        ].canonical_projection()
    )


def test_profiles_without_prerequisites_are_not_ready(profiled_composition):
    from assurance_product.runtime_ports import _preflight_selected_root, _requires_opencode
    from graph_engine.attempts.secret_sources import empty_runtime_authorization

    assert not _requires_opencode(profiled_composition, ("assurance.execution.task.execute.v1",))
    assert _requires_opencode(
        profiled_composition, ("assurance.execution.task.execute.v1", "assurance.intake.agent.intake.v1")
    )
    with pytest.raises(ValueError, match="NOT_READY"):
        _preflight_selected_root(
            profiled_composition, empty_runtime_authorization(), ("assurance.execution.task.execute.v1",)
        )


def test_profile_mismatch_rejects_before_delegate(profiled_composition):
    import asyncio
    from assurance_execution.contracts.agent import ExecutionPrepareInputV1
    from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

    value = ExecutionPrepareInputV1(
        change_id="c",
        plan_digest="a" * 64,
        plan_ref=EvidenceArtifactRefV1(path="qa/changes/c/plan.json", digest="b" * 64),
        selected_test_families=("api",),
        capability_leafs=(),
    )
    executor = profiled_composition.semantic_attempt_contracts["assurance.execution.task.execute.v1"].executor
    with pytest.raises(ValueError, match="validation profile"):
        asyncio.run(executor.execute(value, None))
    with pytest.raises(ValueError, match="validation profile"):
        asyncio.run(executor.reconcile(value, None, None))


@pytest.mark.parametrize("profile", ["api_db.v1", "api_db_trace.v1"])
def test_authenticated_host_prerequisites_are_profile_specific(tmp_path, monkeypatch, profile):
    import json
    from assurance_product.verification_execution import VerificationConfiguration, preflight_verification
    from assurance_execution.operations.verified_process import DockerVerificationHost
    from graph_engine.attempts.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )

    run = tmp_path / "run"
    run.mkdir()
    authority = {
        "kind": "user-invocation-host.v1",
        "authority_root": str(run),
        "fault": "none",
        "sut_base_url": "http://127.0.0.1:1",
        "sqlite_path": str(run / "missing.sqlite3"),
        "instance_id": "missing",
    }
    monkeypatch.setenv("AA_TEST_PREFLIGHT_AUTHORITY", json.dumps(authority))
    monkeypatch.setenv("AA_TEST_PREFLIGHT_CREDENTIAL", json.dumps({"token": "test", "user_password": "test"}))
    monkeypatch.setenv(
        "AA_TEST_PREFLIGHT_COLLECTOR", json.dumps({"collector_ready": True, "otel_ready": True})
    )
    sources = tuple(
        SecretSourceBinding(handle, "environment", locator)
        for handle, locator in (
            ("sut.authority", "AA_TEST_PREFLIGHT_AUTHORITY"),
            ("sut.credential", "AA_TEST_PREFLIGHT_CREDENTIAL"),
            ("sut.collector", "AA_TEST_PREFLIGHT_COLLECTOR"),
        )
    )
    authorization = InvocationRuntimeAuthorization(
        schema_version="1", secret_sources=sources, digest=runtime_authorization_digest(sources)
    )

    config = VerificationConfiguration.model_validate(
        {
            "validation_profile": profile,
            "host": {
                "sut_source_root": str(tmp_path),
                "managed_sut_authority_handle": "sut.authority",
                "credential_handle": "sut.credential",
            },
        }
    )
    with pytest.raises(ValueError, match="NOT_READY"):
        preflight_verification(config, authorization)
    monkeypatch.setattr(
        DockerVerificationHost, "preflight", lambda self: pytest.fail("unexpected OCI dependency")
    )
    if profile == "api_db_trace.v1":
        with pytest.raises(ValueError, match="NOT_READY"):
            preflight_verification(config, authorization)
        config = config.model_copy(
            update={"host": config.host.model_copy(update={"collector_readiness_handle": "sut.collector"})}
        )
    # An authority token in an empty run directory cannot establish readiness.
    with pytest.raises(ValueError, match="NOT_READY"):
        preflight_verification(config, authorization)


def test_legacy_facade_calls_existing_raw_executor(opencode_composition, monkeypatch, tmp_path):
    import asyncio
    from agent_runtime_contracts import ResolvedRawAgentExecutor
    from graph_engine.attempts import PermanentTaskFailure
    from assurance_execution.contracts.agent import ExecutionPrepareInputV1
    from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
    from tests.acg_plan_fixture import install_plan
    from types import SimpleNamespace

    plan, ref = install_plan(tmp_path, "c")

    seen = []

    async def raw(self, value, scope):
        seen.append(self)
        return PermanentTaskFailure(kind="invalid_input", message="legacy sentinel")

    monkeypatch.setattr(ResolvedRawAgentExecutor, "execute", raw)
    facade = opencode_composition.semantic_attempt_contracts["assurance.execution.task.execute.v1"].executor
    value = ExecutionPrepareInputV1(
        change_id="c",
        plan_digest=plan.plan_digest,
        plan_ref=EvidenceArtifactRefV1.model_validate(ref),
        selected_test_families=("api",),
        capability_leafs=(),
    )
    result = asyncio.run(
        facade.execute(value, SimpleNamespace(workspace=SimpleNamespace(project_root=tmp_path)))
    )
    assert result.message == "legacy sentinel"
    assert len(seen) == 1 and isinstance(seen[0], ResolvedRawAgentExecutor)


def test_legacy_facade_preserves_finalize_phase_write_claims(opencode_composition, tmp_path):
    import asyncio
    from graph_engine.attempts import AttemptExecutionContext, AttemptKey, AuthorizedAttemptScope
    from graph_engine.attempts.workspace import TaskWorkspaceStore
    from graph_engine.plugin_api import TaskOutcome

    facade = opencode_composition.semantic_attempt_contracts["assurance.execution.task.execute.v1"]
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    try:
        workspace = store.begin(
            task_id="a" * 64,
            attempt=1,
            output_paths=facade.contract.resources.resolve({"change_id": "c"}).writes,
        )
        scope = AuthorizedAttemptScope(
            execution=AttemptExecutionContext(
                invocation_id="inv",
                public_entrypoint="execute",
                semantic_node_id="execution.execute",
                attempt_key=AttemptKey(digest="a" * 64),
                fencing_token=1,
            ),
            workspace=workspace,
        )

        async def finalize():
            path = workspace.write_root / "qa/changes/c/execution/execute-result.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")
            return TaskOutcome.succeeded({"legacy": True})

        outcome = asyncio.run(facade.executor._legacy._run_phase("finalize", finalize, scope))
        assert isinstance(outcome, TaskOutcome)

        async def forged_fact():
            path = workspace.write_root / "qa/changes/c/execution/forged-facts.json"
            path.write_text("{}")
            return TaskOutcome.succeeded({"legacy": True})

        rejected = asyncio.run(facade.executor._legacy._run_phase("finalize", forged_fact, scope))
        assert rejected.kind == "invalid_output"
        assert "forged-facts.json" in rejected.message
    finally:
        store.close()


def test_sut_source_root_is_independent_and_runner_configuration_is_rejected():
    from assurance_product.models import VerificationHostConfigV1

    assert "sut_source_root" in VerificationHostConfigV1.model_fields
    host = VerificationHostConfigV1(sut_source_root="/installed/sut-source")
    assert host.sut_source_root == "/installed/sut-source"
    for field in ("runner", "qualification_path", "qualification_digest"):
        assert field not in VerificationHostConfigV1.model_fields
        with pytest.raises(ValueError):
            VerificationHostConfigV1.model_validate({field: "obsolete"})
    import json
    from importlib.resources import files

    schema = json.loads(
        files("assurance_product").joinpath("resources/declarations/deployment.schema.json").read_text()
    )
    assert schema["properties"]["verification_host"] == VerificationHostConfigV1.model_json_schema()


def test_legacy_facade_rejects_verified_root_plan(opencode_composition, tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from assurance_execution.contracts.agent import ExecutionPrepareInputV1
    from agent_runtime_contracts import ResolvedRawAgentExecutor
    from graph_engine.attempts import PermanentTaskFailure
    from tests.acg_plan_fixture import install_plan

    plan, ref = install_plan(
        tmp_path,
        "c",
        verification_policy={
            "validation_profile": "api_db.v1",
            "resource_id": "assurance.product.configuration.verification-policy",
            "digest": "a" * 64,
        },
    )
    value = ExecutionPrepareInputV1.model_validate(
        {
            "change_id": "c",
            "plan_digest": plan.plan_digest,
            "plan_ref": ref,
            "selected_test_families": ["api"],
            "capability_leafs": ["entities.item.constraints.name"],
        }
    )

    async def raw(*args):
        return PermanentTaskFailure(kind="invalid_input", message="must not reach raw delegate")

    monkeypatch.setattr(ResolvedRawAgentExecutor, "execute", raw)
    scope = SimpleNamespace(workspace=SimpleNamespace(project_root=tmp_path))
    facade = opencode_composition.semantic_attempt_contracts["assurance.execution.task.execute.v1"].executor
    with pytest.raises(ValueError, match="root plan validation profile"):
        asyncio.run(facade.execute(value, scope))
