from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from agent_runtime_contracts import AgentRunRequest
from graph_engine.plugin_api import ResourceClaimTemplate
from graph_engine.plugin_api import InvocationMetadata, TaskContext, TaskRequest
from graph_engine.attempts.workspace import TaskWorkspaceStore

pytestmark = pytest.mark.usefixtures("installed_sources")


EXPECTED_AGENT_PROFILES = {
    "assurance.intake.agent.case-design.v1": "assurance-v1-doc-author",
    "assurance.intake.agent.case-review.v1": "assurance-v1-reviewer",
    "assurance.intake.agent.explore.v1": "assurance-v1-explorer",
    "assurance.intake.agent.intake.v1": "assurance-v1-doc-author",
    "assurance.generation.agent.api.codegen-fix.v1": "assurance-v1-test-author",
    "assurance.generation.agent.api.codegen.v1": "assurance-v1-test-author",
    "assurance.generation.agent.api.plan-review.v1": "assurance-v1-reviewer",
    "assurance.generation.agent.api.plan.v1": "assurance-v1-doc-author",
    "assurance.generation.agent.e2e.codegen-fix.v1": "assurance-v1-test-author",
    "assurance.generation.agent.e2e.codegen.v1": "assurance-v1-test-author",
    "assurance.generation.agent.e2e.plan-review.v1": "assurance-v1-reviewer",
    "assurance.generation.agent.e2e.plan.v1": "assurance-v1-doc-author",
    "assurance.generation.agent.fuzz.codegen.v1": "assurance-v1-test-author",
    "assurance.generation.agent.fuzz.plan-review.v1": "assurance-v1-reviewer",
    "assurance.generation.agent.fuzz.plan.v1": "assurance-v1-doc-author",
    "assurance.generation.agent.performance.codegen.v1": "assurance-v1-test-author",
    "assurance.generation.agent.performance.plan-review.v1": "assurance-v1-reviewer",
    "assurance.generation.agent.performance.plan.v1": "assurance-v1-doc-author",
    "assurance.execution.agent.execute.v1": "assurance-v1-executor",
    "assurance.execution.agent.run.v1": "assurance-v1-executor",
    "assurance.healing.agent.coverage-repair.v1": "assurance-v1-test-author",
    "assurance.healing.agent.fix-proposal.v1": "assurance-v1-doc-author",
    "assurance.healing.agent.apply-test-repair.v1": "assurance-v1-test-author",
    "assurance.quality.agent.fact-baseline.v1": "assurance-v1-doc-author",
    "assurance.quality.agent.inspect.v1": "assurance-v1-reviewer",
    "assurance.quality.agent.issue-analysis.v1": "assurance-v1-reporter",
    "assurance.quality.agent.issue-triage.v1": "assurance-v1-reporter",
    "assurance.quality.agent.report.v1": "assurance-v1-reporter",
    "assurance.improvement.agent.archive.v1": "assurance-v1-archiver",
    "assurance.improvement.agent.improvement-review.v1": "assurance-v1-reviewer",
    "assurance.improvement.agent.retro-eval-analysis.v1": "assurance-v1-doc-author",
    "assurance.improvement.agent.retro-issue-analysis.v1": "assurance-v1-doc-author",
    "assurance.improvement.agent.retro-workflow-analysis.v1": "assurance-v1-doc-author",
    "assurance.improvement.agent.retro.v1": "assurance-v1-doc-author",
}


def test_opencode_agent_installation_is_complete_noninteractive_and_idempotent(tmp_path) -> None:
    from assurance_product.opencode_agents import bounded_agent_profiles, install_opencode_agents

    project = tmp_path / "project"
    project.mkdir()
    first = install_opencode_agents(project)
    second = install_opencode_agents(project)
    assert first == second
    assert first == (
        project / "opencode.json",
        project / ".opencode" / "plugins" / "assurance-boundary.mjs",
    )
    assert first[1].is_file()
    config = json.loads(first[0].read_text(encoding="utf-8"))
    assert config["plugin"] == ["./.opencode/plugins/assurance-boundary.mjs"]
    assert set(config["agent"]) == set(bounded_agent_profiles())
    for profile, metadata in config["agent"].items():
        assert metadata["description"].endswith(profile + ".")
        assert metadata["tools"]["question"] is False
        assert metadata["tools"]["skill"] is False
        mutation = profile != "assurance-v1-archiver"
        assert metadata["tools"]["write"] is mutation
        assert metadata["tools"]["artifact_write"] is mutation
        assert metadata["tools"]["apply_patch"] is mutation
        assert metadata["tools"]["todowrite"] is False
        assert metadata["tools"]["lsp_diagnostics"] is False
        assert metadata["tools"]["ast_grep_search"] is False
        assert metadata["permission"]["external_directory"] == "deny"
        assert metadata["permission"]["edit"]["**"] == "deny"
        assert "Do not ask questions" in metadata["prompt"]
        assert "current session directory is the complete project root" in metadata["prompt"]
        assert "`apply_patch` is allowed only" in metadata["prompt"]
        assert "Never invoke write, edit, artifact_write, or apply_patch in parallel" in metadata["prompt"]
        assert "native write tool" in metadata["prompt"]
    explorer = config["agent"]["assurance-v1-explorer"]
    assert explorer["permission"]["bash"] == {"*": "deny"}
    assert explorer["tools"]["bash"] is False
    assert "Shell commands and legacy aa commands are disabled" in explorer["prompt"]
    for author_or_reviewer in (
        "assurance-v1-doc-author",
        "assurance-v1-test-author",
        "assurance-v1-reviewer",
    ):
        profile = config["agent"][author_or_reviewer]
        assert profile["permission"]["bash"] == {"*": "deny"}
        assert profile["tools"]["bash"] is False
    archiver = config["agent"]["assurance-v1-archiver"]
    assert archiver["permission"]["bash"] == {"*": "deny"}
    assert archiver["tools"]["bash"] is False
    assert archiver["tools"]["write"] is False
    assert archiver["tools"]["apply_patch"] is False
    assert set(archiver["permission"]["edit"]) == {
        "**",
        "**qa/changes/**/explore/context.json",
        "**qa/changes/**/workflow-state.json",
        "**qa/changes/**/workflow-state.yaml",
    }
    assert all(value == "deny" for value in archiver["permission"]["edit"].values())
    doc_author = config["agent"]["assurance-v1-doc-author"]
    assert doc_author["tools"]["apply_patch"] is True
    assert "tool literally named `write` is available" in doc_author["prompt"]
    executor = config["agent"]["assurance-v1-executor"]
    execution_view = "qa/changes/*/.staging/execution/*"
    pytest_command = (
        "PYTHONDONTWRITEBYTECODE=1 "
        "HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-* "
        f"uv run --isolated pytest -p no:cacheprovider --rootdir {execution_view} *"
    )
    assert executor["permission"]["bash"][pytest_command] == "allow"
    assert (
        executor["permission"]["bash"][
            f"PYTHONDONTWRITEBYTECODE=1 uv run --isolated locust --locustfile {execution_view} *"
        ]
        == "allow"
    )
    assert all(execution_view in command for command in executor["permission"]["bash"] if command != "*")
    assert "uv run --isolated pytest *" not in executor["permission"]["bash"]
    assert "uv run --isolated locust *" not in executor["permission"]["bash"]
    assert "pytest *" not in executor["permission"]["bash"]
    assert "python -m pytest *" not in executor["permission"]["bash"]
    assert "uv run pytest *" not in executor["permission"]["bash"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_opencode_boundary_confines_apply_patch_to_agent_write_surface(tmp_path: Path) -> None:
    from assurance_product.opencode_agents import install_opencode_agents, workspace_binding_title

    project = tmp_path / "project"
    project.mkdir()
    _config, plugin = install_opencode_agents(project)
    driver = r"""
import { pathToFileURL } from "node:url";
const pluginPath = process.argv[1];
const payload = JSON.parse(process.argv[2]);
const { default: createPlugin } = await import(pathToFileURL(pluginPath).href);
const hooks = await createPlugin({
  client: { session: { get: async () => ({ data: payload.session }) } },
});
const output = { args: { patchText: payload.patchText } };
try {
  await hooks["tool.execute.before"](
    { tool: "apply_patch", sessionID: payload.session.id, callID: "call-test" },
    output,
  );
  process.stdout.write(JSON.stringify(output.args) + "\n");
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 23;
}
"""

    def run(agent: str, target: str, allowed: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        patch_text = f"*** Begin Patch\n*** Update File: {target}\n@@\n-old\n+new\n*** End Patch"
        title = workspace_binding_title(
            session_id="ses-test",
            agent_profile=agent,
            project_root=project,
            write_root="qa/changes/CH-1/.staging/task-1/attempt-1",
            allowed_outputs=allowed,
            task_id="task-1",
            attempt=1,
            attempt_id="attempt-1",
        )
        return subprocess.run(
            [
                shutil.which("node") or "node",
                "--input-type=module",
                "-e",
                driver,
                str(plugin),
                json.dumps(
                    {
                        "session": {
                            "id": "ses-test",
                            "directory": str(project.resolve()),
                            "agent": agent,
                            "title": title,
                        },
                        "patchText": patch_text,
                    }
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    allowed = ("qa/changes/CH-1/requirement.md",)
    redirected = run("assurance-v1-doc-author", "qa/changes/CH-1/requirement.md", allowed)
    assert redirected.returncode == 0
    assert "qa/changes/CH-1/.staging/task-1/attempt-1/qa/changes/CH-1/requirement.md" in redirected.stdout
    denied = run("assurance-v1-test-author", "tests/e2e/test_dept.py", ("qa/changes/CH-1/codegen/out.py",))
    assert denied.returncode == 23
    assert "path is not allowed" in denied.stderr
    denied = run(
        "assurance-v1-explorer", "qa/changes/CH-1/explore/context.json", ("qa/changes/CH-1/explore/notes.md",)
    )
    assert denied.returncode == 23
    assert "path is not allowed" in denied.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_opencode_boundary_confines_read_search_paths_to_session(tmp_path: Path) -> None:
    from assurance_product.opencode_agents import install_opencode_agents, workspace_binding_title

    project = tmp_path / "project"
    project.mkdir()
    (project / "inside.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    _config, plugin = install_opencode_agents(project)
    title = workspace_binding_title(
        session_id="ses-test",
        agent_profile="assurance-v1-explorer",
        project_root=project,
        write_root="qa/changes/CH-1/.staging/task-1/attempt-1",
        allowed_outputs=("qa/changes/CH-1/explore/notes.md",),
        task_id="task-1",
        attempt=1,
        attempt_id="attempt-1",
    )
    driver = r"""
import { pathToFileURL } from "node:url";
const pluginPath = process.argv[1];
const payload = JSON.parse(process.argv[2]);
const { default: createPlugin } = await import(pathToFileURL(pluginPath).href);
const hooks = await createPlugin({
  client: { session: { get: async () => ({ data: payload.session }) } },
});
try {
  await hooks["tool.execute.before"](
    { tool: payload.tool, sessionID: payload.session.id, callID: "call-test" },
    { args: payload.args },
  );
  process.stdout.write("ALLOW\n");
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 23;
}
"""

    def run(tool: str, args: Mapping[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                shutil.which("node") or "node",
                "--input-type=module",
                "-e",
                driver,
                str(plugin),
                json.dumps(
                    {
                        "session": {
                            "id": "ses-test",
                            "directory": str(project.resolve()),
                            "agent": "assurance-v1-explorer",
                            "title": title,
                        },
                        "tool": tool,
                        "args": args,
                    }
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    assert run("read", {"filePath": "inside.txt"}).returncode == 0
    assert run("glob", {"pattern": "**/*.py"}).returncode == 0
    assert run("glob", {"pattern": "**/*.py", "path": "."}).returncode == 0
    assert run("grep", {"pattern": "inside", "path": "."}).returncode == 0
    for tool, args in (
        ("read", {"filePath": str(outside)}),
        ("glob", {"pattern": "**/*", "path": str(tmp_path)}),
        ("grep", {"pattern": "secret", "path": ".."}),
    ):
        denied = run(tool, args)
        assert denied.returncode == 23
        assert "not allowed" in denied.stderr or "path escapes" in denied.stderr


def test_opencode_agent_installation_rejects_conflicting_project_profile(tmp_path) -> None:
    from assurance_product.opencode_agents import install_opencode_agents

    project = tmp_path / "project"
    project.mkdir()
    (project / "opencode.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="already has different content"):
        install_opencode_agents(project)


def test_feature_owned_agent_job_catalogs_are_provider_neutral() -> None:
    from assurance_product.agent_contracts import (
        FEATURE_AGENT_JOB_CATALOGS,
        all_feature_agent_contracts,
    )
    from assurance_product.models import all_binding_ids

    all_contracts = [contract for catalog in FEATURE_AGENT_JOB_CATALOGS for contract in catalog.values()]
    assert sum(len(catalog) for catalog in FEATURE_AGENT_JOB_CATALOGS) == 34
    assert len(all_feature_agent_contracts()) == 34
    assert set(all_feature_agent_contracts()) == set(all_binding_ids())
    assert all(not hasattr(contract, "requires_provider_schema") for contract in all_contracts)
    assert all(
        "opencode" not in json.dumps(contract.canonical_projection()).lower() for contract in all_contracts
    )
    assert all(
        "cursor" not in json.dumps(contract.canonical_projection()).lower() for contract in all_contracts
    )
    assert all(hasattr(contract, "contract_id") for contract in all_contracts)
    assert not any(item.startswith("assurance.product.agent.") for item in all_binding_ids())


def test_semantic_contract_ids_are_the_feature_job_union() -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS, FEATURE_AGENT_JOB_CATALOGS
    from assurance_product.models import all_binding_ids

    derived = []
    for catalog in FEATURE_AGENT_JOB_CATALOGS:
        for contract in catalog.values():
            body = contract.contract_id.removeprefix("assurance.").removesuffix(".v1")
            feature, marker, base = body.partition(".agent.")
            assert marker == ".agent."
            derived.append(contract.contract_id)
    assert set(derived) == set(AGENT_EXECUTION_CONTRACTS) == set(EXPECTED_AGENT_PROFILES)
    assert set(all_binding_ids()) == set(AGENT_EXECUTION_CONTRACTS)


def test_all_agent_skills_have_one_bound_agent_and_execution_contract(installed_sources) -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from assurance_product.binding_builder import _binding_documents
    from assurance_product.models import DeploymentBindingsV1

    assert set(EXPECTED_AGENT_PROFILES) == set(AGENT_EXECUTION_CONTRACTS)
    fixture = Path(__file__).parent / "fixtures" / "deployment" / "opencode.yaml"
    bindings = DeploymentBindingsV1.model_validate(yaml.safe_load(fixture.read_text(encoding="utf-8")))
    binding_documents = {
        str(document["capability_id"]): document for document in _binding_documents(bindings)
    }
    assert set(binding_documents) == set(AGENT_EXECUTION_CONTRACTS)
    for contract_id, expected_agent in EXPECTED_AGENT_PROFILES.items():
        binding_data = binding_documents[contract_id]["data"]
        assert isinstance(binding_data, Mapping)
        assert binding_data["agent_profile"] == expected_agent
        contract = AGENT_EXECUTION_CONTRACTS[contract_id]
        assert contract.agent_profile == expected_agent
        assert contract.resources.writes


def test_agent_execute_contracts_render_exact_current_change_output_claims() -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from assurance_product.output_routes import OutputRouteCatalog

    change_id = "CH-CURRENT-001"
    catalog = OutputRouteCatalog()
    forbidden_prefixes = (
        "qa/changes",
        "tests",
        "qa/retro",
        "qa/improvements",
        "qa/archive",
        "qa/cases",
    )
    extra_claims = {
        "assurance.intake.agent.case-design.v1": (f"qa/changes/{change_id}/cases",),
        "assurance.intake.agent.case-review.v1": (f"qa/changes/{change_id}/cases/reviewed-case.json",),
        "assurance.intake.agent.explore.v1": (f"qa/changes/{change_id}/explore/context.json",),
    }
    for contract_id, contract in AGENT_EXECUTION_CONTRACTS.items():
        assert isinstance(contract.resources, ResourceClaimTemplate)
        assert contract.resources.parameters == {"change_id": "/workspace/scope_id"}
        resolved = contract.resources.resolve({"workspace": {"scope_id": change_id}})
        extra = extra_claims.get(contract_id, ())
        body = contract_id.removeprefix("assurance.").removesuffix(".v1")
        feature, _, rest = body.partition(".agent.")
        family, _, job = rest.rpartition(".")
        if feature == "generation" and job in {"codegen", "codegen-fix"}:
            extra = (*extra, f"qa/changes/{change_id}/generated/{family}/files")
        outputs = catalog.outputs(contract_id, change_id)
        assert outputs == tuple(path for path in resolved.writes if path not in extra)
        assert extra == tuple(path for path in resolved.writes if path not in outputs)
        assert all(path.startswith(f"qa/changes/{change_id}/") for path in resolved.writes)
        assert all("/.runtime/" not in path and "/.staging/" not in path for path in resolved.writes)
        assert all(path not in forbidden_prefixes for path in resolved.writes)


def test_exact_current_change_claims_do_not_scan_a_symlinked_sibling_on_promotion(tmp_path: Path) -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS

    project = tmp_path / "project"
    current_id = "CH-CURRENT-001"
    sibling_id = "CH-HISTORICAL-001"
    current = project / "qa" / "changes" / current_id
    sibling = project / "qa" / "changes" / sibling_id
    current.mkdir(parents=True)
    sibling.mkdir(parents=True)
    outside = tmp_path / "historical-state"
    outside.mkdir()
    historical_link = sibling / ".runtime"
    historical_link.symlink_to(outside, target_is_directory=True)

    template = AGENT_EXECUTION_CONTRACTS["assurance.intake.agent.intake.v1"].resources
    assert isinstance(template, ResourceClaimTemplate)
    claims = template.resolve({"workspace": {"scope_id": current_id}}).writes
    store = TaskWorkspaceStore(project, current / ".staging", current / ".runtime" / "receipts")
    try:
        binding = store.begin(task_id="intake-execute", attempt=1, output_paths=claims)
        assert all(path.startswith(f"qa/changes/{current_id}/") for path in binding.identity.output_paths)
        assert all(
            "/.runtime/" not in path and "/.staging/" not in path for path in binding.identity.output_paths
        )

        target = binding.write_root / claims[0]
        target.parent.mkdir(parents=True)
        target.write_text("current change only\n", encoding="utf-8")
        store.promote(binding.identity, store.seal(binding.identity))
    finally:
        store.close()

    assert historical_link.is_symlink()
    assert (project / claims[0]).read_text(encoding="utf-8") == "current change only\n"


def test_explore_prepare_claim_ignores_a_symlinked_sibling_and_promotes_context(
    tmp_path: Path,
    installed_sources,
) -> None:
    from assurance_intake.operations import ExplorePrepareHandler
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS

    project = tmp_path / "project"
    current_id = "CH-CURRENT-001"
    sibling_id = "CH-HISTORICAL-001"
    current = project / "qa" / "changes" / current_id
    sibling = project / "qa" / "changes" / sibling_id
    current.mkdir(parents=True)
    sibling.mkdir(parents=True)
    requirement = current / "requirement.md"
    requirement.write_text("# Current requirement\n\nCover item creation.\n", encoding="utf-8")
    outside = tmp_path / "historical-state"
    outside.mkdir()
    sentinel = outside / "sentinel.json"
    sentinel.write_text('{"historical":true}\n', encoding="utf-8")
    historical_link = sibling / ".runtime"
    historical_link.symlink_to(outside, target_is_directory=True)

    resources = AGENT_EXECUTION_CONTRACTS["assurance.intake.agent.explore.v1"].resources
    assert isinstance(resources, ResourceClaimTemplate)
    assert resources.parameters == {"change_id": "/workspace/scope_id"}
    assert resources.reads == ("qa",)
    claims = resources.resolve({"workspace": {"scope_id": current_id}}).writes
    context_claim = f"qa/changes/{current_id}/explore/context.json"
    assert claims == (
        context_claim,
        f"qa/changes/{current_id}/explore/exploration.json",
    )

    store = TaskWorkspaceStore(project, current / ".staging", current / ".runtime" / "receipts")
    try:
        binding = store.begin(task_id="explore-prepare", attempt=1, output_paths=(context_claim,))
        invocation = InvocationMetadata(
            invocation_id="inv-explore-prepare",
            lock_digest="a" * 64,
            composition_digest="b" * 64,
            entrypoint="intake",
        )
        request = TaskRequest(
            invocation_id=invocation.invocation_id,
            task_id=binding.identity.task_id,
            graph_instance_id="explore-graph",
            node_id="prepare",
            capability_id="assurance.intake.agent.explore.v1",
            binding_data={
                "agent_profile": "aa-explorer",
                "execution": {
                    "provider_model": "test-model",
                    "worker_profile": "worker",
                    "permission_profile_digest": "c" * 64,
                    "limits": {"max_seconds": 5},
                },
                "request_policy_digest": "d" * 64,
                "request_config_digest": "e" * 64,
            },
            invocation=invocation,
            attempt=1,
            input={
                "change_id": current_id,
                "capability_leafs": ["entities.item.create"],
                "artifact_paths": [f"qa/changes/{current_id}/requirement.md"],
            },
        )
        outcome = asyncio.run(
            ExplorePrepareHandler().execute(
                request,
                TaskContext(
                    project_root=project,
                    write_root=binding.write_root,
                    workspace_identity=binding.identity,
                    heartbeat=lambda: None,
                    cancel_requested=lambda: False,
                    invocation=invocation,
                ),
            ),
        )

        assert outcome.status == "succeeded"
        agent_request = AgentRunRequest.model_validate(outcome.output)
        assert agent_request.workspace.scope_id == current_id
        staged_context = binding.write_root / context_claim
        document = json.loads(staged_context.read_bytes())
        assert document["change_id"] == current_id
        assert document["requirement_summary"] == "# Current requirement\n\nCover item creation.\n"

        store.promote(binding.identity, store.seal(binding.identity))
    finally:
        store.close()

    assert historical_link.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == '{"historical":true}\n'
    assert tuple(path.name for path in outside.iterdir()) == ("sentinel.json",)
    assert (project / context_claim).read_bytes() == staged_context.read_bytes()


def test_transient_agent_provider_failure_retries_the_skill_node(
    opencode_composition, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS

    del tmp_path, monkeypatch
    composition = opencode_composition
    assert not hasattr(composition, "workflow")
    leftover_alias = "assurance.product.agent.intake.intake.execute"
    contract_id = "assurance.intake.agent.intake.v1"
    assert leftover_alias not in composition.registries.capabilities.entries
    assert contract_id in AGENT_EXECUTION_CONTRACTS
    assert contract_id in composition.registries.capabilities.entries
    binding = composition.registries.capabilities.entries[contract_id]
    assert getattr(binding, "target_capability_id", None) == "runtime.opencode.execute"
