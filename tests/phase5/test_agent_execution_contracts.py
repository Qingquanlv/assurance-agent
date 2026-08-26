from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from graph_engine.plugin_api import ResourceClaimTemplate, TaskOutcome
from graph_engine.runtime.engine import Engine
from graph_engine.runtime.task_workspace import TaskWorkspaceStore

from tests.phase5.cli_support import (
    SECRET_ENV,
    SECRET_VALUE,
    _CompletingScriptedHost,
    start_lifecycle_invocation,
)

pytestmark = pytest.mark.usefixtures("installed_sources")


EXPECTED_AGENT_PROFILES = {
    "assurance.intake.case-design.prepare": "assurance-v1-doc-author",
    "assurance.intake.case-review.prepare": "assurance-v1-reviewer",
    "assurance.intake.explore.prepare": "assurance-v1-explorer",
    "assurance.intake.intake.prepare": "assurance-v1-doc-author",
    "assurance.generation.api.codegen-fix.prepare": "assurance-v1-test-author",
    "assurance.generation.api.codegen.prepare": "assurance-v1-test-author",
    "assurance.generation.api.plan-review.prepare": "assurance-v1-reviewer",
    "assurance.generation.api.plan.prepare": "assurance-v1-doc-author",
    "assurance.generation.e2e.codegen-fix.prepare": "assurance-v1-test-author",
    "assurance.generation.e2e.codegen.prepare": "assurance-v1-test-author",
    "assurance.generation.e2e.plan-review.prepare": "assurance-v1-reviewer",
    "assurance.generation.e2e.plan.prepare": "assurance-v1-doc-author",
    "assurance.generation.fuzz.codegen.prepare": "assurance-v1-test-author",
    "assurance.generation.fuzz.plan-review.prepare": "assurance-v1-reviewer",
    "assurance.generation.fuzz.plan.prepare": "assurance-v1-doc-author",
    "assurance.generation.performance.codegen.prepare": "assurance-v1-test-author",
    "assurance.generation.performance.plan-review.prepare": "assurance-v1-reviewer",
    "assurance.generation.performance.plan.prepare": "assurance-v1-doc-author",
    "assurance.execution.execute.prepare": "assurance-v1-executor",
    "assurance.execution.run.prepare": "assurance-v1-executor",
    "assurance.healing.coverage-repair.prepare": "assurance-v1-test-author",
    "assurance.healing.fix-proposal.prepare": "assurance-v1-doc-author",
    "assurance.quality.fact-baseline.prepare": "assurance-v1-doc-author",
    "assurance.quality.inspect.prepare": "assurance-v1-reviewer",
    "assurance.quality.issue-analysis.prepare": "assurance-v1-reporter",
    "assurance.quality.issue-triage.prepare": "assurance-v1-reporter",
    "assurance.quality.report.prepare": "assurance-v1-reporter",
    "assurance.improvement.archive.prepare": "assurance-v1-archiver",
    "assurance.improvement.improvement-review.prepare": "assurance-v1-reviewer",
    "assurance.improvement.retro-eval-analysis.prepare": "assurance-v1-doc-author",
    "assurance.improvement.retro-issue-analysis.prepare": "assurance-v1-doc-author",
    "assurance.improvement.retro-workflow-analysis.prepare": "assurance-v1-doc-author",
    "assurance.improvement.retro.prepare": "assurance-v1-doc-author",
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


def test_all_agent_skills_have_one_bound_agent_and_execution_contract() -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from assurance_product.binding_builder import _binding_documents
    from assurance_product.models import DeploymentBindingsV1
    from assurance_product.models import PREPARE_IDS, alias_ids_for_prepare
    from assurance_product.product import load_canonical_workflow

    assert set(PREPARE_IDS) == set(EXPECTED_AGENT_PROFILES) == set(AGENT_EXECUTION_CONTRACTS)
    fixture = Path(__file__).parent / "fixtures" / "deployment" / "opencode.yaml"
    bindings = DeploymentBindingsV1.model_validate(yaml.safe_load(fixture.read_text(encoding="utf-8")))
    binding_documents = {
        str(document["capability_id"]): document for document in _binding_documents(bindings)
    }
    workflow = load_canonical_workflow()
    assert workflow.retry["agent-transient"].max_attempts == 12
    assert workflow.retry["agent-transient"].retry_on == ("transient",)
    graph_nodes = {
        node.capability: node
        for graph in workflow.graphs.values()
        for node in graph.nodes.values()
        if node.capability is not None
    }
    for prepare_id, expected_agent in EXPECTED_AGENT_PROFILES.items():
        prepare_alias, execute_alias, _finalize_alias = alias_ids_for_prepare(prepare_id)
        prepare_binding = binding_documents[prepare_alias]
        binding_data = prepare_binding["data"]
        assert isinstance(binding_data, Mapping)
        assert binding_data["agent_profile"] == expected_agent
        contract = AGENT_EXECUTION_CONTRACTS[prepare_id]
        assert contract.agent_profile == expected_agent
        assert contract.resources.writes
        assert graph_nodes[execute_alias].resources == contract.resources


def test_agent_execute_contracts_render_exact_current_change_output_claims() -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from assurance_product.output_routes import OutputRouteCatalog, execute_alias_for_prepare

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
    for prepare_id, contract in AGENT_EXECUTION_CONTRACTS.items():
        execute_alias = execute_alias_for_prepare(prepare_id)
        assert isinstance(contract.resources, ResourceClaimTemplate)
        assert contract.resources.parameters == {"change_id": "/workspace/scope_id"}
        resolved = contract.resources.resolve({"workspace": {"scope_id": change_id}})
        assert resolved.writes == catalog.outputs(execute_alias, change_id)
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

    template = AGENT_EXECUTION_CONTRACTS["assurance.intake.intake.prepare"].resources
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


def test_transient_agent_provider_failure_retries_the_skill_node(
    installed_sources, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "assurance.product.agent.intake.intake.execute"

    class TransientOnceHost(_CompletingScriptedHost):
        def __init__(self) -> None:
            super().__init__(
                execution_sequence=(),
                coverage_sequence=(),
                threshold=0.90,
                coverage_rounds=1,
                review_decision="pass",
                healing_decision="allowed",
            )
            self.target_attempts = 0

        def _outcome(self, capability_id: str) -> TaskOutcome:
            if capability_id == target:
                self.target_attempts += 1
                if self.target_attempts == 1:
                    return TaskOutcome.failed("transient", "provider TLS handshake failed")
            return super()._outcome(capability_id)

    host = TransientOnceHost()

    def host_factory(root: Path, authorization: object) -> Engine:
        del authorization
        return Engine(root, host=host)

    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    invocation = start_lifecycle_invocation(
        tmp_path,
        installed_sources,
        invocation_id="inv-agent-transient-retry",
        drive=True,
        entrypoint="intake",
        host_factory=host_factory,
    )
    try:
        assert host.target_attempts == 2
    finally:
        invocation.engine.close()
