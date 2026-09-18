from __future__ import annotations

import importlib
import json
import sys
import zipfile
from pathlib import Path
from collections.abc import Mapping
from typing import Any

import yaml
from graph_engine.composition import ConfigTreePluginSource, WheelPluginSource

from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
from assurance_product.application import AssuranceProductApplication
from assurance_product.binding_builder import build_deployment_wheel
from assurance_product.bootstrap.contracts import RunSpecV1
from assurance_product.configuration import capability_leafs_from_knowledge
from assurance_product.models import LOCKED_ALLOWED_ARTIFACT_PATHS, ProductInputV1
from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition

_HISTORICAL_PREPARE_IDS = (
    "assurance.intake.case-design.prepare",
    "assurance.intake.case-review.prepare",
    "assurance.intake.explore.prepare",
    "assurance.intake.intake.prepare",
    "assurance.generation.api.codegen.prepare",
    "assurance.generation.api.codegen-review.prepare",
    "assurance.generation.e2e.codegen.prepare",
    "assurance.generation.e2e.codegen-review.prepare",
    "assurance.generation.fuzz.codegen.prepare",
    "assurance.generation.fuzz.codegen-review.prepare",
    "assurance.generation.performance.codegen.prepare",
    "assurance.generation.performance.codegen-review.prepare",
    "assurance.execution.execute.prepare",
    "assurance.execution.run.prepare",
    "assurance.healing.coverage-repair.prepare",
    "assurance.healing.fix-proposal.prepare",
    "assurance.quality.fact-baseline.prepare",
    "assurance.quality.inspect.prepare",
    "assurance.quality.issue-analysis.prepare",
    "assurance.quality.issue-triage.prepare",
    "assurance.quality.report.prepare",
    "assurance.improvement.archive.prepare",
    "assurance.improvement.improvement-review.prepare",
    "assurance.improvement.retro-eval-analysis.prepare",
    "assurance.improvement.retro-issue-analysis.prepare",
    "assurance.improvement.retro-workflow-analysis.prepare",
    "assurance.improvement.retro.prepare",
)

_ADAPTER_CONFIGURATION_DIGEST = "7cd4626d1a9d26c705f3c0578c1f19b19c1f7f364daf15e175740e0541aa7c8d"
_TLS_IDENTITY_DIGEST = "fe528b695a03f2a0e62f03bfb958d53adce124684f6879c2df73776aa3c80003"
_PERMISSION_PROFILE_ID = "assurance.product.agent.permission.default"
_REQUEST_POLICY_ID = "assurance.product.agent.request.default"
_CATALOG_RESOURCE_ID = "assurance.product.configuration.capability-catalog"
_POLICY_RESOURCE_ID = "assurance.product.configuration.product-policy"
_KNOWLEDGE_RESOURCE_ID = "assurance.product.configuration.data-knowledge"


def _semantic_contract_id(prepare_id: str) -> str:
    rest = prepare_id.removeprefix("assurance.").removesuffix(".prepare")
    feature, _, base = rest.partition(".")
    return f"assurance.{feature}.agent.{base}.v1"


PREPARE_IDS = tuple(_semantic_contract_id(item) for item in _HISTORICAL_PREPARE_IDS) + (
    "assurance.healing.agent.apply-test-repair.v1",
)

_PLUGIN_YAML = """schema_version: \"1\"
plugin_id: assurance.product.configuration
plugin_version: \"1.0.0\"
engine_api: \"2.0\"
dependencies:
  - plugin_id: assurance.intake
    version_specifier: \"==0.3.0\"
  - plugin_id: assurance.generation
    version_specifier: \"==0.3.0\"
  - plugin_id: assurance.execution
    version_specifier: \"==0.3.0\"
  - plugin_id: assurance.healing
    version_specifier: \"==0.3.0\"
  - plugin_id: assurance.quality
    version_specifier: \"==0.3.0\"
  - plugin_id: assurance.improvement
    version_specifier: \"==0.3.0\"
files:
  - kind: resource
    resource_id: assurance.product.configuration.product-policy
    path: .aa/policy.yaml
    media_type: application/yaml
  - kind: resource
    resource_id: assurance.product.configuration.data-knowledge
    path: .aa/data-knowledge.yaml
    media_type: application/yaml
  - kind: resource
    resource_id: assurance.product.configuration.project-config
    path: .aa/config.yaml
    media_type: application/yaml
  - kind: resource
    resource_id: assurance.product.configuration.capability-catalog
    path: .aa/capability-catalog.json
    media_type: application/json
bindings: []
"""

_CONFIG_YAML = """schema_version: \"1\"
product_policy:
  schema_version: \"1\"
  organization: example
data_knowledge:
  schema_version: \"1\"
  notes: []
capability_catalog:
  schema_version: \"1\"
  typed_leafs: []
node_policy_values: {}
"""


def _write_config_tree_skeleton(config_tree: Path) -> None:
    aa = config_tree / ".aa"
    aa.mkdir(parents=True)
    (config_tree / "plugin.yaml").write_text(_PLUGIN_YAML, encoding="utf-8")
    (aa / "config.yaml").write_text(_CONFIG_YAML, encoding="utf-8")
    (aa / "policy.yaml").write_text("schema_version: '1'\norganization: example\n", encoding="utf-8")
    (aa / "data-knowledge.yaml").write_text("schema_version: '1'\nnotes: []\n", encoding="utf-8")
    (aa / "capability-catalog.json").write_text(
        '{"schema_version":"1","typed_leafs":[]}\n',
        encoding="utf-8",
    )


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError(f"live SUT must provide a regular {label}")


def _load_mapping(path: Path, label: str) -> tuple[bytes, dict[str, object]]:
    _require_regular_file(path, label)
    try:
        raw = path.read_bytes()
        document = yaml.safe_load(raw)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"live SUT {label} is not valid YAML") from error
    if not isinstance(document, dict):
        raise ValueError(f"live SUT {label} must be a mapping")
    return raw, document


def _write_deployment_manifest(
    path: Path,
    *,
    spec: RunSpecV1,
    project_scope: str,
    opencode_endpoint: str,
) -> None:
    routes = {
        contract_id: {
            "provider_model": spec.routes.provider_model,
            "worker_profile": spec.routes.worker_profile,
            "permission_profile_id": _PERMISSION_PROFILE_ID,
            "request_policy_id": _REQUEST_POLICY_ID,
            "limits": {"max_seconds": 3600},
        }
        for contract_id in PREPARE_IDS
    }
    document = {
        "schema_version": "1",
        "runtime_plugin_id": "runtime.opencode",
        "adapter_binding": {
            "adapter_configuration_digest": _ADAPTER_CONFIGURATION_DIGEST,
            "cancel_timeout_seconds": 60,
            "endpoint": opencode_endpoint,
            "max_response_bytes": 4000000,
            "observation_horizon_seconds": 3600,
            "progress_timeout_seconds": 300,
            "poll_interval_seconds": 2,
            "project_scope": project_scope,
            "protocol_profile": "opencode-http-v1",
            "request_timeout_seconds": 300,
            "schema_version": "1",
            "secret_handle": "opencode.token",
            "tls_identity_digest": _TLS_IDENTITY_DIGEST,
        },
        "routes": routes,
        "permission_profiles": {
            _PERMISSION_PROFILE_ID: {
                "schema_version": "1",
                "allowed_tools": ["bash", "edit", "glob", "grep", "read", "write"],
            }
        },
        "request_policies": {
            _REQUEST_POLICY_ID: {
                "schema_version": "1",
                "max_output_bytes": 4_000_000,
            }
        },
        "secret_handles": ["opencode.token"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _materialize_config_tree(config_tree: Path, project_dir: Path) -> None:
    policy_bytes, policy = _load_mapping(project_dir / ".aa" / "policy.yaml", ".aa/policy.yaml")
    knowledge_bytes, knowledge = _load_mapping(
        project_dir / ".aa" / "data-knowledge.yaml",
        ".aa/data-knowledge.yaml",
    )
    _write_config_tree_skeleton(config_tree)
    (config_tree / ".aa" / "policy.yaml").write_bytes(policy_bytes)
    (config_tree / ".aa" / "data-knowledge.yaml").write_bytes(knowledge_bytes)
    catalog = {
        "schema_version": "1",
        "typed_leafs": list(capability_leafs_from_knowledge(knowledge)),
    }
    catalog_bytes = (json.dumps(catalog, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (project_dir / ".aa" / "capability-catalog.json").write_bytes(catalog_bytes)
    (config_tree / ".aa" / "capability-catalog.json").write_bytes(catalog_bytes)
    config_path = config_tree / ".aa" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("project configuration must be a mapping")
    config["product_policy"] = policy
    config["data_knowledge"] = knowledge
    config["capability_catalog"] = catalog
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _extract_binding_wheel(wheel: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    resolved = destination.resolve()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(resolved)
    root = str(resolved)
    if root not in sys.path:
        sys.path.insert(0, root)
    importlib.invalidate_caches()


def _resource_ref(composition: object, resource_id: str) -> dict[str, str]:
    entries = getattr(getattr(getattr(composition, "registries", None), "resources", None), "entries", None)
    if not isinstance(entries, Mapping):
        raise ValueError("composition resource registry is unavailable")
    entry = entries[resource_id]
    return {"resource_id": resource_id, "sha256": entry.sha256}


def _catalog_leafs(composition: object, resource_id: str) -> list[str]:
    entries = getattr(getattr(getattr(composition, "registries", None), "resources", None), "entries", None)
    if not isinstance(entries, Mapping):
        raise ValueError("composition resource registry is unavailable")
    document = json.loads(entries[resource_id].content.decode("utf-8"))
    leafs = document.get("typed_leafs") if isinstance(document, dict) else None
    if not isinstance(leafs, list) or any(not isinstance(item, str) for item in leafs):
        raise ValueError("authenticated capability catalog is missing typed_leafs")
    return leafs


def prepare_composition(
    *,
    project_dir: Path,
    run_dir: Path,
    spec: RunSpecV1,
    opencode_endpoint: str,
    change_id: str,
) -> dict[str, object]:
    if set(PREPARE_IDS) != set(AGENT_EXECUTION_CONTRACTS):
        raise ValueError("PREPARE_IDS drifted from AGENT_EXECUTION_CONTRACTS")
    project = project_dir.resolve()
    destination = run_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    config_tree = destination / "config-tree"
    _materialize_config_tree(config_tree, project)
    manifest_path = destination / "deployment.yaml"
    _write_deployment_manifest(
        manifest_path,
        spec=spec,
        project_scope=str(project),
        opencode_endpoint=opencode_endpoint,
    )
    built = build_deployment_wheel(manifest_path, destination / "binding-wheel")
    _extract_binding_wheel(built.wheel, destination / "binding-extract")
    composition = resolve_assurance_composition(
        AssuranceCompositionRequest(
            product_entrypoint="assurance-opencode",
            deployment_source=WheelPluginSource(
                distribution=built.distribution,
                entrypoint_name="deployment",
                declaration_path=built.declaration_path,
            ),
            configuration_tree=ConfigTreePluginSource(path=config_tree),
        )
    )
    AssuranceProductApplication().compile(
        composition,
        product=spec.product,
        config_tree=str(config_tree),
    )
    catalog_ref = _resource_ref(composition, _CATALOG_RESOURCE_ID)
    payload: dict[str, Any] = {
        "schema_version": "1",
        "change_id": change_id,
        "requirement": spec.requirement,
        "run_mode": "case",
        "candidate_test_families": list(spec.candidate_test_families),
        "case_delta_paths": [f"qa/cases/{module}/case.yaml" for module in spec.case_modules],
        "capability_leafs": _catalog_leafs(composition, catalog_ref["resource_id"]),
        "capability_catalog": catalog_ref,
        "product_policy": _resource_ref(composition, _POLICY_RESOURCE_ID),
        "data_knowledge": _resource_ref(composition, _KNOWLEDGE_RESOURCE_ID),
        "allowed_artifact_paths": list(LOCKED_ALLOWED_ARTIFACT_PATHS),
        "budgets": spec.budgets.model_dump(mode="json"),
    }
    value = ProductInputV1.model_validate(payload)
    value.validate_for_entrypoint(spec.entrypoint).authenticate_against(composition)
    input_path = destination / "product-input.json"
    input_path.write_text(
        json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "product": spec.product,
        "binding_dist": built.distribution,
        "binding_declaration": built.declaration_path,
        "config_tree": config_tree,
        "input_path": input_path,
    }
