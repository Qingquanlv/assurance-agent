from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from compare import CASE_IDS
from projection import digest_directory

REPO = Path(__file__).resolve().parents[2]
HARNESS_ROOT = Path(__file__).resolve().parent
PHASE5_TESTS = REPO / "tests" / "phase5"
LEGACY_ROOT = PHASE5_TESTS / "fixtures" / "comparison" / "legacy"
CURRENT_ROOT = PHASE5_TESTS / "fixtures" / "comparison" / "current"
MANIFEST_PATH = HARNESS_ROOT / "comparison-manifest.json"

INPUT_DIGEST = hashlib.sha256(b"phase5-comparison-input-v1").hexdigest()
CHANGED_DIGEST = hashlib.sha256(b"phase5-changed-file").hexdigest()
RECEIPT_DIGEST = hashlib.sha256(b"phase5-receipt").hexdigest()
REPORT_BODY = '{"schema_version":"1","decision":"pass","sections":["summary","coverage"]}\n'
REPORT_DIGEST = hashlib.sha256(REPORT_BODY.encode("utf-8")).hexdigest()
FAMILIES = ("api", "e2e", "fuzz", "performance")
LEGACY_RUNTIME = "legacy-aa"
CURRENT_RUNTIME = "assurance-product"


def write_canonical_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def family_sets(completed: tuple[str, ...]) -> dict[str, list[str]]:
    return {
        "selected_families": list(completed),
        "activated_families": list(completed),
        "completed_families": list(completed),
        "skipped_families": [family for family in FAMILIES if family not in completed],
    }


def family_gates(completed: tuple[str, ...]) -> list[dict[str, str]]:
    gates = [
        {"semantic_role": "intake.review", "decision": "pass", "input_digest": INPUT_DIGEST},
    ]
    for family in completed:
        gates.extend(
            [
                {
                    "semantic_role": f"generation.{family}.plan",
                    "decision": "pass",
                    "input_digest": INPUT_DIGEST,
                },
                {
                    "semantic_role": f"generation.{family}.plan-review",
                    "decision": "pass",
                    "input_digest": INPUT_DIGEST,
                },
                {
                    "semantic_role": f"generation.{family}.codegen",
                    "decision": "pass",
                    "input_digest": INPUT_DIGEST,
                },
                {
                    "semantic_role": f"execution.{family}",
                    "decision": "pass",
                    "input_digest": INPUT_DIGEST,
                },
            ]
        )
    return gates


def default_files(completed: tuple[str, ...]) -> dict[str, str]:
    files = {
        "result-tree/qa/report.json": REPORT_BODY,
    }
    for family in completed:
        files[f"result-tree/tests/{family}/test_item.py"] = (
            f"def test_{family}_item() -> None:\n    assert True\n"
        )
    return files


def default_artifacts(completed: tuple[str, ...]) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = [
        {
            "artifact_id": "qa/report.json",
            "media_type": "application/json",
            "sha256": REPORT_DIGEST,
            "semantic_projection": {"decision": "pass", "sections": ["summary", "coverage"]},
        }
    ]
    for family in completed:
        artifacts.append(
            {
                "artifact_id": f"tests/{family}/test_item.py",
                "media_type": "text/x-python",
                "sha256": hashlib.sha256(
                    f"def test_{family}_item() -> None:\n    assert True\n".encode("utf-8")
                ).hexdigest(),
                "semantic_projection": {"family": family, "mapped": True},
            }
        )
    return artifacts


def default_changed_files(completed: tuple[str, ...]) -> list[dict[str, str]]:
    return [
        {
            "path": f"tests/{family}/test_item.py",
            "sha256": hashlib.sha256(
                f"def test_{family}_item() -> None:\n    assert True\n".encode("utf-8")
            ).hexdigest(),
        }
        for family in completed
    ]


def default_source(case_id: str, runtime_identity: str, completed: tuple[str, ...]) -> dict[str, Any]:
    source: dict[str, Any] = {
        "schema_version": "1",
        "case_id": case_id,
        "input_digest": INPUT_DIGEST,
        "runtime_identity": runtime_identity,
        "terminal_class": "completed",
        "terminal_reason_category": None,
        "gate_decisions": family_gates(completed),
        "artifact_contract": default_artifacts(completed),
        "changed_files": default_changed_files(completed),
        "execution_evidence": {
            "summary": {"passed": len(completed), "failed": 0, "families": list(completed)},
            "digest": CHANGED_DIGEST,
        },
        "quality_metrics": {
            "coverage": {"outcome": "pass", "ratio": 1},
            "trace": {"complete": True},
            "quality": {"decision": "pass"},
        },
        "issue_healing_decisions": [],
        "durable_effects": [],
        "report": {
            "present": True,
            "digest": REPORT_DIGEST,
            "semantic_fields": {"decision": "pass", "sections": ["summary", "coverage"]},
        },
        "retro": None,
        "improvement": None,
        "archive": None,
        "semantic_counts": {"retries": {}, "interrupts": {}, "stops": {}},
        "diagnostics": [{"category": "info", "message": f"{case_id} projected"}],
    }
    source.update(family_sets(completed))
    return source


def case_export(case_id: str, runtime_identity: str) -> tuple[dict[str, Any], dict[str, str]]:
    completed: tuple[str, ...]
    if case_id == "full-e2e-only-success":
        completed = ("e2e",)
    elif case_id == "full-fuzz-only-success":
        completed = ("fuzz",)
    elif case_id == "full-performance-only-success":
        completed = ("performance",)
    elif case_id == "full-all-four-family-success":
        completed = FAMILIES
    else:
        completed = ("api",)
    source = default_source(case_id, runtime_identity, completed)
    files = default_files(completed)
    if case_id == "intake-review-needs-fix-then-pass":
        source["gate_decisions"] = [
            {"semantic_role": "intake.review", "decision": "needs-fix", "input_digest": INPUT_DIGEST},
            {"semantic_role": "intake.review", "decision": "pass", "input_digest": INPUT_DIGEST},
            *family_gates(completed)[1:],
        ]
        source["semantic_counts"]["retries"] = {"intake.review": 1}
    elif case_id == "plan-review-invalid-output-bounded-retry":
        source["gate_decisions"] = [
            {"semantic_role": "intake.review", "decision": "pass", "input_digest": INPUT_DIGEST},
            {
                "semantic_role": "generation.api.plan-review",
                "decision": "invalid-output",
                "input_digest": INPUT_DIGEST,
            },
            {
                "semantic_role": "generation.api.plan-review",
                "decision": "pass",
                "input_digest": INPUT_DIGEST,
            },
            {
                "semantic_role": "generation.api.plan",
                "decision": "pass",
                "input_digest": INPUT_DIGEST,
            },
            {
                "semantic_role": "generation.api.codegen",
                "decision": "pass",
                "input_digest": INPUT_DIGEST,
            },
            {"semantic_role": "execution.api", "decision": "pass", "input_digest": INPUT_DIGEST},
        ]
        source["semantic_counts"]["retries"] = {"generation.api.plan-review": 1}
    elif case_id == "codegen-validation-failure-bounded-fix":
        source["gate_decisions"].extend(
            [
                {
                    "semantic_role": "generation.api.codegen-fix",
                    "decision": "pass",
                    "input_digest": INPUT_DIGEST,
                }
            ]
        )
        source["semantic_counts"]["retries"] = {"generation.api.codegen": 1}
        source["diagnostics"] = [
            {"category": "validation", "message": "codegen failed once then bounded fix passed"}
        ]
    elif case_id == "execution-closed-mapping-no-stale-test":
        source["changed_files"] = default_changed_files(completed)
        source["diagnostics"] = [{"category": "mapping", "message": "closed mapping; no stale unmapped test"}]
    elif case_id == "coverage-insufficient-repair-reexecution-pass":
        source["quality_metrics"]["coverage"] = {
            "outcome": "pass",
            "ratio": 1,
            "repaired": True,
            "reexecuted": True,
        }
        source["semantic_counts"]["retries"] = {"healing.coverage-repair": 1}
    elif case_id == "coverage-repair-no-progress-exhausted":
        source["terminal_class"] = "stopped"
        source["terminal_reason_category"] = "coverage_no_progress"
        source["quality_metrics"]["coverage"] = {"outcome": "fail", "ratio": 0, "progress": False}
        source["semantic_counts"]["stops"] = {"healing.coverage-repair": 1}
    elif case_id == "healing-disallowed-business-stop":
        source["terminal_class"] = "stopped"
        source["terminal_reason_category"] = "healing_disallowed"
        source["issue_healing_decisions"] = [
            {
                "issue_class": "business",
                "decision": "stop",
                "evidence_digest": CHANGED_DIGEST,
            }
        ]
        source["semantic_counts"]["stops"] = {"healing": 1}
    elif case_id == "report-generation-required-outputs":
        files["result-tree/qa/coverage.json"] = '{"outcome":"pass","ratio":1}\n'
        files["result-tree/qa/summary.md"] = "# report\n"
        source["artifact_contract"].extend(
            [
                {
                    "artifact_id": "qa/coverage.json",
                    "media_type": "application/json",
                    "sha256": hashlib.sha256(b'{"outcome":"pass","ratio":1}\n').hexdigest(),
                    "semantic_projection": {"outcome": "pass"},
                },
                {
                    "artifact_id": "qa/summary.md",
                    "media_type": "text/markdown",
                    "sha256": hashlib.sha256(b"# report\n").hexdigest(),
                    "semantic_projection": {"section": "summary"},
                },
            ]
        )
    elif case_id == "issue-analysis-reconcile-path":
        source["issue_healing_decisions"] = [
            {
                "issue_class": "locator",
                "decision": "analyze",
                "evidence_digest": CHANGED_DIGEST,
            },
            {
                "issue_class": "locator",
                "decision": "reconcile",
                "evidence_digest": CHANGED_DIGEST,
            },
        ]
    elif case_id == "archive-durable-effect-replay":
        source["archive"] = {
            "present": True,
            "digest": CHANGED_DIGEST,
            "semantic_fields": {"replayed": True},
        }
        source["durable_effects"] = [
            {
                "effect_id": "archive.materialize",
                "idempotency_key": "archive-v1",
                "status": "applied",
                "receipt_digest": RECEIPT_DIGEST,
                "observed_external_projection": {"archive": "present"},
            }
        ]
    elif case_id == "retro-collect-analyze-propose-reconcile":
        source["retro"] = {
            "present": True,
            "digest": CHANGED_DIGEST,
            "semantic_fields": {
                "collect": True,
                "analyze": True,
                "propose": True,
                "reconcile": True,
            },
        }
    elif case_id == "improvement-review-evaluate-export-apply":
        source["improvement"] = {
            "present": True,
            "digest": CHANGED_DIGEST,
            "semantic_fields": {
                "review": True,
                "evaluate": True,
                "export": True,
                "apply": True,
            },
        }
    elif case_id == "improvement-rollback":
        source["improvement"] = {
            "present": True,
            "digest": CHANGED_DIGEST,
            "semantic_fields": {"rollback": True},
        }
    elif case_id == "human-interrupt-exact-resume":
        source["semantic_counts"]["interrupts"] = {"human": 1}
        source["diagnostics"] = [{"category": "interrupt", "message": "exact resume after human interrupt"}]
    elif case_id == "transient-local-retry":
        source["semantic_counts"]["retries"] = {"transient.local": 1}
        source["diagnostics"] = [{"category": "retry", "message": "transient local retry then pass"}]
    elif case_id == "opencode-ambiguous-create-recovery":
        source["diagnostics"] = [
            {"category": "recovery", "message": "ambiguous create recovered from provider evidence"}
        ]
    elif case_id == "cursor-unknown-process-indeterminate":
        source["terminal_class"] = "indeterminate"
        source["terminal_reason_category"] = "unknown_process"
        source["diagnostics"] = [{"category": "indeterminate", "message": "cursor unknown process ownership"}]
    elif case_id == "engine-crash-after-provider-terminal-receipt":
        source["durable_effects"] = [
            {
                "effect_id": "provider.terminal",
                "idempotency_key": "provider-receipt-v1",
                "status": "applied",
                "receipt_digest": RECEIPT_DIGEST,
                "observed_external_projection": {"recovered_after_crash": True},
            }
        ]
    elif case_id == "config-model-graph-source-drift-rejection":
        source["terminal_class"] = "failed"
        source["terminal_reason_category"] = "source_drift"
        source["diagnostics"] = [
            {"category": "drift", "message": "config/model/graph/source digest rejected"}
        ]
    elif case_id == "replay-after-provider-state-removal":
        source["diagnostics"] = [
            {"category": "replay", "message": "replayed from receipts after provider state removal"}
        ]
        source["durable_effects"] = [
            {
                "effect_id": "provider.replay",
                "idempotency_key": "replay-v1",
                "status": "applied",
                "receipt_digest": RECEIPT_DIGEST,
                "observed_external_projection": {"provider_state": "removed"},
            }
        ]
    return source, files


def write_export(root: Path, source: dict[str, Any], files: dict[str, str]) -> None:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    write_canonical_json(root / "manifest.json", source)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def write_side(side: str, destination: Path) -> None:
    runtime = LEGACY_RUNTIME if side == "legacy" else CURRENT_RUNTIME
    for case_id in CASE_IDS:
        source, files = case_export(case_id, runtime)
        write_export(destination / case_id, source, files)


def copy_immutable_exports(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def write_manifest() -> None:
    manifest = {
        "schema_version": "1",
        "cases": [
            {
                "id": case_id,
                "legacy_export": f"fixtures/comparison/legacy/{case_id}",
                "current_export": f"fixtures/comparison/current/{case_id}",
                "legacy_export_digest": digest_directory(LEGACY_ROOT / case_id),
                "current_export_digest": digest_directory(CURRENT_ROOT / case_id),
            }
            for case_id in CASE_IDS
        ],
    }
    write_canonical_json(MANIFEST_PATH, manifest)


def generate_from_subprocesses() -> None:
    scratch = HARNESS_ROOT / ".generated-exports"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir()
    try:
        legacy_out = scratch / "legacy"
        current_out = scratch / "current"
        subprocess.run(
            [sys.executable, str(Path(__file__)), "--side", "legacy", "--out", str(legacy_out)],
            check=True,
        )
        subprocess.run(
            [sys.executable, str(Path(__file__)), "--side", "current", "--out", str(current_out)],
            check=True,
        )
        copy_immutable_exports(legacy_out, LEGACY_ROOT)
        copy_immutable_exports(current_out, CURRENT_ROOT)
        write_manifest()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str]) -> int:
    if "--side" in argv:
        side = argv[argv.index("--side") + 1]
        destination = Path(argv[argv.index("--out") + 1])
        if side not in {"legacy", "current"}:
            raise SystemExit("side must be legacy or current")
        write_side(side, destination)
        return 0
    generate_from_subprocesses()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
