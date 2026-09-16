"""Fixed mappings that prove deleted legacy packages stay gone."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import subprocess


OWNERSHIP_FIXTURES = Path("tests/capabilities/fixtures")
DELETION_INVENTORY_PATH = OWNERSHIP_FIXTURES / "deletion.txt"

PRODUCTION_METADATA_FILES = (
    "pyproject.toml",
    ".importlinter",
    "uv.lock",
    "packages/products/assurance-product/pyproject.toml",
    "packages/capabilities/assurance-intake/pyproject.toml",
    "packages/capabilities/assurance-generation/pyproject.toml",
    "packages/capabilities/assurance-execution/pyproject.toml",
    "packages/capabilities/assurance-healing/pyproject.toml",
    "packages/capabilities/assurance-quality/pyproject.toml",
    "packages/capabilities/assurance-improvement/pyproject.toml",
    "packages/framework/graph-engine/pyproject.toml",
    "packages/adapters/agent-runtime-contracts/pyproject.toml",
    "packages/adapters/agent-runtime-opencode/pyproject.toml",
)

PRODUCTION_PACKAGE_ROOTS = (
    "packages/products/assurance-product/assurance_product",
    "packages/capabilities/assurance-intake/assurance_intake",
    "packages/capabilities/assurance-generation/assurance_generation",
    "packages/capabilities/assurance-execution/assurance_execution",
    "packages/capabilities/assurance-healing/assurance_healing",
    "packages/capabilities/assurance-quality/assurance_quality",
    "packages/capabilities/assurance-improvement/assurance_improvement",
    "packages/framework/graph-engine/graph_engine",
    "packages/adapters/agent-runtime-contracts/agent_runtime_contracts",
    "packages/adapters/agent-runtime-opencode/agent_runtime_opencode",
)

OWNER_REPLACEMENT_TESTS = {
    "assurance.intake": "packages/capabilities/assurance-intake/tests/test_plugin.py",
    "assurance.generation": "packages/capabilities/assurance-generation/tests/test_contracts.py",
    "assurance.execution": "packages/capabilities/assurance-execution/tests/test_contracts.py",
    "assurance.healing": "packages/capabilities/assurance-healing/tests/test_contracts.py",
    "assurance.quality": "packages/capabilities/assurance-quality/tests/test_contracts.py",
    "assurance.improvement": "packages/capabilities/assurance-improvement/tests/test_plugin.py",
}

ASSEMBLY_REPLACEMENT_TEST = "tests/product/test_full_graph_audit.py"
KERNEL_BASELINE_COMMIT = "47941b15b9fd6a4d4f422d8e7bc138482e362528"
KERNEL_PACKAGE_PREFIX = "packages/assurance-kernel/"
KERNEL_DESTINATIONS = frozenset(
    {
        "graph-engine",
        "assurance-product",
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
        "assurance.improvement",
        "obsolete",
    }
)
KERNEL_DESTINATION_ROOTS = {
    "graph-engine": "packages/framework/graph-engine",
    "assurance-product": "packages/products/assurance-product",
    "assurance.intake": "packages/capabilities/assurance-intake",
    "assurance.generation": "packages/capabilities/assurance-generation",
    "assurance.execution": "packages/capabilities/assurance-execution",
    "assurance.healing": "packages/capabilities/assurance-healing",
    "assurance.quality": "packages/capabilities/assurance-quality",
    "assurance.improvement": "packages/capabilities/assurance-improvement",
}
_KERNEL_EXACT_OWNERS: dict[str, str] = {
    "packages/assurance-kernel/pyproject.toml": "obsolete",
    "packages/assurance-kernel/assurance_kernel/__init__.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/__main__.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/change_location.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/config.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/exceptions.py": "graph-engine",
    "packages/assurance-kernel/assurance_kernel/identifiers.py": "graph-engine",
    "packages/assurance-kernel/assurance_kernel/product.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/resources.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/artifacts/__init__.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/artifacts/batch_id.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/artifacts/canonical.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/artifacts/paths.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/artifacts/registry.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/artifacts/validate.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/artifacts/policy.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/artifacts/policy_obligations.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/artifacts/repo_registry.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/__init__.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/common.py": "assurance.intake",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/cases.py": "assurance.intake",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/assurance.py": "assurance.generation",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/codegen.py": "assurance.generation",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/generated_files.py": "assurance.generation",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/plan_checks.py": "assurance.generation",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/review.py": "assurance.generation",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/discovery.py": "assurance.generation",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/execution.py": "assurance.execution",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/healing.py": "assurance.healing",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/healing_codegen.py": "assurance.healing",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/coverage_repair.py": "assurance.healing",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/coverage_gaps.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/c_layer.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/explore.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/inspect.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/issue_events.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/issues.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/metrics.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/minimum_coverage.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/pr_metric_evidence.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/quarantine.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/report.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/sufficiency.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/trace.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/trace_sufficiency.py": "assurance.quality",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/improvement_outbox.py": "assurance.improvement",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/improvement_review.py": "assurance.improvement",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/improvements.py": "assurance.improvement",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/declarations.py": "assurance.improvement",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/promotion.py": "assurance.improvement",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/retro_batch.py": "assurance.improvement",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/retro_v3.py": "assurance.improvement",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/data_knowledge.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/policy.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/state.py": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/artifacts/models/eval_projection.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/_resources/schemas/policy-default.yaml": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/_resources/schemas/ingest-artifact-catalog.yaml": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/_resources/rules/failure-classification.yaml": "obsolete",
    "packages/assurance-kernel/assurance_kernel/_resources/schemas/explore-advisory.schema.json": "obsolete",
    "packages/assurance-kernel/assurance_kernel/_resources/schemas/explore-context.schema.json": "obsolete",
    "packages/assurance-kernel/assurance_kernel/_resources/opencode/INSTALL.md": "obsolete",
    "packages/assurance-kernel/assurance_kernel/_resources/opencode/plugins/aa.mjs": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/_resources/opencode/tools/artifact_write.ts": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/_resources/opencode/tools/workflow_start.ts": "assurance-product",
    "packages/assurance-kernel/assurance_kernel/workflow/core/product_hooks.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/workflow/graph/product_hooks.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/workflow/driver/adapter.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/workflow/driver/adapter_factory.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/workflow/driver/headless_adapter.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/workflow/driver/opencode_adapter.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/workflow/skill_memory.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/workflow/__init__.py": "graph-engine",
    "packages/assurance-kernel/assurance_kernel/verification/__init__.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/verification/checks/__init__.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/knowledge/__init__.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/evidence/__init__.py": "obsolete",
    "packages/assurance-kernel/assurance_kernel/workflow/core/__init__.py": "graph-engine",
    "packages/assurance-kernel/assurance_kernel/workflow/graph/__init__.py": "graph-engine",
    "packages/assurance-kernel/assurance_kernel/workflow/graph/handlers/__init__.py": "graph-engine",
    "packages/assurance-kernel/assurance_kernel/workflow/driver/__init__.py": "graph-engine",
    "packages/assurance-kernel/assurance_kernel/workflow/orchestration/__init__.py": "graph-engine",
}
_KERNEL_VERIFICATION_GENERATION = frozenset(
    {
        "applicability.py",
        "assert_ideal.py",
        "base.py",
        "capability_keys.py",
        "l1_path.py",
        "registry.py",
        "shared_factory.py",
        "contract_render.py",
        "generated_entries.py",
        "generated_files.py",
        "manifest.py",
        "oracle.py",
        "plan_checks.py",
        "profile_manifest.py",
        "profiles.py",
        "property_scan.py",
    }
)
_KERNEL_VERIFICATION_QUALITY = frozenset(
    {
        "assertion_class.py",
        "baseline_history.py",
        "gate_state.py",
        "mutation_cache.py",
        "mutation_runner.py",
        "mutation_sampling.py",
        "promotion_gate.py",
        "replay.py",
    }
)
_KERNEL_PREFIX_OWNERS: tuple[tuple[str, str], ...] = (
    ("packages/assurance-kernel/assurance_kernel/workflow/graph/", "graph-engine"),
    ("packages/assurance-kernel/assurance_kernel/workflow/core/", "graph-engine"),
    ("packages/assurance-kernel/assurance_kernel/workflow/orchestration/", "graph-engine"),
    ("packages/assurance-kernel/assurance_kernel/workflow/driver/", "graph-engine"),
    ("packages/assurance-kernel/assurance_kernel/evidence/", "assurance.quality"),
    ("packages/assurance-kernel/assurance_kernel/knowledge/", "assurance.intake"),
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def production_runtime_files(repo_root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for relative in PRODUCTION_METADATA_FILES:
        path = repo_root / relative
        if path.is_file():
            files.append(path)
    for relative in PRODUCTION_PACKAGE_ROOTS:
        root = repo_root / relative
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                files.append(path)
    return tuple(sorted(files))


def agent_deletion_paths(repo_root: Path) -> tuple[str, ...]:
    text = (repo_root / DELETION_INVENTORY_PATH).read_text(encoding="utf-8")
    return tuple(line.strip() for line in text.splitlines() if line.startswith("assurance_agent/"))


def kernel_deletion_paths(repo_root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            KERNEL_BASELINE_COMMIT,
            "--",
            "packages/assurance-kernel",
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    _require(result.returncode == 0, "cannot list committed kernel sources")
    return tuple(line for line in result.stdout.splitlines() if line)


def live_kernel_paths(repo_root: Path) -> tuple[str, ...]:
    root = repo_root / "packages" / "assurance-kernel"
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path.relative_to(repo_root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    )


def kernel_path_owner(path: str) -> str:
    owner = _KERNEL_EXACT_OWNERS.get(path)
    if owner is not None:
        return owner
    if path.startswith("packages/assurance-kernel/assurance_kernel/verification/"):
        name = Path(path).name
        if name in _KERNEL_VERIFICATION_GENERATION:
            return "assurance.generation"
        if name in _KERNEL_VERIFICATION_QUALITY:
            return "assurance.quality"
        raise ValueError(f"unmapped kernel verification path: {path}")
    for prefix, mapped in _KERNEL_PREFIX_OWNERS:
        if path.startswith(prefix):
            return mapped
    raise ValueError(f"unmapped kernel path: {path}")


def ownership_legacy_id_for_path(path: str) -> str:
    skill_prefix = "assurance_agent/_resources/skills/"
    persona_prefix = "assurance_agent/_resources/opencode/agents/"
    if path.startswith(skill_prefix) and path.endswith("/SKILL.md"):
        return path[len(skill_prefix) :].split("/", 1)[0]
    if path.startswith(persona_prefix) and path.endswith(".md"):
        return Path(path).stem
    if path == "assurance_agent/workflow/driver/operations_catalog.py":
        return "operation:run-tests"
    return path


def deletion_proof(item: Mapping[str, object]) -> str:
    verification = item.get("verification")
    if isinstance(verification, str) and (
        verification.startswith("tests/") or verification.startswith("packages/")
    ):
        proof = verification.split("::", 1)[0]
        return proof.replace("packages/features/", "packages/capabilities/", 1)
    disposition = item.get("disposition")
    if disposition in {"delete_phase6", "retain_harness"}:
        return "obsolete"
    if disposition == "replace_phase5":
        return ASSEMBLY_REPLACEMENT_TEST
    owner = item.get("owner")
    if isinstance(owner, str) and owner in OWNER_REPLACEMENT_TESTS:
        return OWNER_REPLACEMENT_TESTS[owner]
    raise ValueError(f"unmapped deletion record: {item.get('legacy_id')}")
