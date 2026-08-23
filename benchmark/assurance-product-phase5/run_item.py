"""Fail-closed Phase 5 provider-live driver through aa-next."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

_AMBIENT_OVERRIDE_VARS = frozenset(
    {
        "OPENCODE_ENDPOINT",
        "OPENCODE_MODEL",
        "CURSOR_EXECUTABLE",
        "CURSOR_MODEL",
        "AA_MODEL",
        "PROVIDER_MODEL",
    }
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|token|secret)\s*[:=]\s*\S+|sk-[A-Za-z0-9-]+"
)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped", "interrupted"})
_SUT_COPY_IGNORE = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    ".history",
    ".opencode",
    ".playwright-mcp",
    ".cursor",
    ".claude",
    ".sisyphus",
    ".auth",
    ".tmp",
    ".vscode",
    ".DS_Store",
    ".superpowers",
    ".graph-runtime",
    "benchmark-results",
    "benchmark",
    "eval",
    "eval-fixtures",
    "qa",
    "deploy",
    "skills",
    "dist",
    "build",
    "opencode.json",
    "worker.err",
    "Untitled",
}
_WRITE_PRODUCT_INPUT = r"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from graph_engine.composition import ConfigTreePluginSource, WheelPluginSource

from assurance_product.models import ProductInputV1
from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition


def _ref(composition, resource_id: str) -> dict[str, str]:
    entry = composition.registries.resources.entries[resource_id]
    return {"resource_id": resource_id, "sha256": entry.sha256}


def _first_ref(composition, *resource_ids: str) -> dict[str, str]:
    for resource_id in resource_ids:
        if resource_id in composition.registries.resources.entries:
            return _ref(composition, resource_id)
    raise KeyError(resource_ids)


def main() -> int:
    arguments = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    composition = resolve_assurance_composition(
        AssuranceCompositionRequest(
            product_entrypoint=arguments["product"],
            deployment_source=WheelPluginSource(
                distribution=arguments["binding_dist"],
                entrypoint_name="deployment",
                declaration_path=arguments["binding_declaration"],
            ),
            configuration_tree=ConfigTreePluginSource(path=Path(arguments["config_tree"])),
        )
    )
    payload = {
        "schema_version": "1",
        "change_id": arguments["change_id"],
        "requirement": arguments["requirement"],
        "run_mode": "case",
        "selected_test_families": list(arguments["selected_test_families"]),
        "auto_archive": bool(arguments["auto_archive"]),
        "capability_catalog": _first_ref(
            composition,
            "assurance.product.configuration.capability-catalog",
            "assurance.product.configuration.project-config",
        ),
        "product_policy": _ref(composition, "assurance.product.configuration.product-policy"),
        "data_knowledge": _ref(composition, "assurance.product.configuration.data-knowledge"),
        "allowed_artifact_paths": ["qa/archive", "qa/cases", "qa/changes"],
        "budgets": {
            "review_rounds": 2,
            "coverage_rounds": 2,
            "healing_rounds": 2,
            "execution_retries": 2,
        },
    }
    value = ProductInputV1.model_validate(payload)
    value.validate_for_entrypoint(arguments["entrypoint"]).authenticate_against(composition)
    destination = Path(arguments["output"])
    destination.write_text(json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""
_VALIDATE_EXPORT = r"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from graph_engine.composition import ConfigTreePluginSource, WheelPluginSource
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.models import fold_events

from assurance_product.models import ResultExportV1
from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition


AGENT_PREFIX = "assurance.product.agent."
OPERATION_STEPS = {
    "assurance.improvement.apply-memory-improvement": "improvement.apply",
    "assurance.improvement.evaluate-memory-improvement": "improvement.evaluate",
    "assurance.improvement.export-change-improvement": "improvement.export",
    "assurance.improvement.rollback-memory-improvement": "improvement.rollback",
}


def _logical_step(capability: str) -> str | None:
    operation = OPERATION_STEPS.get(capability)
    if operation is not None:
        return operation
    if not capability.startswith(AGENT_PREFIX) or not capability.endswith(".finalize"):
        return None
    return capability.removeprefix(AGENT_PREFIX).removesuffix(".finalize")


def main() -> int:
    arguments = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    export_root = Path(arguments["export_root"])
    document = json.loads((export_root / "manifest.json").read_text(encoding="utf-8"))
    exported = ResultExportV1.model_validate(document)
    composition = resolve_assurance_composition(
        AssuranceCompositionRequest(
            product_entrypoint=arguments["product"],
            deployment_source=WheelPluginSource(
                distribution=arguments["binding_dist"],
                entrypoint_name="deployment",
                declaration_path=arguments["binding_declaration"],
            ),
            configuration_tree=ConfigTreePluginSource(path=Path(arguments["config_tree"])),
        )
    )
    ledger = Ledger(Path(arguments["engine_root"]) / "invocations" / arguments["invocation_id"] / "ledger")
    projection = fold_events(ledger.read_all())
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    steps: list[str] = []
    for activation in projection.activations:
        if activation.status not in {"completed", "stopped"}:
            continue
        graph = graphs[activation.graph_instance_id]
        node = composition.workflow.graphs[graph.graph_id].nodes[activation.node_id]
        if node.definition.kind != "task" or node.definition.capability is None:
            continue
        step = _logical_step(node.definition.capability)
        if step is not None:
            steps.append(step)
    Path(arguments["output"]).write_text(
        json.dumps(
            {
                "schema_version": exported.schema_version,
                "invocation_id": exported.invocation_id,
                "lock_digest": exported.lock_digest,
                "status": exported.status.status,
                "selected_test_families": list(exported.status.selected_test_families),
                "logical_steps": steps,
                "artifact_ids": [item.artifact_id for item in exported.artifact_index],
                "coverage_progress": None
                if exported.status.coverage_progress is None
                else exported.status.coverage_progress.model_dump(mode="json"),
                "durable_effects": [item.kind for item in exported.status.durable_effects],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fail(message: str) -> int:
    print(f"phase5-live: {message}", file=sys.stderr)
    return 1


def _reject_ambient_overrides() -> None:
    for name in _AMBIENT_OVERRIDE_VARS:
        if os.environ.get(name):
            raise SystemExit(f"ambient override {name!r} is forbidden")


def _reject_credentials_in_text(text: str, *, label: str) -> None:
    if _CREDENTIAL_PATTERN.search(text):
        raise SystemExit(f"credential material detected in {label}")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _reject_credentials_in_text(text, label=str(path))
    path.write_text(text, encoding="utf-8")


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.name != "manifest.json":
        raise SystemExit("driver must consume the committed manifest.json only")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("manifest must be a JSON object")
    return document


def _manifest_item(document: Mapping[str, Any], item_id: str, adapter: str) -> dict[str, Any]:
    items = document.get("items")
    if not isinstance(items, list):
        raise SystemExit("manifest must declare items[]")
    matches = [item for item in items if isinstance(item, dict) and item.get("id") == item_id]
    if len(matches) != 1:
        raise SystemExit(f"manifest must declare exactly one item {item_id!r}")
    item = matches[0]
    product = f"assurance-{adapter}"
    if item.get("product") != product:
        raise SystemExit(f"adapter/product mismatch: {adapter} != {item.get('product')}")
    if item.get("adapter_version") != "0.1.0":
        raise SystemExit(f"unsupported adapter_version {item.get('adapter_version')!r}")
    routes = item.get("routing_assignments")
    binding_routes = item.get("deployment_binding_routes")
    if not isinstance(routes, dict) or routes != binding_routes:
        raise SystemExit("routing_assignments must equal deployment_binding_routes")
    models = {assignment.get("provider_model") for assignment in routes.values()}
    workers = {assignment.get("worker_profile") for assignment in routes.values()}
    if adapter == "opencode" and models != {"openai/gpt-5.6-terra"}:
        raise SystemExit(f"OpenCode model mismatch: {sorted(models)}")
    if adapter == "opencode" and workers != {"max"}:
        raise SystemExit(f"OpenCode worker mismatch: {sorted(workers)}")
    families = tuple(item.get("selected_test_families") or ())
    if item.get("entrypoint") == "full" and families != ("api", "e2e", "fuzz", "performance"):
        raise SystemExit("full entrypoint requires all four families in canonical order")
    return item


def _resolve_sut(repo: Path, relative: str) -> Path:
    candidates = (
        repo / relative,
        repo.parents[1] / relative,
    )
    for candidate in candidates:
        if (candidate / "app").is_dir() and (candidate / "web").is_dir():
            return candidate.resolve()
    raise SystemExit(f"live SUT is missing at {relative}")


def _copy_sut(source: Path, destination: Path) -> None:
    def ignore(path: str, names: list[str]) -> set[str]:
        del path
        ignored = {name for name in names if name in _SUT_COPY_IGNORE}
        ignored.update(name for name in names if name.startswith("db.sqlite3"))
        return ignored

    shutil.copytree(source, destination, ignore=ignore, symlinks=False)


_PRODUCT_WHEEL_PACKAGES = (
    "graph-engine",
    "agent-runtime-contracts",
    "assurance-intake",
    "assurance-generation",
    "assurance-execution",
    "assurance-healing",
    "assurance-quality",
    "assurance-improvement",
    "assurance-product",
    "agent-runtime-opencode",
)


def _aa_next(
    binary: Path,
    *args: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [str(binary), *args],
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    label: str,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(  # noqa: S603
        list(command),
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"{label} failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return completed


def _prepare_installed_product_env(
    *,
    repo: Path,
    output: Path,
    env: Mapping[str, str],
) -> tuple[Path, Path]:
    dist_root = output / "wheels"
    dist_root.mkdir()
    venv = (output / "venv").resolve()
    for package in _PRODUCT_WHEEL_PACKAGES:
        _run_checked(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(dist_root),
                "--package",
                package,
            ],
            cwd=repo,
            env=env,
            timeout=180,
            label=f"uv build {package}",
        )
    _run_checked(
        ["uv", "venv", "--python", "3.11", str(venv)],
        cwd=repo,
        env=env,
        timeout=60,
        label="uv venv",
    )
    python = venv / "bin" / "python"
    aa_next = venv / "bin" / "aa-next"
    _run_checked(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--find-links",
            str(dist_root),
            "assurance-product[opencode]",
        ],
        cwd=repo,
        env=env,
        timeout=180,
        label="install assurance-product[opencode]",
    )
    if not aa_next.is_file():
        raise SystemExit("isolated env is missing aa-next")
    return python, aa_next


def _parse_json(output: str, *, label: str) -> dict[str, Any]:
    text = output.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise SystemExit(f"{label} did not emit JSON: {error}") from error
    if not isinstance(payload, dict):
        raise SystemExit(f"{label} JSON must be an object")
    return payload


def _last_json_object(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return {
                "invocation_id": payload.get("invocation_id"),
                "status": payload.get("status"),
                "terminal_reason": payload.get("terminal_reason"),
                "actions": payload.get("actions") or [],
            }
    return None


def _source_args(
    *,
    product: str,
    binding_dist: str,
    binding_declaration: str,
    config_tree: Path,
) -> list[str]:
    return [
        "--product",
        product,
        "--binding-dist",
        binding_dist,
        "--binding-entrypoint",
        "deployment",
        "--binding-declaration",
        binding_declaration,
        "--config-tree",
        str(config_tree),
    ]


def _secret_arg(item: Mapping[str, Any]) -> str:
    return f"{item['secret_handle']}=env:{item['secret_env']}"


def _load_opencode_secret(secret_env: str) -> None:
    if os.environ.get(secret_env):
        return
    phase3 = os.environ.get("OPENCODE_PHASE3_TOKEN")
    os.environ[secret_env] = phase3 if phase3 is not None else ""


def _check_opencode(endpoint: str) -> int:
    origin = endpoint.rstrip("/")
    try:
        request = Request(f"{origin}/global/health", headers={"Accept": "application/json"}, method="GET")
        with urlopen(request, timeout=5) as response:  # noqa: S310 - pinned local endpoint
            health = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as error:
        return _fail(f"OpenCode server at pinned endpoint {origin} is unavailable: {error}")
    if not isinstance(health, dict):
        return _fail("OpenCode health response is not an object")
    return 0


def _write_deployment_manifest(path: Path, item: Mapping[str, Any], *, project_scope: str) -> None:
    binding = dict(item["adapter_binding"])
    binding["project_scope"] = project_scope
    document = {
        "schema_version": "1",
        "runtime_plugin_id": "runtime.opencode",
        "adapter_binding": binding,
        "routes": item["routing_assignments"],
        "permission_profiles": {
            "assurance.product.agent.permission.default": {
                "schema_version": "1",
                "allowed_tools": ["bash", "edit", "glob", "grep", "read", "write"],
            }
        },
        "request_policies": {
            "assurance.product.agent.request.default": {
                "schema_version": "1",
                "max_output_bytes": 4_000_000,
            }
        },
        "secret_handles": [item["secret_handle"]],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, sort_keys=True)
    # bindings build accepts YAML; JSON is valid YAML.
    path.write_text(text + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, sort_keys=True)
    redacted = _CREDENTIAL_PATTERN.sub("[REDACTED]", text)
    value = json.loads(redacted)
    if not isinstance(value, dict):
        raise SystemExit("redacted evidence is not an object")
    return value


def _write_evidence_markdown(path: Path, payload: Mapping[str, Any]) -> None:
    safe = _redact_evidence(payload)
    lines = [
        "# OpenCode provider-live benchmark",
        "",
        f"**Item:** `{safe.get('item_id', '')}`",
        f"**Product:** `{safe.get('product', '')}`",
        f"**Entrypoint:** `{safe.get('entrypoint', '')}`",
        f"**Started:** {safe.get('started_at', '')}",
        f"**Ended:** {safe.get('ended_at', '')}",
        f"**Terminal status:** `{safe.get('terminal_status', 'unknown')}`",
        f"**Lock digest:** `{safe.get('lock_digest', '')}`",
        f"**Outcome:** {safe.get('outcome', '')}",
        "",
        "## Routing",
        "",
        "Every prepare ID is locked to `openai/gpt-5.6-terra` / `max`.",
        "",
        "## Status",
        "",
        "```json",
        json.dumps(safe.get("status") or {}, indent=2, sort_keys=True),
        "```",
        "",
        "## Validation",
        "",
        "```json",
        json.dumps(safe.get("validation") or {}, indent=2, sort_keys=True),
        "```",
        "",
        "## Notes",
        "",
        str(safe.get("notes") or "none"),
        "",
    ]
    text = "\n".join(lines)
    _reject_credentials_in_text(text, label=str(path))
    path.write_text(text, encoding="utf-8")


def _logical_step_from_node(node_id: str, graph_id: str) -> str | None:
    name = node_id.rsplit("/", 1)[-1]
    if name != "finalize":
        return None
    prefix = node_id.rsplit("/", 1)[0] if "/" in node_id else graph_id
    if prefix.startswith("generation.") or prefix.startswith("intake."):
        return prefix
    if prefix in {"intake", "explore", "case-design", "case-review"}:
        return f"intake.{prefix}"
    if prefix in {"execute", "run"}:
        return f"execution.{prefix}"
    if prefix in {"fact-baseline", "inspect", "report", "issue-analysis", "issue-triage"}:
        return f"quality.{prefix}"
    if prefix in {"archive", "retro", "improvement-review"}:
        return f"improvement.{prefix}"
    if graph_id.startswith(("generation.", "intake.", "execution.", "quality.", "improvement.")):
        return graph_id
    return None


def _status_steps(status: Mapping[str, Any]) -> tuple[str, ...]:
    graphs = {
        item["graph_instance_id"]: item
        for item in status.get("graph_hierarchy") or []
        if isinstance(item, dict)
    }
    steps: list[str] = []
    for node in status.get("node_states") or []:
        if not isinstance(node, dict) or node.get("state") not in {"succeeded", "stopped"}:
            continue
        graph = graphs.get(node.get("graph_instance_id"), {})
        step = _logical_step_from_node(str(node.get("node_id") or ""), str(graph.get("graph_id") or ""))
        if step is not None:
            steps.append(step)
    return tuple(steps)


def _validate_live_result(
    *,
    item: Mapping[str, Any],
    status: Mapping[str, Any],
    export_validation: Mapping[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    expected = str(item["expected_terminal"])
    actual = str(status.get("status") or "")
    if actual != expected:
        errors.append(f"terminal status {actual!r} != {expected!r}")
    families = tuple(status.get("selected_test_families") or ())
    required_families = tuple(item["selected_test_families"])
    if families != required_families:
        errors.append(f"selected families {families!r} != {required_families!r}")
    steps = (
        tuple(export_validation.get("logical_steps") or []) if export_validation else _status_steps(status)
    )
    missing = [step for step in item["required_steps"] if step not in steps]
    if missing:
        errors.append(f"missing required workflow steps: {missing}")
    if required_families:
        missing_families = [
            family
            for family in required_families
            if not any(step.startswith(f"generation.{family}.") for step in steps)
        ]
        if missing_families:
            errors.append(f"absent selected-family branches: {missing_families}")
    if "quality.report" not in steps:
        errors.append("missing report")
    if item.get("auto_archive") is True and "improvement.archive" not in steps:
        errors.append("auto_archive is true but improvement.archive did not run")
    if item.get("auto_archive") is False and "improvement.archive" in steps:
        errors.append("auto_archive is false but improvement.archive ran")
    coverage = status.get("coverage_progress")
    if isinstance(coverage, Mapping):
        used = coverage.get("round")
        maximum = coverage.get("maximum_rounds")
        if isinstance(used, int) and isinstance(maximum, int) and used > maximum:
            errors.append("coverage-loop contract violation: rounds exceeded budget")
    if any(key in status for key in ("messages", "conversation", "session_text", "provider_text")):
        errors.append("provider session text used as status")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--item", required=True)
    parser.add_argument("--adapter", choices=("opencode", "cursor"), required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=int, default=28800)
    arguments = parser.parse_args(argv)

    _reject_ambient_overrides()
    repo = _repo_root()
    manifest_path = repo / "benchmark" / "assurance-product-phase5" / "manifest.json"
    document = _load_manifest(manifest_path)
    item = _manifest_item(document, arguments.item, arguments.adapter)
    if arguments.adapter != "opencode":
        return _fail("this runner currently accepts only the OpenCode item")

    started_at = _utc_now()
    output = arguments.output
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output = repo / "benchmark" / "assurance-product-phase5" / "results" / f"opencode-{stamp}"
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        return _fail(f"result directory must be fresh and empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    evidence: dict[str, Any] = {
        "item_id": arguments.item,
        "product": item["product"],
        "entrypoint": item["entrypoint"],
        "sut_item_id": item["sut_item_id"],
        "selected_test_families": list(item["selected_test_families"]),
        "auto_archive": item["auto_archive"],
        "adapter_version": item["adapter_version"],
        "provider_model": "openai/gpt-5.6-terra",
        "worker_profile": "max",
        "started_at": started_at,
        "ended_at": None,
        "terminal_status": None,
        "lock_digest": None,
        "outcome": "incomplete",
        "notes": "",
        "status": {},
        "validation": {},
    }
    evidence_md = (
        repo
        / ".superpowers"
        / "sdd"
        / "2026-08-22-pure-graph-engine-phase5-assurance-product-assembly"
        / "opencode-benchmark.md"
    )

    def finish(code: int, *, notes: str, status: Mapping[str, Any] | None = None) -> int:
        evidence["ended_at"] = _utc_now()
        if status is not None:
            evidence["status"] = {
                key: status.get(key)
                for key in (
                    "status",
                    "lock_digest",
                    "entrypoint",
                    "selected_test_families",
                    "coverage_progress",
                    "terminal_reason",
                )
            }
            evidence["terminal_status"] = status.get("status")
            evidence["lock_digest"] = status.get("lock_digest") or evidence.get("lock_digest")
        evidence["notes"] = notes
        _write_json(output / "evidence.json", _redact_evidence(evidence))
        _write_evidence_markdown(evidence_md, evidence)
        return code

    env = os.environ.copy()
    secret_env = str(item["secret_env"])
    _load_opencode_secret(secret_env)
    env[secret_env] = os.environ[secret_env]
    isolated_env = dict(env)
    isolated_env.pop("PYTHONPATH", None)
    isolated_env.pop("UV_PROJECT", None)
    isolated_env["PYTHONNOUSERSITE"] = "1"

    endpoint = str(item["adapter_binding"]["endpoint"])
    preflight = _check_opencode(endpoint)
    if preflight != 0:
        evidence["outcome"] = "blocked"
        return finish(preflight, notes=f"OpenCode preflight failed at {endpoint}")

    try:
        isolated_python, aa_next = _prepare_installed_product_env(repo=repo, output=output, env=env)
    except SystemExit as error:
        evidence["outcome"] = "blocked"
        return finish(1, notes=str(error))
    isolated_env["PATH"] = f"{aa_next.parent}{os.pathsep}{isolated_env.get('PATH', '')}"

    project_dir = output / "project"
    try:
        _copy_sut(_resolve_sut(repo, str(item["sut_root"])), project_dir)
    except SystemExit as error:
        evidence["outcome"] = "blocked"
        return finish(1, notes=str(error))

    config_tree = output / "config-tree"
    shutil.copytree(repo / "tests" / "phase5" / "fixtures" / "project-config", config_tree)

    deployment_manifest = output / "deployment.yaml"
    _write_deployment_manifest(deployment_manifest, item, project_scope=str(project_dir))
    wheel_dir = output / "binding-wheel"
    wheel_dir.mkdir()
    built = _aa_next(
        aa_next,
        "bindings",
        "build",
        "--json",
        "--manifest",
        str(deployment_manifest),
        "--output-dir",
        str(wheel_dir),
        cwd=repo,
        env=isolated_env,
        timeout=120,
    )
    if built.returncode != 0:
        evidence["outcome"] = "blocked"
        return finish(built.returncode, notes=f"bindings build failed: {built.stderr.strip()}")
    built_doc = _parse_json(built.stdout, label="bindings build")
    binding_dist = str(built_doc["distribution"])
    binding_declaration = str(built_doc["declaration_path"])
    wheel = Path(str(built_doc["wheel"]))
    installed = subprocess.run(  # noqa: S603
        ["uv", "pip", "install", "--python", str(isolated_python), str(wheel)],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if installed.returncode != 0:
        evidence["outcome"] = "blocked"
        return finish(
            installed.returncode, notes=f"deployment wheel install failed: {installed.stderr.strip()}"
        )

    source_args = _source_args(
        product=str(item["product"]),
        binding_dist=binding_dist,
        binding_declaration=binding_declaration,
        config_tree=config_tree,
    )
    compiled = _aa_next(aa_next, "compile", "--json", *source_args, cwd=repo, env=isolated_env, timeout=180)
    if compiled.returncode != 0:
        evidence["outcome"] = "blocked"
        return finish(compiled.returncode, notes=f"compile failed: {compiled.stderr.strip()}")
    compile_doc = _parse_json(compiled.stdout, label="compile")
    evidence["lock_digest"] = compile_doc.get("lock_digest")

    requirement_path = repo / str(item["requirement_path"])
    requirement = requirement_path.read_text(encoding="utf-8").strip()
    helper_dir = output / "helpers"
    helper_dir.mkdir()
    (helper_dir / "write_product_input.py").write_text(_WRITE_PRODUCT_INPUT, encoding="utf-8")
    input_args = helper_dir / "write_product_input.args.json"
    input_path = output / "product-input.json"
    _write_json(
        input_args,
        {
            "product": item["product"],
            "binding_dist": binding_dist,
            "binding_declaration": binding_declaration,
            "config_tree": str(config_tree),
            "change_id": item["sut_item_id"],
            "requirement": requirement,
            "selected_test_families": list(item["selected_test_families"]),
            "auto_archive": item["auto_archive"],
            "entrypoint": item["entrypoint"],
            "output": str(input_path),
        },
    )
    written = subprocess.run(  # noqa: S603
        [str(isolated_python), str(helper_dir / "write_product_input.py"), str(input_args)],
        cwd=repo,
        env=isolated_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if written.returncode != 0:
        evidence["outcome"] = "blocked"
        return finish(written.returncode, notes=f"product input write failed: {written.stderr.strip()}")

    engine_root = output / "engine"
    engine_root.mkdir()
    invocation_id = arguments.item
    started = _aa_next(
        aa_next,
        "start",
        "--json",
        "--project-dir",
        str(project_dir),
        "--engine-root",
        str(engine_root),
        "--invocation-id",
        invocation_id,
        *source_args,
        "--entrypoint",
        str(item["entrypoint"]),
        "--input",
        str(input_path),
        "--secret",
        _secret_arg(item),
        cwd=repo,
        env=isolated_env,
        timeout=600,
    )
    if started.returncode != 0:
        evidence["outcome"] = "blocked"
        return finish(started.returncode, notes=f"start failed: {started.stderr.strip()}")
    start_doc = _parse_json(started.stdout, label="start")
    evidence["lock_digest"] = start_doc.get("lock_digest") or evidence.get("lock_digest")

    run_log = output / "run.log"
    deadline = time.monotonic() + arguments.timeout_seconds
    last_status: dict[str, Any] = {}
    last_run: dict[str, Any] = {}
    transitions: list[dict[str, Any]] = []
    parked_recovery = False
    run_args = [
        "run",
        "--json",
        "--engine-root",
        str(engine_root),
        "--invocation-id",
        invocation_id,
        *source_args,
        "--secret",
        _secret_arg(item),
    ]
    status_args = [
        "status",
        "--json",
        "--engine-root",
        str(engine_root),
        "--invocation-id",
        invocation_id,
        *source_args,
        "--secret",
        _secret_arg(item),
    ]

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            evidence["outcome"] = "blocked"
            evidence["validation"] = {"transitions": transitions, "last_run": last_run}
            return finish(1, notes="timed out waiting for a terminal aa-next status", status=last_status)
        with run_log.open("a", encoding="utf-8") as log:
            run_proc = subprocess.run(  # noqa: S603
                [str(aa_next), *run_args],
                cwd=repo,
                env=isolated_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=max(1, int(remaining)),
                check=False,
            )
        last_run = _last_json_object(run_log) or {
            "status": None,
            "terminal_reason": None,
            "returncode": run_proc.returncode,
        }
        status_proc = _aa_next(
            aa_next,
            *status_args,
            cwd=repo,
            env=isolated_env,
            timeout=120,
        )
        if status_proc.returncode == 0:
            last_status = _parse_json(status_proc.stdout, label="status")
            current = {
                "at": _utc_now(),
                "status": last_status.get("status"),
                "terminal_reason": last_status.get("terminal_reason") or last_run.get("terminal_reason"),
            }
            if not transitions or transitions[-1].get("status") != current["status"]:
                transitions.append(current)
            if last_status.get("status") in _TERMINAL_STATUSES:
                break
        else:
            evidence["outcome"] = "blocked"
            evidence["validation"] = {"transitions": transitions, "last_run": last_run}
            return finish(
                status_proc.returncode or run_proc.returncode or 1,
                notes=f"run ended without readable status: {status_proc.stderr.strip()}",
            )
        if last_run.get("status") == "interrupted":
            actions = last_run.get("actions") or []
            if actions:
                evidence["outcome"] = "blocked"
                evidence["validation"] = {"transitions": transitions, "last_run": last_run}
                return finish(
                    run_proc.returncode or 1,
                    notes="aa-next run returned a pending interrupt; resume was not invoked",
                    status=last_status,
                )
            if last_run.get("terminal_reason") == "activity_recovery":
                if parked_recovery:
                    break
                parked_recovery = True
                time.sleep(min(arguments.poll_seconds, max(0, deadline - time.monotonic())))
                continue
        if last_status.get("status") in _TERMINAL_STATUSES:
            break
        time.sleep(min(arguments.poll_seconds, max(0, deadline - time.monotonic())))

    evidence["validation"] = {"transitions": transitions, "last_run": last_run}
    if last_status.get("status") != item["expected_terminal"]:
        evidence["outcome"] = "blocked"
        detail = f"live item did not reach {item['expected_terminal']!r}"
        run_reason = last_run.get("terminal_reason")
        if run_reason:
            detail = f"{detail}; last aa-next run returned {last_run.get('status')!r}/{run_reason!r}"
        return finish(
            run_proc.returncode or 1,
            notes=detail,
            status=last_status,
        )

    export_root = output / "export"
    exported = _aa_next(
        aa_next,
        "export",
        "--json",
        "--destination",
        str(export_root),
        "--engine-root",
        str(engine_root),
        "--invocation-id",
        invocation_id,
        *source_args,
        "--secret",
        _secret_arg(item),
        cwd=repo,
        env=isolated_env,
        timeout=600,
    )
    if exported.returncode != 0:
        evidence["outcome"] = "blocked"
        return finish(
            exported.returncode, notes=f"export failed: {exported.stderr.strip()}", status=last_status
        )
    export_doc = _parse_json(exported.stdout, label="export")
    evidence["lock_digest"] = export_doc.get("lock_digest") or evidence.get("lock_digest")

    (helper_dir / "validate_export.py").write_text(_VALIDATE_EXPORT, encoding="utf-8")
    validate_args = helper_dir / "validate_export.args.json"
    validation_path = output / "export-validation.json"
    _write_json(
        validate_args,
        {
            "export_root": str(export_root),
            "product": item["product"],
            "binding_dist": binding_dist,
            "binding_declaration": binding_declaration,
            "config_tree": str(config_tree),
            "engine_root": str(engine_root),
            "invocation_id": invocation_id,
            "output": str(validation_path),
        },
    )
    validated = subprocess.run(  # noqa: S603
        [str(isolated_python), str(helper_dir / "validate_export.py"), str(validate_args)],
        cwd=repo,
        env=isolated_env,
        text=True,
        capture_output=True,
        check=False,
    )
    export_validation: dict[str, Any] | None = None
    if validated.returncode == 0 and validation_path.is_file():
        export_validation = json.loads(validation_path.read_text(encoding="utf-8"))
    errors = _validate_live_result(item=item, status=last_status, export_validation=export_validation)
    evidence["validation"] = {
        "transitions": transitions,
        "export": export_validation or {},
        "errors": errors,
    }
    if errors:
        evidence["outcome"] = "blocked"
        return finish(1, notes="; ".join(errors), status=last_status)
    evidence["outcome"] = "completed"
    evidence["artifact_digests"] = {
        "export_lock_digest": export_doc.get("lock_digest"),
        "result_tree_digest": export_doc.get("result_tree_digest"),
        "event_stream_digest": export_doc.get("event_stream_digest"),
    }
    return finish(0, notes="live item reached completed and export validated", status=last_status)


if __name__ == "__main__":
    raise SystemExit(main())
