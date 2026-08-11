# tests/integration/test_eval_cli.py
from __future__ import annotations

import itertools
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.assurance import LAYER_NAMES, LayerName
from assurance_agent.cli import main
from assurance_agent.eval import write_scan
from assurance_agent.eval.types import CODEGEN_HARD_METRICS, CODEGEN_SUITE_PENDING_TIERS
from assurance_agent.verification.generated_files import get_generated_files_contract
from tests.helpers_aa import write_aa_config
from tests.helpers_four_layer_runtime import FourLayerDeterministicAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES_SRC = REPO_ROOT / "benchmark" / "vue-fastapi-admin" / "eval-fixtures"

_CODEGEN_CLI_CASES = (
    ("workflow-api-codegen", "WAC-001", "api"),
    ("workflow-e2e-codegen", "WEEC-001", "e2e"),
    ("workflow-fuzz-codegen", "WFUZ-001", "fuzz"),
    ("workflow-performance-codegen", "WPER-001", "performance"),
)


def _seed(project_root: Path) -> None:
    suites = project_root / "eval" / "suites"
    suites.mkdir(parents=True)
    (suites / "workflow-case.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "workflow-case",
                "scorer": "workflow-case",
                "executor": {"type": "workflow-run", "entrypoint": "case"},
                "thresholds": [
                    {"metric": "case_review_gate_pass_rate", "gate": "hard", "op": "gte", "value": 0.99},
                ],
            }
        ),
        encoding="utf-8",
    )
    ds = project_root / "eval" / "datasets" / "workflow-case"
    ds.mkdir(parents=True)
    (ds / "WC-001.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "WC-001",
                "suite": "workflow-case",
                "input": {"change_id": "eval-sample-001"},
                "expected": {},
            }
        ),
        encoding="utf-8",
    )
    sut = project_root / "sut"
    write_aa_config(sut)
    change = sut / "qa" / "changes" / "eval-sample-001" / "review"
    change.mkdir(parents=True)
    (change / "case-review.json").write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
    # Fresh throwaway repo in the isolated filesystem — write-scan requires the
    # SUT workspace to be a git repo (same precondition as the TS executor).
    subprocess.run(["git", "init"], cwd=sut, check=True, capture_output=True)


def _stub_execute_attempt(monkeypatch):
    from assurance_agent.eval import runner as runner_mod
    from assurance_agent.eval.types import ExecutionResult

    def fake_execute(sample, attempt_dir, **kwargs):
        attempt_dir.mkdir(parents=True, exist_ok=True)
        raw = attempt_dir / "raw-output"
        raw.mkdir(parents=True, exist_ok=True)
        review = raw / "review"
        review.mkdir(parents=True, exist_ok=True)
        (review / "case-review.json").write_text('{"decision":"pass"}', encoding="utf-8")
        (attempt_dir / "stdout.log").write_text("ok\n", encoding="utf-8")
        (attempt_dir / "stderr.log").write_text("", encoding="utf-8")
        (attempt_dir / "execution.json").write_text('{"exit_code":0}', encoding="utf-8")
        return ExecutionResult(
            sample_id=sample.id, attempt=0, executor="workflow-run", status="ok", exit_code=0
        )

    monkeypatch.setattr(runner_mod, "execute_attempt", fake_execute)


def test_eval_run_json_shape(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")
        _stub_execute_attempt(monkeypatch)
        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-case",
                "--sut-dir",
                str(project_root / "sut"),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["verdict"] == "pass"
        assert payload["run_id"].startswith("eval-")


def test_eval_run_output_id_prints_only_run_id(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")
        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-case",
                "--sut-dir",
                str(project_root / "sut"),
                "--output",
                "id",
            ],
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip().startswith("eval-")


def test_eval_run_requires_suite_or_plan() -> None:
    result = CliRunner().invoke(main, ["eval", "run"])
    assert result.exit_code == 1
    assert "--suite" in result.output


def test_eval_run_suite_and_plan_mutually_exclusive() -> None:
    result = CliRunner().invoke(main, ["eval", "run", "--suite", "x", "--plan", "p.json"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_eval_run_passes_resolved_extra_memory_dir(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        overlay = project_root / "overlay"
        (overlay / ".aa" / "memory").mkdir(parents=True)
        seen: dict = {}

        def fake_run_suite(**kwargs):  # noqa: ANN003, ANN202
            seen.update(kwargs)
            return "eval-1", SimpleNamespace(verdict="pass")

        monkeypatch.setattr("assurance_agent.commands.eval_cmd.run_suite", fake_run_suite)
        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-case",
                "--sut-dir",
                str(project_root / "sut"),
                "--extra-memory-dir",
                str(overlay),
            ],
        )

        assert result.exit_code == 0, result.output
        assert seen["extra_memory_dir"] == overlay.resolve()


def test_eval_plan_writes_json() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main,
            [
                "eval",
                "plan",
                "--event",
                "manual",
                "--suite",
                "workflow-case",
                "--out",
                "eval-plan.json",
            ],
        )
        assert result.exit_code == 0, result.output
        plan = json.loads(Path("eval-plan.json").read_text())
        assert plan["suites"] == ["workflow-case"]


def test_eval_report_json(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")
        _stub_execute_attempt(monkeypatch)
        run = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-case",
                "--change",
                "RET-user-1",
                "--change",
                "RET-role-1",
                "--sut-dir",
                str(project_root / "sut"),
                "--output",
                "id",
            ],
        )
        run_id = run.output.strip()
        result = runner.invoke(
            main,
            [
                "eval",
                "report",
                "--run",
                run_id,
                "--json",
                "--sut-dir",
                str(project_root / "sut"),
            ],
        )
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["run_id"] == run_id
        assert report["verdict"] == "pass"
        assert report["source_change_ids"] == ["RET-user-1", "RET-role-1"]


def test_eval_run_list_form_selection_single_resolution(monkeypatch) -> None:
    """Real aa eval run with YAML list test_types converges on one canonical tuple."""
    from assurance_agent.eval.selection import SELECTION_NORMALIZER_VERSION

    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        suites = project_root / "eval" / "suites"
        suites.mkdir(parents=True)
        (suites / "workflow-case.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "workflow-case",
                    "scorer": "workflow-case",
                    "executor": {
                        "type": "workflow-run",
                        "entrypoint": "case",
                        "run_mode": "case-only",
                        "test_types": ["performance", "api", "e2e"],
                        "run_tests": False,
                    },
                    "thresholds": [
                        {
                            "metric": "case_review_gate_pass_rate",
                            "gate": "hard",
                            "op": "gte",
                            "value": 0.99,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        ds = project_root / "eval" / "datasets" / "workflow-case"
        ds.mkdir(parents=True)
        (ds / "WC-001.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "WC-001",
                    "suite": "workflow-case",
                    "input": {"change_id": "eval-sample-001"},
                    "expected": {},
                }
            ),
            encoding="utf-8",
        )
        sut = project_root / "sut"
        write_aa_config(sut)
        change = sut / "qa" / "changes" / "eval-sample-001" / "review"
        change.mkdir(parents=True)
        (change / "case-review.json").write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
        subprocess.run(["git", "init"], cwd=sut, check=True, capture_output=True)

        seen: dict = {}

        def fake_execute(sample, attempt_dir, **kwargs):  # noqa: ANN001, ANN003, ANN202
            from assurance_agent.eval.types import ExecutionResult

            seen["selected_layers"] = kwargs.get("selected_layers")
            attempt_dir.mkdir(parents=True, exist_ok=True)
            raw = attempt_dir / "raw-output"
            raw.mkdir(parents=True, exist_ok=True)
            review = raw / "review"
            review.mkdir(parents=True, exist_ok=True)
            (review / "case-review.json").write_text('{"decision":"pass"}', encoding="utf-8")
            (attempt_dir / "stdout.log").write_text("ok\n", encoding="utf-8")
            (attempt_dir / "stderr.log").write_text("", encoding="utf-8")
            layers = list(kwargs["selected_layers"])
            (attempt_dir / "execution.json").write_text(
                json.dumps(
                    {
                        "exit_code": 0,
                        "selected_layers": layers,
                        "selection_normalizer_version": SELECTION_NORMALIZER_VERSION,
                        "runtime_params": {"test_types": layers},
                    }
                ),
                encoding="utf-8",
            )
            return ExecutionResult(
                sample_id=sample.id,
                attempt=0,
                executor="workflow-run",
                status="ok",
                exit_code=0,
                selected_layers=tuple(layers),
                selection_normalizer_version=SELECTION_NORMALIZER_VERSION,
            )

        from assurance_agent.eval import runner as runner_mod

        monkeypatch.setattr(runner_mod, "execute_attempt", fake_execute)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")
        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-case",
                "--sut-dir",
                str(sut),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert seen["selected_layers"] == ("api", "e2e", "performance")
        execution = next(sut.joinpath("eval", "out", "runs").rglob("execution.json"))
        payload = json.loads(execution.read_text(encoding="utf-8"))
        assert payload["selected_layers"] == ["api", "e2e", "performance"]
        assert payload["runtime_params"]["test_types"] == ["api", "e2e", "performance"]
        assert payload["selection_normalizer_version"] == SELECTION_NORMALIZER_VERSION


def _copy_codegen_suite(project_root: Path, suite_name: str) -> None:
    suite_src = REPO_ROOT / "eval" / "suites" / f"{suite_name}.yaml"
    ds_src = REPO_ROOT / "eval" / "datasets" / suite_name
    suites = project_root / "eval" / "suites"
    suites.mkdir(parents=True, exist_ok=True)
    shutil.copy2(suite_src, suites / f"{suite_name}.yaml")
    shutil.copytree(ds_src, project_root / "eval" / "datasets" / suite_name, dirs_exist_ok=True)


def _prepare_codegen_sut(project_root: Path) -> Path:
    sut = project_root / "sut"
    sut.mkdir(parents=True, exist_ok=True)
    write_aa_config(sut)
    subprocess.run(["git", "init"], cwd=sut, check=True, capture_output=True)
    shutil.copytree(_FIXTURES_SRC, sut / "eval-fixtures")
    return sut


def _install_four_layer_adapter(monkeypatch) -> None:
    from assurance_agent.commands import eval_cmd

    def factory(**_: object) -> FourLayerDeterministicAdapter:
        return FourLayerDeterministicAdapter()

    monkeypatch.setattr(eval_cmd, "_resolve_adapter_factory", lambda **_: factory)


def _attempt_execution(sut: Path) -> dict:
    execution = next(sut.joinpath("eval", "out", "runs").rglob("execution.json"))
    return json.loads(execution.read_text(encoding="utf-8"))


@pytest.mark.parametrize("suite_name,sample_id,layer", _CODEGEN_CLI_CASES)
def test_eval_run_codegen_pending_import_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    suite_name: str,
    sample_id: str,
    layer: str,
) -> None:
    """Real aa eval run + FourLayerDeterministicAdapter against pending import checkpoint."""
    from assurance_agent.eval.selection import SELECTION_NORMALIZER_VERSION

    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _copy_codegen_suite(project_root, suite_name)
        sut = _prepare_codegen_sut(project_root)
        _install_four_layer_adapter(monkeypatch)

        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                suite_name,
                "--sample",
                sample_id,
                "--sut-dir",
                str(sut),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["verdict"] == "pass"

        execution = _attempt_execution(sut)
        assert execution["selected_layers"] == [layer]
        assert execution["runtime_params"]["test_types"] == [layer]
        assert execution["selection_normalizer_version"] == SELECTION_NORMALIZER_VERSION
        assert execution["write_policy_schema_version"] == write_scan.WRITE_POLICY_SCHEMA_VERSION
        assert execution["root_invocation_id"]
        assert execution.get("export_manifest")
        assert execution.get("root_slice")
        assert execution.get("write_policy")
        assert execution.get("change_location")

        assert execution["root_invocation_id"]
        assert CODEGEN_SUITE_PENDING_TIERS[suite_name].endswith("-pending")
        assert execution.get("change_repo_path") == "qa/changes/eval-sample-001"

        run_dir = next((sut / "eval" / "out" / "runs").iterdir())
        suite_metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        for name in CODEGEN_HARD_METRICS:
            assert suite_metrics["metrics"][name] == 1.0
        assert suite_metrics["metrics"]["evidence_integrity"] == 1.0
        gate = json.loads((run_dir / "gate-result.json").read_text(encoding="utf-8"))
        assert gate["verdict"] == "pass"


def test_eval_run_codegen_fresh_root_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-tier/no-import control: fresh execute root + D17 envelope; cannot hard-pass codegen gates."""
    from assurance_agent.eval.selection import SELECTION_NORMALIZER_VERSION
    from tests.helpers_four_layer_runtime import CHANGE_ID, seed_four_layer_project

    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        suites = project_root / "eval" / "suites"
        suites.mkdir(parents=True)
        suite_body = yaml.safe_load(
            (REPO_ROOT / "eval" / "suites" / "workflow-api-codegen.yaml").read_text(encoding="utf-8")
        )
        (suites / "workflow-api-codegen.yaml").write_text(
            yaml.safe_dump(suite_body),
            encoding="utf-8",
        )
        ds = project_root / "eval" / "datasets" / "workflow-api-codegen"
        ds.mkdir(parents=True)
        (ds / "WAC-FRESH.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "WAC-FRESH",
                    "suite": "workflow-api-codegen",
                    # No fixture_tier — executor will not import_checkpoint.
                    "input": {"change_id": CHANGE_ID},
                    "expected": {},
                }
            ),
            encoding="utf-8",
        )
        sut, change_dir, import_path = seed_four_layer_project(
            project_root,
            selected_layers=("api",),
            applicable_layers=("api",),
        )
        # Remove helper import so this is a true fresh-root execute (no-import).
        import_path.unlink()
        assert not (change_dir / ".graph-runtime" / "import-manifest.yaml").exists()
        assert "fixture_tier" not in yaml.safe_load((ds / "WAC-FRESH.yaml").read_text())["input"]

        _install_four_layer_adapter(monkeypatch)
        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-api-codegen",
                "--sample",
                "WAC-FRESH",
                "--sut-dir",
                str(sut),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        # Fresh execute stops at registry without an import checkpoint — not a pass.
        assert payload["verdict"] in {"fail", "inconclusive"}
        execution = _attempt_execution(sut)
        assert execution["selected_layers"] == ["api"]
        assert execution["selection_normalizer_version"] == SELECTION_NORMALIZER_VERSION
        assert execution["write_policy_schema_version"] == write_scan.WRITE_POLICY_SCHEMA_VERSION
        assert execution["root_invocation_id"]  # fresh root id still assigned
        assert execution.get("write_policy")
        assert execution.get("change_location")
        assert execution.get("export_manifest")
        assert execution.get("root_slice")
        assert execution.get("change_repo_path") == f"qa/changes/{CHANGE_ID}"
        run_dir = next((sut / "eval" / "out" / "runs").iterdir())
        suite_metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        # Error attempt yields empty aggregated metrics (or zeros) — not a hard-pass.
        metrics = suite_metrics.get("metrics") or {}
        for name in CODEGEN_HARD_METRICS:
            assert metrics.get(name, 0.0) == 0.0


def test_eval_codegen_fifteen_selection_policy_table() -> None:
    """Re-run the fifteen selection-subset WritePolicyV1 / claim parity table."""
    subsets: list[tuple[LayerName, ...]] = []
    for width in range(1, 5):
        subsets.extend(itertools.combinations(LAYER_NAMES, width))
    assert len(subsets) == 15
    for subset in subsets:
        policy = write_scan.build_write_policy_v1(
            run_mode="codegen-only",
            selected_layers=subset,
            change_repo_path="qa/changes/eval-sample-001",
        )
        claims = write_scan.selected_layer_contract_write_claims(subset)
        for layer in subset:
            root = get_generated_files_contract(layer).private_test_root
            assert root in policy.patterns
            assert f"{root}/**" in claims
        for layer in LAYER_NAMES:
            if layer not in subset:
                root = get_generated_files_contract(layer).private_test_root
                assert root not in policy.patterns
        again = write_scan.build_write_policy_v1(
            run_mode="codegen-only",
            selected_layers=subset,
            change_repo_path="qa/changes/eval-sample-001",
        )
        assert canonical_json_bytes(policy) == canonical_json_bytes(again)


def test_eval_run_codegen_tampered_policy_zeros_hard_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tampered WritePolicyV1 fails policy replay → hard metrics stay 0 → fail verdict."""
    from assurance_agent.eval import runner as runner_mod
    from assurance_agent.eval.types import ExecutionResult

    real_execute = runner_mod.execute_attempt

    def tampering_execute(sample, attempt_dir, **kwargs):  # noqa: ANN001, ANN003, ANN202
        result = real_execute(sample, attempt_dir, **kwargs)
        policy_path = attempt_dir / "evidence" / "write-policy.json"
        if policy_path.is_file():
            raw = json.loads(policy_path.read_text(encoding="utf-8"))
            patterns = list(raw.get("patterns") or [])
            patterns.append("tampered-sibling/**")
            raw["patterns"] = patterns
            policy_path.write_text(json.dumps(raw), encoding="utf-8")
        return result if isinstance(result, ExecutionResult) else result

    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _copy_codegen_suite(project_root, "workflow-api-codegen")
        sut = _prepare_codegen_sut(project_root)
        _install_four_layer_adapter(monkeypatch)
        monkeypatch.setattr(runner_mod, "execute_attempt", tampering_execute)
        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-api-codegen",
                "--sample",
                "WAC-001",
                "--sut-dir",
                str(sut),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["verdict"] == "fail"
        run_dir = next((sut / "eval" / "out" / "runs").iterdir())
        suite_metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        for name in CODEGEN_HARD_METRICS:
            assert suite_metrics["metrics"][name] == 0.0
