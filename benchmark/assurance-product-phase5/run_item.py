"""Fail-closed Phase 5 provider-live driver through aa-next."""

from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

from assurance_product.configuration import capability_leafs_from_knowledge

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
_OPENCODE_RESOLVED_READ_TIMEOUT_SECONDS = 300
_OPENCODE_RESOLVED_RETRY_BACKOFF_SECONDS = 10
_OPENCODE_RESOLVED_READ_ATTEMPTS = 3
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


def _catalog_leafs(composition, resource_id: str) -> list[str]:
    entry = composition.registries.resources.entries[resource_id]
    document = json.loads(entry.content.decode("utf-8"))
    leafs = document.get("typed_leafs") if isinstance(document, dict) else None
    if not isinstance(leafs, list) or any(not isinstance(item, str) for item in leafs):
        raise ValueError("authenticated capability catalog is missing typed_leafs")
    return leafs


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
    catalog_ref = _ref(composition, "assurance.product.configuration.capability-catalog")
    payload = {
        "schema_version": "1",
        "change_id": arguments["change_id"],
        "requirement": arguments["requirement"],
        "run_mode": "case",
        "selected_test_families": list(arguments["selected_test_families"]),
        "capability_leafs": _catalog_leafs(composition, catalog_ref["resource_id"]),
        "capability_catalog": catalog_ref,
        "product_policy": _ref(composition, "assurance.product.configuration.product-policy"),
        "data_knowledge": _ref(composition, "assurance.product.configuration.data-knowledge"),
        "allowed_artifact_paths": ["qa/archive", "qa/cases", "qa/changes", "tests"],
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


def derive_change_id(*, item_id: str, stamp: str, nonce: str) -> str:
    parts = (("item", item_id), ("stamp", stamp), ("nonce", nonce))
    for label, value in parts:
        if not value or any(character in value for character in ("/", "\\", " ", "\x00")):
            raise SystemExit(f"{label} is not a safe change-id component")
    return f"BENCH-{item_id}-{stamp}-{nonce}"


def _prepare_project_config_tree(config_tree: Path, project_dir: Path) -> None:
    knowledge_path = project_dir / ".aa" / "data-knowledge.yaml"
    if not knowledge_path.is_file() or knowledge_path.is_symlink():
        raise SystemExit("live SUT must provide a regular .aa/data-knowledge.yaml")
    try:
        knowledge = yaml.safe_load(knowledge_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise SystemExit("live SUT data knowledge is not valid YAML") from error
    if not isinstance(knowledge, dict):
        raise SystemExit("live SUT data knowledge must be a mapping")
    destination_knowledge = config_tree / ".aa" / "data-knowledge.yaml"
    destination_knowledge.write_text(
        yaml.safe_dump(knowledge, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    config_path = config_tree / ".aa" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise SystemExit("benchmark project configuration must be a mapping")
    config["data_knowledge"] = knowledge
    config["capability_catalog"] = {
        "schema_version": "1",
        "typed_leafs": list(capability_leafs_from_knowledge(knowledge)),
    }
    (config_tree / ".aa" / "capability-catalog.json").write_text(
        json.dumps(config["capability_catalog"], sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


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
    "agent-runtime-cursor",
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
    adapter: str,
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
            f"assurance-product[{adapter}]",
        ],
        cwd=repo,
        env=env,
        timeout=180,
        label=f"install assurance-product[{adapter}]",
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


def _last_json_object(path: Path, *, start: int = 0) -> dict[str, Any] | None:
    if start < 0:
        raise ValueError("run log start offset must be non-negative")
    with path.open(encoding="utf-8") as stream:
        stream.seek(start)
        text = stream.read()
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


def _agent_profile_errors(
    resolved_config: Mapping[str, Any], expected_config: Mapping[str, Any]
) -> list[str]:
    errors: list[str] = []
    resolved_agents = resolved_config.get("agent")
    expected_agents = expected_config.get("agent")
    if not isinstance(resolved_agents, Mapping):
        return ["resolved OpenCode config has no agent mapping"]
    if not isinstance(expected_agents, Mapping):
        return ["installed OpenCode config has no agent mapping"]
    for profile, expected_value in expected_agents.items():
        if not isinstance(profile, str) or not isinstance(expected_value, Mapping):
            errors.append(f"installed OpenCode agent definition is invalid: {profile!r}")
            continue
        resolved_value = resolved_agents.get(profile)
        if not isinstance(resolved_value, Mapping):
            errors.append(f"{profile}: agent is absent from resolved OpenCode config")
            continue
        expected_prompt = expected_value.get("prompt")
        if resolved_value.get("prompt") != expected_prompt:
            errors.append(f"{profile}: resolved prompt differs from installed project profile")
        expected_permission = expected_value.get("permission")
        resolved_permission = resolved_value.get("permission")
        if not isinstance(expected_permission, Mapping) or not isinstance(resolved_permission, Mapping):
            errors.append(f"{profile}: permission mapping is absent")
            continue
        for permission, expected_rule in expected_permission.items():
            resolved_rule = resolved_permission.get(permission)
            if isinstance(expected_rule, Mapping):
                if not isinstance(resolved_rule, Mapping):
                    errors.append(f"{profile}: permission {permission!s} is not a rule mapping")
                    continue
                for pattern, action in expected_rule.items():
                    actual = resolved_rule.get(pattern)
                    if actual != action:
                        errors.append(
                            f"{profile}: permission {permission!s}[{pattern!r}] resolved to "
                            f"{actual!r}, expected {action!r}"
                        )
                for pattern, action in resolved_rule.items():
                    if action == "allow" and expected_rule.get(pattern) != "allow":
                        expected_allows = (
                            candidate
                            for candidate, candidate_action in expected_rule.items()
                            if candidate_action == "allow" and isinstance(candidate, str)
                        )
                        is_narrower_allow = isinstance(pattern, str) and any(
                            fnmatchcase(pattern, candidate) for candidate in expected_allows
                        )
                        if pattern not in expected_rule and not is_narrower_allow:
                            errors.append(
                                f"{profile}: permission {permission!s}[{pattern!r}] has an ambient allow"
                            )
            elif resolved_rule != expected_rule:
                errors.append(
                    f"{profile}: permission {permission!s} resolved to {resolved_rule!r}, "
                    f"expected {expected_rule!r}"
                )
        expected_tools = expected_value.get("tools")
        resolved_tools = resolved_value.get("tools")
        if not isinstance(expected_tools, Mapping):
            errors.append(f"{profile}: installed tools mapping is absent")
            continue
        for tool, enabled in expected_tools.items():
            if not isinstance(tool, str):
                errors.append(f"{profile}: installed tool name is not text")
                continue
            if tool in expected_permission and isinstance(expected_permission[tool], Mapping):
                continue
            actual: object
            if isinstance(resolved_tools, Mapping) and tool in resolved_tools:
                actual = resolved_tools[tool]
            else:
                action = resolved_permission.get(tool)
                actual = (
                    {"allow": True, "deny": False}.get(action, action) if isinstance(action, str) else action
                )
            if actual is not enabled:
                errors.append(f"{profile}: tool {tool!s} resolved to {actual!r}, expected {enabled!r}")
    return errors


def _product_locked_opencode_config() -> dict[str, Any]:
    from assurance_product.opencode_agents import _opencode_config

    document = json.loads(_opencode_config())
    if not isinstance(document, dict):
        raise SystemExit("product-locked OpenCode config is not an object")
    return document


def _check_opencode_agent_profiles(endpoint: str, project_dir: Path) -> list[str]:
    try:
        expected = _product_locked_opencode_config()
    except (SystemExit, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        return [f"could not read product-locked OpenCode agent config: {error}"]
    last_error: Exception | None = None
    for attempt in range(_OPENCODE_RESOLVED_READ_ATTEMPTS):
        try:
            query = urlencode({"directory": str(project_dir.resolve())})
            request = Request(
                f"{endpoint.rstrip('/')}/config?{query}",
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urlopen(request, timeout=_OPENCODE_RESOLVED_READ_TIMEOUT_SECONDS) as response:  # noqa: S310 - pinned local endpoint
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                return ["resolved OpenCode config exceeds 4 MB"]
            resolved = json.loads(raw.decode("utf-8"))
            break
        except (OSError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            last_error = error
            if attempt + 1 < _OPENCODE_RESOLVED_READ_ATTEMPTS:
                time.sleep(_OPENCODE_RESOLVED_RETRY_BACKOFF_SECONDS)
    else:
        return [f"could not read resolved OpenCode agent config: {last_error}"]
    if not isinstance(expected, Mapping) or not isinstance(resolved, Mapping):
        return ["OpenCode agent config must resolve to an object"]
    return _agent_profile_errors(resolved, expected)


def _check_opencode_boundary_plugin(endpoint: str, project_dir: Path) -> list[str]:
    last_error: Exception | None = None
    for attempt in range(_OPENCODE_RESOLVED_READ_ATTEMPTS):
        try:
            query = urlencode({"directory": str(project_dir.resolve())})
            request = Request(
                f"{endpoint.rstrip('/')}/experimental/tool/ids?{query}",
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urlopen(request, timeout=_OPENCODE_RESOLVED_READ_TIMEOUT_SECONDS) as response:  # noqa: S310 - pinned local endpoint
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                return ["resolved OpenCode tool surface exceeds 1 MB"]
            tools = json.loads(raw.decode("utf-8"))
            break
        except (OSError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            last_error = error
            if attempt + 1 < _OPENCODE_RESOLVED_READ_ATTEMPTS:
                time.sleep(_OPENCODE_RESOLVED_RETRY_BACKOFF_SECONDS)
    else:
        return [f"could not read resolved OpenCode tool surface: {last_error}"]
    if not isinstance(tools, list) or any(not isinstance(item, str) for item in tools):
        return ["resolved OpenCode tool surface must be a string list"]
    if "assurance_boundary_v1" not in tools:
        return ["resolved OpenCode tool surface is missing assurance_boundary_v1"]
    return []


def _write_deployment_manifest(
    path: Path,
    item: Mapping[str, Any],
    *,
    project_scope: str,
    adapter: str,
) -> None:
    binding = dict(item["adapter_binding"])
    if adapter == "opencode":
        binding["project_scope"] = project_scope
    document = {
        "schema_version": "1",
        "runtime_plugin_id": f"runtime.{adapter}",
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
        f"**SUT:** `{safe.get('sut_root', '')}`",
        f"**Change ID:** `{safe.get('change_id', '')}`",
        f"**Change root:** `{safe.get('change_root', '')}`",
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


def _change_is_achieved(status: Mapping[str, Any]) -> bool:
    change = status.get("change")
    return isinstance(change, Mapping) and change.get("state") == "achieved"


def _provider_reference(
    status: Mapping[str, Any], *, adapter: str, item: Mapping[str, Any]
) -> dict[str, Any]:
    session = None
    evidence = status.get("adapter_evidence") or []
    if evidence and isinstance(evidence[0], Mapping):
        session = evidence[0].get("activity_id") or evidence[0].get("activation_id")
    raw_binding = item.get("adapter_binding")
    binding = raw_binding if isinstance(raw_binding, Mapping) else {}
    if adapter == "opencode":
        process = {"endpoint": binding.get("endpoint"), "protocol": "opencode-http-v1"}
    else:
        process = {"executable": binding.get("executable"), "protocol": "confined_process"}
    return {"session": session, "process": process}


def _validate_live_result(
    *,
    item: Mapping[str, Any],
    status: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    expected = str(item["expected_terminal"])
    actual = str(status.get("status") or "")
    if actual != expected:
        errors.append(f"terminal status {actual!r} != {expected!r}")
    if expected == "completed" and not _change_is_achieved(status):
        errors.append("completed status is not an achieved change")
    families = tuple(status.get("selected_test_families") or ())
    required_families = tuple(item["selected_test_families"])
    if families != required_families:
        errors.append(f"selected families {families!r} != {required_families!r}")
    steps = _status_steps(status)
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
    coverage = status.get("coverage_progress")
    if isinstance(coverage, Mapping):
        used = coverage.get("round")
        maximum = coverage.get("maximum_rounds")
        if isinstance(used, int) and isinstance(maximum, int) and used > maximum:
            errors.append("coverage-loop contract violation: rounds exceeded budget")
    if any(key in status for key in ("messages", "conversation", "session_text", "provider_text")):
        errors.append("provider session text used as status")
    return errors


def _lifecycle_args(
    *,
    project_dir: Path,
    change_id: str,
    invocation_id: str,
    source_args: Sequence[str],
) -> list[str]:
    return [
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
        "--invocation-id",
        invocation_id,
        *source_args,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--item", required=True)
    parser.add_argument("--adapter", choices=("opencode", "cursor"), required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=int, default=28800)
    parser.add_argument("--nonce")
    parser.add_argument("--stamp")
    arguments = parser.parse_args(argv)

    _reject_ambient_overrides()
    repo = _repo_root()
    manifest_path = repo / "benchmark" / "assurance-product-phase5" / "manifest.json"
    document = _load_manifest(manifest_path)
    item = _manifest_item(document, arguments.item, arguments.adapter)

    started_at = _utc_now()
    stamp = arguments.stamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    nonce = arguments.nonce or uuid.uuid4().hex[:8]
    change_id = derive_change_id(item_id=arguments.item, stamp=stamp, nonce=nonce)
    output = arguments.output
    if output is None:
        output = (
            repo
            / "benchmark"
            / "assurance-product-phase5"
            / "results"
            / f"{arguments.adapter}-{stamp}-{nonce}"
        )
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        return _fail(f"result directory must be fresh and empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    try:
        sut_root = _resolve_sut(repo, str(item["sut_root"]))
    except SystemExit as error:
        return _fail(str(error))
    change_root = sut_root / "qa" / "changes" / change_id
    run_log = output / "run.log"
    run_log.touch()

    evidence: dict[str, Any] = {
        "item_id": arguments.item,
        "product": item["product"],
        "entrypoint": item["entrypoint"],
        "sut_item_id": item["sut_item_id"],
        "sut_root": str(sut_root),
        "change_id": change_id,
        "change_root": str(change_root),
        "selected_test_families": list(item["selected_test_families"]),
        "adapter_version": item["adapter_version"],
        "provider_model": next(
            iter({route.get("provider_model") for route in item["routing_assignments"].values()})
        ),
        "worker_profile": next(
            iter({route.get("worker_profile") for route in item["routing_assignments"].values()})
        ),
        "started_at": started_at,
        "ended_at": None,
        "terminal_status": None,
        "lock_digest": None,
        "outcome": "incomplete",
        "notes": "",
        "status": {},
        "validation": {},
        "publish_receipt": None,
        "provider": {"session": None, "process": None},
        "logs": {"run_log": str(run_log)},
    }
    evidence_md = output / "evidence.md"

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
            evidence["provider"] = _provider_reference(status, adapter=arguments.adapter, item=item)
        evidence["notes"] = notes
        _write_json(output / "evidence.json", _redact_evidence(evidence))
        _write_evidence_markdown(evidence_md, evidence)
        return code

    env = os.environ.copy()
    secret_env = str(item["secret_env"])
    if arguments.adapter == "opencode":
        _load_opencode_secret(secret_env)
    env[secret_env] = os.environ.get(secret_env, "")
    isolated_env = dict(env)
    isolated_env.pop("PYTHONPATH", None)
    isolated_env.pop("UV_PROJECT", None)
    isolated_env["PYTHONNOUSERSITE"] = "1"

    if arguments.adapter == "opencode":
        endpoint = str(item["adapter_binding"]["endpoint"])
        preflight = _check_opencode(endpoint)
        if preflight != 0:
            evidence["outcome"] = "blocked"
            return finish(preflight, notes=f"OpenCode preflight failed at {endpoint}")
        evidence["provider"] = {
            "session": None,
            "process": {"endpoint": endpoint, "protocol": "opencode-http-v1"},
        }

    try:
        isolated_python, aa_next = _prepare_installed_product_env(
            repo=repo, output=output, env=env, adapter=arguments.adapter
        )
    except SystemExit as error:
        evidence["outcome"] = "blocked"
        return finish(1, notes=str(error))
    isolated_env["PATH"] = f"{aa_next.parent}{os.pathsep}{isolated_env.get('PATH', '')}"

    project_dir = sut_root
    if arguments.adapter == "opencode":
        endpoint = str(item["adapter_binding"]["endpoint"])
        agent_profile_errors = _check_opencode_agent_profiles(endpoint, project_dir)
        agent_profile_errors.extend(_check_opencode_boundary_plugin(endpoint, project_dir))
        if agent_profile_errors:
            evidence["outcome"] = "blocked"
            return finish(
                1,
                notes="OpenCode resolved agent preflight failed: " + "; ".join(agent_profile_errors),
            )

    config_tree = output / "config-tree"
    shutil.copytree(repo / "tests" / "phase5" / "fixtures" / "project-config", config_tree)
    _prepare_project_config_tree(config_tree, project_dir)

    deployment_manifest = output / "deployment.yaml"
    _write_deployment_manifest(
        deployment_manifest,
        item,
        project_scope=str(project_dir),
        adapter=arguments.adapter,
    )
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
            "change_id": change_id,
            "requirement": requirement,
            "selected_test_families": list(item["selected_test_families"]),
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

    invocation_id = change_id
    change_args = _lifecycle_args(
        project_dir=project_dir,
        change_id=change_id,
        invocation_id=invocation_id,
        source_args=source_args,
    )
    started = _aa_next(
        aa_next,
        "start",
        "--json",
        *change_args,
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

    deadline = time.monotonic() + arguments.timeout_seconds
    last_status: dict[str, Any] = {}
    last_run: dict[str, Any] = {}
    transitions: list[dict[str, Any]] = []
    run_args = [
        "run",
        "--json",
        *change_args,
        "--secret",
        _secret_arg(item),
    ]
    status_args = [
        "status",
        "--json",
        *change_args,
        "--secret",
        _secret_arg(item),
    ]

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            evidence["outcome"] = "blocked"
            evidence["validation"] = {"transitions": transitions, "last_run": last_run}
            return finish(1, notes="timed out waiting for a terminal aa-next status", status=last_status)
        run_start = run_log.stat().st_size
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
        parsed_run = _last_json_object(run_log, start=run_start)
        last_run = parsed_run or {
            "status": None,
            "terminal_reason": None,
            "returncode": run_proc.returncode,
        }
        if run_proc.returncode != 0 and parsed_run is None:
            evidence["outcome"] = "blocked"
            evidence["validation"] = {"transitions": transitions, "last_run": last_run}
            return finish(
                run_proc.returncode,
                notes="aa-next run exited non-zero without a structured run result",
            )
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
                time.sleep(min(arguments.poll_seconds, max(0, deadline - time.monotonic())))
                continue
        if last_status.get("status") in _TERMINAL_STATUSES:
            break
        time.sleep(min(arguments.poll_seconds, max(0, deadline - time.monotonic())))

    evidence["validation"] = {"transitions": transitions, "last_run": last_run}
    errors = _validate_live_result(item=item, status=last_status)
    if (
        errors
        or last_status.get("status") != item["expected_terminal"]
        or not _change_is_achieved(last_status)
    ):
        evidence["outcome"] = "blocked"
        evidence["validation"] = {**evidence["validation"], "errors": errors}
        detail = "; ".join(errors) if errors else f"live item did not reach {item['expected_terminal']!r}"
        run_reason = last_run.get("terminal_reason")
        if run_reason:
            detail = f"{detail}; last aa-next run returned {last_run.get('status')!r}/{run_reason!r}"
        return finish(
            run_proc.returncode or 1,
            notes=detail,
            status=last_status,
        )

    exported = _aa_next(
        aa_next,
        "export",
        "--json",
        "--project-dir",
        str(project_dir),
        "--change",
        change_id,
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
    evidence["publish_receipt"] = export_doc
    evidence["lock_digest"] = export_doc.get("lock_digest") or evidence.get("lock_digest")
    evidence["outcome"] = "completed"
    return finish(0, notes="live item reached achieved and export published once", status=last_status)


if __name__ == "__main__":
    raise SystemExit(main())
