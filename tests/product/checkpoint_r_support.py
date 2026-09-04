from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from tests.product.cli_support import SECRET_ENV, SECRET_HANDLE, write_project_dir
from tests.product.composition_harness import request_for
from tests.product.test_feature_graph_bundles import PUBLIC_BUNDLE_FIELDS, _build_owner

PROTECTED_ENV_NAMES = (
    "CHECKPOINT_R_OPENCODE_AUTH_JSON",
    "CHECKPOINT_R_OPENCODE_PROVIDER_JSON",
    "CHECKPOINT_R_OPENCODE_BINARY_SHA256",
    "CHECKPOINT_R_SERVER_SECRET",
)
MIN_OPENCODE_VERSION = (1, 18, 26)
LIVE_REQUEST_TIMEOUT_SECONDS = 300
LIVE_OBSERVATION_HORIZON_SECONDS = 3600
LIVE_PROGRESS_TIMEOUT_SECONDS = 1800
LIVE_POLL_INTERVAL_SECONDS = 1.0
LIVE_CANCEL_TIMEOUT_SECONDS = 30
LIVE_MAX_RESPONSE_BYTES = 4_000_000
LIVE_MAX_OUTPUT_BYTES = 4_000_000
LIVE_ROUTE_MAX_SECONDS = 3600
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHANGE_ID = "CH-R-001"
_DIGEST = "a" * 64
_CAPABILITY_LEAF = "demo_capability"
_ISSUE_ANALYSIS_ID = "assurance.quality.agent.issue-analysis.v1"
_CASE_DESIGN_ID = "assurance.intake.agent.case-design.v1"
_EVALUATE_ID = "assurance.improvement.task.evaluate-memory-improvement"
_COMPOSITION: Any = None
_FAMILIES = ("api", "e2e", "fuzz", "performance")
_CASE_TYPES = {"api": "API", "e2e": "E2E", "fuzz": "Fuzz", "performance": "Performance"}
_FRAMEWORKS = {
    "api": "pytest",
    "e2e": "pytest-playwright",
    "fuzz": "schemathesis",
    "performance": "locust",
}
_PLAN_OUTPUT_NAMES = {
    "api": (
        "api-plan.md",
        "api-test-data-plan.md",
        "api-codegen-plan.md",
        "api-codegen-mapping.json",
        "m3-review-summary.md",
    ),
    "e2e": (
        "e2e-plan.md",
        "e2e-test-data-plan.md",
        "e2e-codegen-plan.md",
        "e2e-codegen-mapping.json",
        "m4-review-summary.md",
    ),
    "fuzz": (
        "fuzz-plan.md",
        "fuzz-codegen-plan.md",
        "fuzz-codegen-mapping.json",
        "fuzz-review-summary.md",
    ),
    "performance": (
        "performance-plan.md",
        "performance-codegen-plan.md",
        "performance-codegen-mapping.json",
        "performance-review-summary.md",
    ),
}


class CheckpointRPreflightError(RuntimeError):
    """Raised when a required protected Checkpoint R input is missing."""


@dataclass(frozen=True, slots=True)
class CandidateInventory:
    agent_contracts: tuple[str, ...]
    agent_occurrences: tuple[str, ...]
    semantic_contracts: tuple[str, ...]
    attempt_occurrences: tuple[str, ...]
    agent_contract_ids: tuple[str, ...]
    runtime_binding_digests: tuple[str, ...]
    candidate_sha: str


@dataclass
class LiveAgentRow:
    contract_id: str
    contract_digest: str
    attempt_key_digest: str = ""


@dataclass(frozen=True, slots=True)
class CheckpointRReceipt:
    attempt_key_digest: str
    contract_digest: str
    source_terminal_receipt_digest: str
    promotion_receipt_digest: str
    product_lock_digest: str
    graph_revision: str
    binding_digest: str


@dataclass(frozen=True, slots=True)
class CheckpointRResult:
    receipt: CheckpointRReceipt
    evidence: Mapping[str, object]


def candidate_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, text=True).strip()


def installed_agent_contract_ids() -> tuple[str, ...]:
    from assurance_product.agent_contracts import all_feature_agent_contracts

    return tuple(sorted(all_feature_agent_contracts()))


def _row_digest(row: object) -> str:
    from graph_engine.canonical import canonical_digest

    return canonical_digest(row.model_dump(mode="json"))  # type: ignore[attr-defined]


def locked_runtime_binding_digests(composition: object | None = None) -> tuple[str, ...]:
    from assurance_product.agent_contracts import all_feature_agent_contracts
    from assurance_product.runtime_bindings import (
        authenticate_raw_agent_runtime_bindings,
        raw_agent_runtime_binding_rows,
    )

    resolved = composition if composition is not None else _COMPOSITION
    if resolved is None:
        raise CheckpointRPreflightError("locked runtime binding digests require a compiled composition")
    from graph_engine.composition import FrozenComposition

    rows = authenticate_raw_agent_runtime_bindings(
        raw_agent_runtime_binding_rows(cast(FrozenComposition, resolved)),
        all_feature_agent_contracts(),
        adapter="opencode",
    )
    return tuple(_row_digest(row) for row in rows)


_BOUND_CACHE: tuple[str, ...] | None = None


def _bound_contract_ids() -> tuple[str, ...]:
    global _BOUND_CACHE
    if _BOUND_CACHE is None:
        bound: list[str] = []
        for owner_id in PUBLIC_BUNDLE_FIELDS:
            _bundle, context, _digest = _build_owner(owner_id)
            bound.extend(context.bound_contract_ids)
        _BOUND_CACHE = tuple(bound)
    return _BOUND_CACHE


def exact_occurrences(bound: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from assurance_product.agent_contracts import (
        all_feature_agent_contracts,
        all_feature_task_contracts,
    )

    agents = set(all_feature_agent_contracts())
    tasks = {contract.contract_id for contract in all_feature_task_contracts().values()}
    agent_occurrences = tuple(item for item in bound if item in agents)
    attempt_occurrences = tuple(item for item in bound if item in agents or item in tasks)
    if agent_occurrences.count(_CASE_DESIGN_ID) != 2:
        raise AssertionError("case-design must occur twice")
    if agent_occurrences.count(_ISSUE_ANALYSIS_ID) != 2:
        raise AssertionError("issue-analysis must occur twice")
    if attempt_occurrences.count(_EVALUATE_ID) != 2:
        raise AssertionError("evaluate-memory-improvement must occur twice")
    return agent_occurrences, attempt_occurrences


def count_exact_agent_occurrences(bound: Sequence[str] | None = None) -> int:
    occurrences, _attempts = exact_occurrences(bound if bound is not None else _bound_contract_ids())
    return len(occurrences)


def build_candidate_inventory(composition: object, bound: Sequence[str]) -> CandidateInventory:
    from assurance_product.agent_contracts import (
        all_feature_agent_contracts,
        all_feature_task_contracts,
    )

    agents = all_feature_agent_contracts()
    tasks = {contract.contract_id for contract in all_feature_task_contracts().values()}
    agent_ids = tuple(sorted(agents))
    semantic_ids = tuple(sorted(set(agent_ids) | tasks))
    agent_occurrences, attempt_occurrences = exact_occurrences(bound)
    return CandidateInventory(
        agent_contracts=agent_ids,
        agent_occurrences=agent_occurrences,
        semantic_contracts=semantic_ids,
        attempt_occurrences=attempt_occurrences,
        agent_contract_ids=agent_ids,
        runtime_binding_digests=locked_runtime_binding_digests(composition),
        candidate_sha=candidate_sha(),
    )


def installed_live_agent_rows() -> tuple[LiveAgentRow, ...]:
    from graph_engine.canonical import JSONValue, canonical_digest

    from assurance_product.agent_contracts import all_feature_agent_contracts

    contracts = all_feature_agent_contracts()
    return tuple(
        LiveAgentRow(
            contract_id=contract_id,
            contract_digest=canonical_digest(cast(JSONValue, contracts[contract_id].canonical_projection())),
        )
        for contract_id in sorted(contracts)
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip():
        raise CheckpointRPreflightError(f"missing protected input: {name}")
    return value


def _runner_temp() -> Path:
    raw = os.environ.get("RUNNER_TEMP")
    if not raw:
        raise CheckpointRPreflightError("missing protected input: RUNNER_TEMP")
    path = Path(raw)
    if not path.is_dir():
        raise CheckpointRPreflightError("RUNNER_TEMP must be a directory outside the repository")
    try:
        path.resolve().relative_to(_REPO_ROOT.resolve())
    except ValueError:
        return path
    raise CheckpointRPreflightError("RUNNER_TEMP must not be inside the repository")


def _parse_version(raw: str) -> tuple[int, ...]:
    token = raw.strip().splitlines()[-1].lstrip("v")
    return tuple(int(part) for part in token.split(".")[:3])


def require_checkpoint_r_preflight() -> dict[str, object]:
    if os.environ.get("AA_CHECKPOINT_R_LIVE") != "1":
        raise CheckpointRPreflightError("missing protected input: AA_CHECKPOINT_R_LIVE=1")
    if os.environ.get("OPENCODE_MODEL"):
        raise CheckpointRPreflightError("OPENCODE_MODEL override is not accepted")
    runner_temp = _runner_temp()
    values = {name: _require_env(name) for name in PROTECTED_ENV_NAMES}
    expected = values["CHECKPOINT_R_OPENCODE_BINARY_SHA256"].strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise CheckpointRPreflightError("CHECKPOINT_R_OPENCODE_BINARY_SHA256 must be a sha256 hex digest")
    binary = (
        _require_env("AA_CHECKPOINT_R_OPENCODE_BINARY")
        if os.environ.get("AA_CHECKPOINT_R_OPENCODE_BINARY")
        else ""
    )
    if not binary:
        from shutil import which

        found = which("opencode")
        if found is None:
            raise CheckpointRPreflightError("missing official OpenCode binary")
        binary = found
    digest = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
    if digest != expected:
        raise CheckpointRPreflightError("OpenCode binary digest does not match the protected digest")
    version = _parse_version(subprocess.check_output([binary, "--version"], text=True))
    if version < MIN_OPENCODE_VERSION:
        raise CheckpointRPreflightError(
            f"official OpenCode binary must be >= {'.'.join(str(part) for part in MIN_OPENCODE_VERSION)}"
        )
    provider = json.loads(values["CHECKPOINT_R_OPENCODE_PROVIDER_JSON"])
    if not isinstance(provider, dict) or not isinstance(provider.get("provider"), dict):
        raise CheckpointRPreflightError("CHECKPOINT_R_OPENCODE_PROVIDER_JSON must carry a provider object")
    model = provider.get("model")
    if not isinstance(model, str) or not model.strip():
        raise CheckpointRPreflightError("protected provider configuration must lock an exact model")
    rows = installed_live_agent_rows()
    if len(rows) != 33:
        raise CheckpointRPreflightError(f"expected 33 live rows, found {len(rows)}")
    sha = os.environ.get("GITHUB_SHA") or ""
    head = candidate_sha()
    if len(head) != 40 or sha != head:
        raise CheckpointRPreflightError("clean checkout must be bound to GITHUB_SHA")
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=_REPO_ROOT, text=True)
    if status.strip():
        raise CheckpointRPreflightError("checkout is not clean")
    return {
        "binary": binary,
        "binary_digest": digest,
        "binary_version": version,
        "model": model.strip(),
        "provider": provider["provider"],
        "runner_temp": runner_temp,
        "rows": rows,
        "candidate_sha": head,
        "server_secret": values["CHECKPOINT_R_SERVER_SECRET"],
        "auth_json": values["CHECKPOINT_R_OPENCODE_AUTH_JSON"],
        "provider_json": values["CHECKPOINT_R_OPENCODE_PROVIDER_JSON"],
    }


def materialize_protected_inputs(preflight: Mapping[str, object]) -> dict[str, Path | str]:
    runner_temp = Path(str(preflight["runner_temp"]))
    auth_path = runner_temp / "opencode-auth.json"
    provider_path = runner_temp / "opencode-provider.json"
    auth_path.write_text(str(preflight["auth_json"]), encoding="utf-8")
    provider_path.write_text(str(preflight["provider_json"]), encoding="utf-8")
    auth_path.chmod(0o600)
    provider_path.chmod(0o600)
    return {"auth_path": auth_path, "provider_path": provider_path}


def apply_live_deployment_overrides(
    document: dict[str, Any],
    *,
    endpoint: str,
    model: str,
    project_scope: str,
) -> dict[str, Any]:
    binding = document["adapter_binding"]
    binding["endpoint"] = endpoint
    binding["project_scope"] = project_scope
    binding["request_timeout_seconds"] = LIVE_REQUEST_TIMEOUT_SECONDS
    binding["observation_horizon_seconds"] = LIVE_OBSERVATION_HORIZON_SECONDS
    binding["progress_timeout_seconds"] = LIVE_PROGRESS_TIMEOUT_SECONDS
    binding["poll_interval_seconds"] = LIVE_POLL_INTERVAL_SECONDS
    binding["cancel_timeout_seconds"] = LIVE_CANCEL_TIMEOUT_SECONDS
    binding["max_response_bytes"] = LIVE_MAX_RESPONSE_BYTES
    if float(binding["observation_horizon_seconds"]) <= 30:
        raise CheckpointRPreflightError("live package must not lock the 30s fixture horizon")
    for route in document["routes"].values():
        route["provider_model"] = model
        route.setdefault("limits", {})["max_seconds"] = LIVE_ROUTE_MAX_SECONDS
    for policy in document.get("request_policies", {}).values():
        policy["max_output_bytes"] = LIVE_MAX_OUTPUT_BYTES
    return document


def _family_case(family: str) -> dict[str, object]:
    case: dict[str, object] = {
        "case_id": f"TC_{family.upper()}_001",
        "title": f"{family} happy path",
        "status": "active",
        "priority": "P1",
        "severity": "major",
        "type": _CASE_TYPES[family],
        "module": "demo",
        "requirement_id": "REQ-1",
        "feature_name": "demo-capability",
        "test_condition_id": "COND-1",
        "design_technique": "use_case",
        "objective": f"verify the {family} behavior",
        "summary": f"exercise and assert the {family} behavior",
        "preconditions": [],
        "test_data": [],
        "steps": ["perform the operation"],
        "assertions": ["the operation succeeds"],
        "postconditions": [],
        "edge_cases": [],
        "related_cases": ["TC_API_001"] if family == "fuzz" else [],
        "risk": {
            "level": "high",
            "likelihood": 3,
            "impact": 4,
            "rationale": "important administration path",
        },
        "automation": {
            "required": True,
            "framework": _FRAMEWORKS[family],
            "status": "planned",
        },
        "regression": {
            "candidate": True,
            "tier": "smoke",
            "rationale": "protect the administration path",
            "selection_reason": ["critical_user_journey"],
            "maintenance_rule": "keep_until_feature_deprecated",
        },
        "trace": {_CAPABILITY_LEAF: {"covered": True}},
    }
    if family == "fuzz":
        automation = cast(dict[str, object], case["automation"])
        automation["fuzz"] = {
            "endpoints": [{"method": "POST", "path": "/items"}],
            "property": "item.create.payload",
            "expectations": ["never returns 5xx", "valid payloads remain schema-conformant"],
        }
    if family == "performance":
        automation = cast(dict[str, object], case["automation"])
        automation["performance"] = {
            "scenario": {
                "capability": _CAPABILITY_LEAF,
                "endpoint": "POST /items",
                "load": {
                    "concurrency": 10,
                    "spawn_rate_per_second": 2,
                    "duration_seconds": 60,
                },
                "thresholds": {"p95_ms": 200, "error_rate_max": 0.01},
            }
        }
    return case


def _reviewed_cases(family: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "added": [_family_case(family)],
        "modified": [],
        "removed": [],
    }


def _reviewed_plan(family: str) -> dict[str, object]:
    case_id = f"TC_{family.upper()}_001"
    return {
        "schema_version": "1",
        "family": family,
        "change_id": _CHANGE_ID,
        "case_ids": (case_id,),
        "required_capabilities": (_CAPABILITY_LEAF,),
        "coverage": (
            {
                "case_id": case_id,
                "operation": "write",
                "risk": "low",
                "required_capabilities": (_CAPABILITY_LEAF,),
            },
        ),
        "output_files": tuple(f"qa/changes/{_CHANGE_ID}/plans/{name}" for name in _PLAN_OUTPUT_NAMES[family]),
    }


def required_change_files(change_id: str = _CHANGE_ID) -> tuple[str, ...]:
    change = f"qa/changes/{change_id}"
    plan_files = tuple(
        f"{change}/plans/{name}" for family in _FAMILIES for name in _PLAN_OUTPUT_NAMES[family]
    )
    return (
        f"{change}/.qa.yaml",
        f"{change}/artifact.json",
        f"{change}/proposal.md",
        f"{change}/requirement.md",
        f"{change}/cases/demo/case.yaml",
        f"{change}/trace/minimum-coverage-matrix.json",
        f"{change}/generated/demo.py",
        f"{change}/mapping.json",
        f"{change}/repair.py",
        "tests/demo/test_demo.py",
        *plan_files,
    )


def seed_change_workspace(project: Path, change_id: str = _CHANGE_ID) -> tuple[str, ...]:
    import json

    import yaml

    change = project / "qa" / "changes" / change_id
    required = required_change_files(change_id)
    case_document = {
        "schema_version": "1.0",
        "added": [_family_case(family) for family in _FAMILIES],
        "modified": [],
        "removed": [],
    }
    files: dict[str, str] = {
        f"qa/changes/{change_id}/.qa.yaml": f"schema_version: '1.0'\nchange_id: {change_id}\n",
        f"qa/changes/{change_id}/artifact.json": json.dumps({"change_id": change_id}, indent=2) + "\n",
        f"qa/changes/{change_id}/proposal.md": "# Checkpoint R proposal\n",
        f"qa/changes/{change_id}/requirement.md": "# Checkpoint R requirement\n",
        f"qa/changes/{change_id}/cases/demo/case.yaml": yaml.safe_dump(case_document, sort_keys=False),
        f"qa/changes/{change_id}/trace/minimum-coverage-matrix.json": json.dumps(
            [
                {
                    "mrc_id": "MRC-1",
                    "key": _CAPABILITY_LEAF,
                    "required": True,
                    "covered_by_cases": ["TC_API_001"],
                    "status": "covered",
                    "skip_reason": None,
                    "category": "api",
                    "layer": "api",
                }
            ],
            indent=2,
        )
        + "\n",
        f"qa/changes/{change_id}/generated/demo.py": "def demo() -> None:\n    return None\n",
        f"qa/changes/{change_id}/mapping.json": json.dumps({"change_id": change_id}, indent=2) + "\n",
        f"qa/changes/{change_id}/repair.py": "def repair() -> None:\n    return None\n",
        "tests/demo/test_demo.py": "def test_ok() -> None:\n    assert True\n",
    }
    for family in _FAMILIES:
        for name in _PLAN_OUTPUT_NAMES[family]:
            relative = f"qa/changes/{change_id}/plans/{name}"
            if name.endswith("-codegen-mapping.json"):
                files[relative] = (
                    json.dumps(
                        {
                            "schema_version": "1",
                            "layer": family,
                            "entries": [
                                {
                                    "case_id": f"TC_{family.upper()}_001",
                                    "symbol": f"test_tc_{family}_001__happy_path",
                                    "target_file": f"tests/{family}/test_demo.py",
                                }
                            ],
                        },
                        indent=2,
                    )
                    + "\n"
                )
            else:
                files[relative] = f"# {family} plan\n"
    for relative, content in files.items():
        path = project.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    missing = [
        relative
        for relative in required
        if not project.joinpath(*relative.split("/")).is_file()
        or project.joinpath(*relative.split("/")).is_symlink()
    ]
    if missing:
        raise CheckpointRPreflightError(f"missing required change-local files: {missing}")
    del change
    return required


def require_seeded_workspace(project: Path, change_id: str = _CHANGE_ID) -> None:
    missing = [
        relative
        for relative in required_change_files(change_id)
        if not project.joinpath(*relative.split("/")).is_file()
        or project.joinpath(*relative.split("/")).is_symlink()
    ]
    if missing:
        raise CheckpointRPreflightError(f"missing required change-local files: {missing}")


def ci_run_identity() -> dict[str, str]:
    names = (
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_WORKFLOW",
        "GITHUB_JOB",
        "GITHUB_SHA",
        "RUNNER_NAME",
        "RUNNER_OS",
    )
    return {name.lower(): os.environ.get(name, "") for name in names}


def _project_file_digests(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if "/.runtime/" in f"/{relative}/" or "/.staging/" in f"/{relative}/":
            continue
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _claimed_write_paths(contract: object, change_id: str) -> set[str]:
    claims = contract.phase_write_claims  # type: ignore[attr-defined]
    raw = (*claims.prepare, *claims.runtime, *claims.finalize)
    return {item.replace("{change_id}", change_id) for item in raw}


def _undeclared_project_writes(
    before: Mapping[str, str],
    after: Mapping[str, str],
    claimed: set[str],
) -> tuple[str, ...]:
    undeclared: list[str] = []
    for relative, digest in after.items():
        if before.get(relative) == digest:
            continue
        if relative in claimed or any(
            relative == claim or relative.startswith(f"{claim.rstrip('/')}/") for claim in claimed
        ):
            continue
        undeclared.append(relative)
    return tuple(sorted(undeclared))


def _measure_prompt_count(events: Sequence[object]) -> int:
    from graph_engine.attempts.events import ActivityBound

    digests: set[str] = set()
    for event in events:
        if not isinstance(event, ActivityBound):
            continue
        reference = event.reference
        if not isinstance(reference, Mapping):
            continue
        token = reference.get("prompt_body_digest") or reference.get("expected_message_id")
        if isinstance(token, str) and token.strip():
            digests.add(token)
    return len(digests)


def _source_and_wheel_digests(product_lock: object) -> dict[str, object]:
    plugins = {
        plugin.plugin_id: plugin.source.digest
        for plugin in product_lock.plugins  # type: ignore[attr-defined]
    }
    return {
        "engine": product_lock.engine.digest,  # type: ignore[attr-defined]
        "product": product_lock.product.source.digest,  # type: ignore[attr-defined]
        "plugins": plugins,
    }


def _entrypoint_for(contract_id: str) -> str:
    from assurance_product.application import ENTRYPOINT_AGENT_CONTRACT_IDS

    for name, contract_ids in ENTRYPOINT_AGENT_CONTRACT_IDS.items():
        if contract_id in contract_ids:
            return name
    return "full"


def installed_contract_input(contract: object) -> BaseModel:
    from assurance_execution.contracts.selection import ClosedMappingEntryV1, ClosedMappingV1, SelectedTargets
    from assurance_healing.contracts.coverage_repair import CoverageRepairBrief
    from assurance_improvement.contracts.retro import RetroSourceManifestV3

    model = contract.input_model  # type: ignore[attr-defined]
    contract_id = str(contract.contract_id)  # type: ignore[attr-defined]
    leafs = ("demo_capability",)
    artifact = f"qa/changes/{_CHANGE_ID}/artifact.json"
    if contract_id.startswith("assurance.intake.agent"):
        payload: dict[str, object] = {
            "change_id": _CHANGE_ID,
            "capability_leafs": leafs,
            "artifact_paths": (artifact,),
        }
        if contract_id.endswith("intake.v1"):
            payload["requirement"] = "Prove the installed intake contract through Product ports."
        if contract_id.endswith("case-design.v1") or contract_id.endswith("case-review.v1"):
            payload["case_delta_paths"] = (f"qa/changes/{_CHANGE_ID}/cases/demo/case.yaml",)
        return model.model_validate(payload)
    if contract_id.startswith("assurance.generation.agent"):
        payload = {
            "change_id": _CHANGE_ID,
            "capability_leafs": leafs,
            "artifact_paths": (artifact,),
        }
        if contract_id.endswith("codegen-fix.v1"):
            family = contract_id.removeprefix("assurance.generation.agent.").removesuffix(".codegen-fix.v1")
            payload.update(
                {
                    "allowed_paths": (f"qa/changes/{_CHANGE_ID}/generated/demo.py",),
                    "approved_proposal": {"status": "approved", "id": "proposal-1"},
                    "reviewed_plan": _reviewed_plan(family),
                    "reviewed_cases": _reviewed_cases(family),
                    "family_constraints": {
                        "write_roots": (f"qa/changes/{_CHANGE_ID}/generated",),
                        "operations": ("write",),
                        "risks": ("low",),
                    },
                    "baseline_tree_id": _DIGEST,
                }
            )
        return model.model_validate(payload)
    if contract_id.startswith("assurance.execution.agent"):
        selected = ("tests/demo/test_demo.py::test_ok",)
        return model.model_validate(
            {
                "change_id": _CHANGE_ID,
                "capability_leafs": leafs,
                "artifact_paths": (artifact,),
                "batch_id": "batch-1",
                "case_ids": ("CASE1",),
                "mapping": ClosedMappingV1(
                    selected=selected,
                    mappings=(
                        ClosedMappingEntryV1(
                            test=selected[0],
                            case_id="CASE1",
                            capability="demo_capability",
                            layer="api",
                        ),
                    ),
                ),
                "selected_targets": SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
                "baseline_tree_id": _DIGEST,
                "runner_profile_digest": _DIGEST,
            }
        )
    if contract_id.startswith("assurance.quality.agent"):
        return model.model_validate(
            {
                "change_id": _CHANGE_ID,
                "capability_leafs": leafs,
                "artifact_paths": (artifact,),
                "batch_id": "batch-1",
                "execution_digest": _DIGEST,
                "healing_digest": _DIGEST,
                "trace_digest": _DIGEST,
                "coverage_digest": _DIGEST,
                "metrics_digest": _DIGEST,
                "case_digest": _DIGEST,
                "plan_digest": _DIGEST,
                "mapping_digest": _DIGEST,
                "issue_digest": _DIGEST,
            }
        )
    if contract_id.startswith("assurance.healing.agent.fix-proposal"):
        return model.model_validate(
            {
                "change_id": _CHANGE_ID,
                "owner_id": "assurance.healing",
                "capability_leafs": leafs,
                "allowed_paths": (f"qa/changes/{_CHANGE_ID}/repair.py",),
                "allowed_roots": ("qa",),
                "baseline_digest": _DIGEST,
                "candidate_digest": _DIGEST,
                "policy_digest": _DIGEST,
                "mapping_paths": (f"qa/changes/{_CHANGE_ID}/mapping.json",),
                "execution_evidence_digest": _DIGEST,
            }
        )
    if contract_id.startswith("assurance.healing.agent.coverage-repair"):
        return model.model_validate(
            {
                "change_id": _CHANGE_ID,
                "brief": CoverageRepairBrief(
                    change_id=_CHANGE_ID,
                    probe_verdict="skipped",
                    eligible=False,
                ),
                "baseline_digest": _DIGEST,
                "allowed_roots": ("qa",),
            }
        )
    if contract_id.startswith("assurance.improvement.agent"):
        return model.model_validate(
            {
                "change_id": _CHANGE_ID,
                "retro_id": "RETRO-1",
                "source_manifest": RetroSourceManifestV3(
                    issue_slice_sha256=_DIGEST,
                    workflow_slice_sha256=_DIGEST,
                    eval_slice_sha256=_DIGEST,
                ),
                "context_digest": _DIGEST,
                "quality_report_digest": _DIGEST,
                "metrics_digest": _DIGEST,
                "issue_digest": _DIGEST,
                "subject_digest": _DIGEST,
                "expected_improvement_version": 1,
                "improvement_id": "IMP-1",
                "invocation_id": "inv-checkpoint-r",
                "archive_digest": _DIGEST,
            }
        )
    raise AssertionError(f"missing installed input fixture for {contract_id}")


def _live_composition(installed_sources: object) -> object:
    from graph_engine.composition import ConfigTreePluginSource, WheelPluginSource

    from assurance_product.product import AssuranceCompositionRequest, resolve_assurance_composition

    dist = os.environ.get("AA_CHECKPOINT_R_BINDING_DIST")
    declaration = os.environ.get("AA_CHECKPOINT_R_BINDING_DECLARATION")
    config_tree = os.environ.get("AA_CHECKPOINT_R_CONFIG_TREE")
    if dist and declaration and config_tree:
        return resolve_assurance_composition(
            AssuranceCompositionRequest(
                product_entrypoint="assurance-opencode",
                deployment_source=WheelPluginSource(
                    distribution=dist,
                    entrypoint_name="deployment",
                    declaration_path=declaration,
                ),
                configuration_tree=ConfigTreePluginSource(path=Path(config_tree).resolve()),
            )
        )
    return resolve_assurance_composition(request_for("opencode", installed_sources))  # type: ignore[arg-type]


def _live_authorization():
    from graph_engine.attempts.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )

    locator = os.environ.get("AA_CHECKPOINT_R_SECRET_ENV") or SECRET_ENV
    sources = (
        SecretSourceBinding(
            handle=SECRET_HANDLE,
            source_kind="environment",
            source_locator=locator,
        ),
    )
    return InvocationRuntimeAuthorization(
        schema_version="1",
        secret_sources=sources,
        digest=runtime_authorization_digest(sources),
    )


def _append_evidence(payload: Mapping[str, object]) -> None:
    raw = os.environ.get("AA_CHECKPOINT_R_EVIDENCE")
    if not raw:
        return
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


@dataclass
class ProtectedCandidate:
    workspace: Any
    composition: Any
    authorization: Any
    invocation_id: str
    product_lock: Any
    product_lock_digest: str
    graph_revision: str
    locked_model: str
    preflight: Mapping[str, object]

    def bind_row(self, row: LiveAgentRow) -> LiveAgentRow:
        from graph_engine.attempts.keys import BusinessActivation, derive_attempt_key

        from assurance_product.agent_contracts import all_feature_agent_contracts

        contract = all_feature_agent_contracts()[row.contract_id]
        validated = installed_contract_input(contract)
        key = derive_attempt_key(
            invocation_id=self.invocation_id,
            graph_revision=self.graph_revision,
            public_entrypoint=_entrypoint_for(row.contract_id),
            semantic_node_id=row.contract_id,
            business_activation=BusinessActivation.one_shot(),
            contract_id=row.contract_id,
            validated_input=validated,
        )
        row.attempt_key_digest = key.digest
        return row

    def execute_agent_attempt(self, row: LiveAgentRow) -> CheckpointRResult:
        return asyncio.run(self._execute(row))

    async def _execute(self, row: LiveAgentRow) -> CheckpointRResult:
        from agent_runtime_contracts.schema import thaw_json as thaw_result
        from agent_runtime_contracts.schema import validate_local_agent_result
        from graph_engine.attempts.context import AttemptExecutionContext
        from graph_engine.attempts.keys import AttemptKey
        from graph_engine.attempts.resolutions import CommittedTaskResult
        from graph_engine.canonical import canonical_digest

        from assurance_product.agent_contracts import all_feature_agent_contracts
        from assurance_product.runtime_bindings import raw_agent_runtime_binding_rows
        from assurance_product.runtime_ports import ProductRuntimePorts

        self.bind_row(row)
        contracts = all_feature_agent_contracts()
        contract = contracts[row.contract_id]
        validated = installed_contract_input(contract)
        binding_rows = {item.contract_id: item for item in raw_agent_runtime_binding_rows(self.composition)}
        binding = binding_rows[row.contract_id]
        if binding.model != self.locked_model:
            raise CheckpointRPreflightError(f"locked model drifted for {row.contract_id}")
        require_seeded_workspace(self.workspace.paths.project_root, _CHANGE_ID)
        before = _project_file_digests(self.workspace.paths.project_root)
        root_input_digest = canonical_digest({"contract_id": row.contract_id})
        async with ProductRuntimePorts.open(
            self.workspace,
            self.composition,
            invocation=self.invocation_id,
            authorization=self.authorization,
            reachable_contract_ids=(row.contract_id,),
        ) as ports:
            lease = await ports.backend.lease.acquire(self.invocation_id, owner_id="checkpoint-r")
            try:
                bound = ports.execution_factory(
                    invocation_id=self.invocation_id,
                    entrypoint=_entrypoint_for(row.contract_id),
                    root_input_digest=root_input_digest,
                ).bind(lease)
                resolved = bound.artifact.attempt_contracts[row.contract_id]
                context = AttemptExecutionContext(
                    invocation_id=self.invocation_id,
                    public_entrypoint=_entrypoint_for(row.contract_id),
                    semantic_node_id=row.contract_id,
                    attempt_key=AttemptKey(digest=row.attempt_key_digest),
                    fencing_token=lease.fencing_token,
                )
                key = AttemptKey(digest=row.attempt_key_digest)
                resolution = await ports.kernel.execute_or_recover(key, resolved, validated, context)
                if not isinstance(resolution, CommittedTaskResult):
                    raise AssertionError(f"{row.contract_id} did not commit: {resolution}")
                snapshot = await ports.attempt_journal.load(key)
                reused = await ports.kernel.execute_or_recover(key, resolved, validated, context)
                if not isinstance(reused, CommittedTaskResult):
                    raise AssertionError(f"{row.contract_id} reuse did not commit: {reused}")
                reused_snapshot = await ports.attempt_journal.load(key)
                records = await ports.attempt_journal._load_records(key.digest)
            finally:
                await ports.backend.lease.release(lease)
        if snapshot is None or not snapshot.source_receipt_digest:
            raise AssertionError(f"{row.contract_id} is missing a source terminal receipt")
        if reused_snapshot is None or reused_snapshot.source_receipt_digest != snapshot.source_receipt_digest:
            raise AssertionError(f"{row.contract_id} did not reuse the source host receipt")
        events = tuple(event for record in records for event in record.events)
        prompt_count = _measure_prompt_count(events)
        if prompt_count != 1:
            raise AssertionError(f"{row.contract_id} admitted {prompt_count} prompts, expected 1")
        exact_payload = thaw_result(snapshot.activity_outcome)
        contract.output_model.model_validate(exact_payload)
        validate_local_agent_result(exact_payload, result_model=contract.output_model)
        after = _project_file_digests(self.workspace.paths.project_root)
        undeclared = _undeclared_project_writes(
            before,
            after,
            _claimed_write_paths(contract, _CHANGE_ID),
        )
        if undeclared:
            raise AssertionError(f"{row.contract_id} wrote undeclared project paths: {undeclared}")
        if not snapshot.released:
            raise AssertionError(f"{row.contract_id} is missing resource-release proof")
        phase_write_claims = contract.phase_write_claims.as_projection()
        result_schema_digest = contract.agent_result_schema_digest()
        finalizer_digest = canonical_digest({"finalize_handler_id": contract.finalize_handler_id})
        secret_handle_digest = canonical_digest([item.handle for item in self.authorization.secret_sources])
        policy_digest = canonical_digest(
            {
                "request_policy_handle": binding.policy.request_policy_handle,
                "request_config_handle": binding.policy.request_config_handle,
            }
        )
        resource_digest = self.product_lock.registry_digests.resources
        effect_receipts = [
            {
                "ordinal": item.ordinal,
                "kind": item.kind,
                "intent_digest": item.intent_digest,
                "receipt_digest": item.receipt_digest,
            }
            for item in snapshot.effects
        ]
        receipt = CheckpointRReceipt(
            attempt_key_digest=row.attempt_key_digest,
            contract_digest=row.contract_digest,
            source_terminal_receipt_digest=snapshot.source_receipt_digest,
            promotion_receipt_digest=snapshot.promotion_receipt_digest or "",
            product_lock_digest=self.product_lock_digest,
            graph_revision=self.graph_revision,
            binding_digest=_row_digest(binding),
        )
        binary_version = self.preflight["binary_version"]
        evidence = {
            "kind": "agent",
            "status": "passed",
            "contract_id": row.contract_id,
            "candidate_sha": candidate_sha(),
            "source_digests": _source_and_wheel_digests(self.product_lock),
            "wheel_digests": _source_and_wheel_digests(self.product_lock),
            "opencode_version": ".".join(str(part) for part in cast(tuple[int, ...], binary_version)),
            "opencode_binary_digest": self.preflight["binary_digest"],
            "provider": binding.provider,
            "model": binding.model,
            "runtime_binding_digest": receipt.binding_digest,
            "contract_digest": receipt.contract_digest,
            "result_schema_digest": result_schema_digest,
            "finalizer_digest": finalizer_digest,
            "product_lock_digest": receipt.product_lock_digest,
            "graph_revision": receipt.graph_revision,
            "policy_digest": policy_digest,
            "resource_digest": resource_digest,
            "secret_handle_digest": secret_handle_digest,
            "attempt_key_digest": receipt.attempt_key_digest,
            "source_terminal_receipt_digest": receipt.source_terminal_receipt_digest,
            "promotion_receipt_digest": receipt.promotion_receipt_digest,
            "effect_receipts": effect_receipts,
            "resources_released": snapshot.released,
            "authorization_id": snapshot.authorization_id,
            "ci_run_identity": ci_run_identity(),
            "prompt_count": prompt_count,
            "local_result_validated": True,
            "phase_write_claims": phase_write_claims,
            "no_direct_project_write": True,
            "host_receipt_reused": True,
            "adapter": binding.adapter,
        }
        _append_evidence(evidence)
        return CheckpointRResult(receipt=receipt, evidence=evidence)


@pytest.fixture(scope="session")
def graph_bound_contract_ids() -> tuple[str, ...]:
    return _bound_contract_ids()


@pytest.fixture(scope="session")
def candidate_inventory(graph_bound_contract_ids, opencode_composition) -> Iterator[CandidateInventory]:
    global _COMPOSITION
    previous = _COMPOSITION
    _COMPOSITION = opencode_composition
    try:
        yield build_candidate_inventory(opencode_composition, graph_bound_contract_ids)
    finally:
        _COMPOSITION = previous


@pytest.fixture
def protected_candidate(installed_sources, tmp_path: Path) -> ProtectedCandidate:
    preflight = require_checkpoint_r_preflight()
    materialize_protected_inputs(preflight)
    locator = os.environ.get("AA_CHECKPOINT_R_SECRET_ENV") or SECRET_ENV
    os.environ[locator] = str(preflight["server_secret"])
    composition = _live_composition(installed_sources)
    from assurance_product.product import (
        prepare_change_workspace,
        product_graph_manifest,
        product_lock_from_composition,
    )
    from graph_engine.composition import FrozenComposition

    from assurance_product.runtime_bindings import raw_agent_runtime_binding_rows

    typed = cast(FrozenComposition, composition)
    rows = raw_agent_runtime_binding_rows(typed)
    if {row.model for row in rows} != {str(preflight["model"])}:
        raise CheckpointRPreflightError("deployment package model is not the locked provider model")
    configured = os.environ.get("AA_CHECKPOINT_R_PROJECT_ROOT")
    if configured:
        project = Path(configured).resolve()
        if not project.is_dir():
            raise CheckpointRPreflightError("AA_CHECKPOINT_R_PROJECT_ROOT must be an existing directory")
        write_project_dir(project)
    else:
        project = write_project_dir(tmp_path / "project")
    seed_change_workspace(project, _CHANGE_ID)
    require_seeded_workspace(project, _CHANGE_ID)
    workspace = prepare_change_workspace(project, _CHANGE_ID)
    product_lock = product_lock_from_composition(typed)
    manifest = product_graph_manifest(typed, product_lock)
    return ProtectedCandidate(
        workspace=workspace,
        composition=composition,
        authorization=_live_authorization(),
        invocation_id="inv-checkpoint-r",
        product_lock=product_lock,
        product_lock_digest=product_lock.digest,
        graph_revision=manifest.revision.revision_id,
        locked_model=str(preflight["model"]),
        preflight=preflight,
    )


def pytest_collection_modifyitems(config, items) -> None:
    if os.environ.get("AA_CHECKPOINT_R_LIVE") == "1":
        return
    markexpr = (getattr(config.option, "markexpr", None) or "").replace(" ", "")
    if "checkpoint_r_live" in markexpr and "notcheckpoint_r_live" not in markexpr:
        return
    items[:] = [item for item in items if item.get_closest_marker("checkpoint_r_live") is None]
