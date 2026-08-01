from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.assurance import LAYER_NAMES, LayerName
from assurance_agent.change_location import ChangeLocationError, ChangeNotFoundError
from assurance_agent.config import CONFIG_RELPATH
from assurance_agent.eval.change_location_evidence import (
    ChangeLocationEvidenceError,
    build_change_location_evidence,
    probe_live_candidates,
    replay_change_location_evidence,
)
from assurance_agent.eval.evidence_export import (
    EVIDENCE_EXPORT_DIR,
    BoundArtifactRefV1,
    EvidenceExportError,
    ExecutionEvidenceV1,
    export_root_execution_closure,
)
from assurance_agent.eval.selection import SELECTION_NORMALIZER_VERSION
from assurance_agent.eval.types import DatasetSample, ExecutionResult
from assurance_agent.eval.write_scan import (
    EVIDENCE_SUBDIR,
    WRITE_DIFF_JSON,
    WRITE_MANIFEST_AFTER,
    WRITE_MANIFEST_BEFORE,
    WRITE_POLICY_JSON,
    WRITE_POLICY_SCHEMA_VERSION,
    build_write_policy_v1,
    capture_write_scan_after,
    capture_write_scan_before,
    manifest_leaf_paths,
)
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
)
from assurance_agent.workflow.driver.runtime_factory import (
    RuntimeBundle,
    build_graph_runtime,
    runtime_context_for,
)
from assurance_agent.workflow.graph.agent_api import AgentInvoker, AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import parse_import_manifest
from assurance_agent.workflow.graph.runtime import GraphRuntimeError

RuntimeFactory = Callable[..., RuntimeBundle]  # tests may return structural stand-ins

_IN_PROCESS_TYPES = frozenset({"in_process", "score-only"})

_SAMPLE_INPUT_VAR = re.compile(r"\{\{\s*sample\.input\.([A-Za-z0-9_]+)\s*\}\}")


def _expand_sample_input_vars(value: str, sample: DatasetSample) -> str:
    """Expand ``{{sample.input.<key>}}`` tokens (suite executor run_mode templating)."""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in sample.input:
            raise AaError(f"sample {sample.id} missing input.{key} referenced by executor config")
        return str(sample.input[key])

    return _SAMPLE_INPUT_VAR.sub(repl, value)


def _copy_change_artifacts(change_dir: Path, raw_output: Path) -> None:
    raw_output.mkdir(parents=True, exist_ok=True)
    if not change_dir.is_dir():
        return
    for entry in change_dir.iterdir():
        target = raw_output / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def _copy_sut_tests(sut_dir: Path, raw_output: Path) -> None:
    """Mirror SUT ``tests/`` into raw-output so codegen scorers can see seeded code.

    Fixture seeding places generated tests at the SUT root (not under
    ``qa/changes/<id>/``). Without this copy, golden-sample replay would leave
    ``raw-output/tests`` empty and ``schema_valid_rate`` would hard-fail.
    """
    src = sut_dir / "tests"
    if not src.is_dir():
        return
    dest = raw_output / "tests"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _exit_for_status(status: str) -> int:
    if status == "completed":
        return EXIT_COMPLETED
    if status == "stopped":
        return EXIT_STOPPED
    if status == "interrupted":
        return EXIT_HUMAN_REVIEW
    return EXIT_ERROR


def _as_invoker(adapter: AgentInvoker | object) -> AgentInvoker:
    """Accept graph ``AgentInvoker`` or legacy ``run_phase``-only adapters."""
    if callable(getattr(adapter, "invoke", None)):
        return adapter  # type: ignore[return-value]

    run_phase = getattr(adapter, "run_phase", None)
    if not callable(run_phase):
        raise AaError("eval adapter must implement invoke() or run_phase()")

    class _Bridge:
        def invoke(self, request: AgentRequest) -> AgentResult:
            from assurance_agent.workflow.driver.adapter import PhaseRequest

            phase = PhaseRequest(
                change_id=request.change_id,
                phase_id=request.node_id,
                skill=request.target.removeprefix("skill:") if request.target.startswith("skill:") else None,
                agent=None,
                prompt=request.prompt,
            )
            result = run_phase(phase)
            ok = bool(getattr(result, "ok", False))
            error = getattr(result, "error", None)
            return AgentResult(ok=ok, error=str(error) if error else None)

    return _Bridge()


def execute_in_process(
    sample: DatasetSample,
    attempt_dir: Path,
    *,
    suite: str,
) -> ExecutionResult:
    """Score-only attempt: no workflow loop, no change_id required.

    Writes the evidence trio (stdout/stderr/execution.json) plus a tiny
    ``raw-output/`` marker so evidence_integrity and _test scorers pass.
    """
    attempt_dir.mkdir(parents=True, exist_ok=True)
    raw = attempt_dir / "raw-output"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / ".in-process").write_text(f"suite={suite}\nsample={sample.id}\n", encoding="utf-8")
    (attempt_dir / "stdout.log").write_text("in_process\n", encoding="utf-8")
    (attempt_dir / "stderr.log").write_text("", encoding="utf-8")
    execution = {
        "executor": f"in_process:{suite}",
        "sample_id": sample.id,
        "exit_code": 0,
        "reason": "in_process",
    }
    (attempt_dir / "execution.json").write_text(json.dumps(execution, indent=2), encoding="utf-8")
    return ExecutionResult(
        sample_id=sample.id,
        attempt=0,
        executor="in_process",
        status="ok",
        exit_code=0,
        error=None,
        extra={},
    )


def _require_selected_layers(selected_layers: tuple[LayerName, ...] | None) -> tuple[LayerName, ...]:
    if selected_layers is None:
        raise AaError(
            "execute_attempt requires selected_layers from normalize_selected_layers; "
            "raw test_type/test_types must not be passed downstream"
        )
    if not isinstance(selected_layers, tuple) or not selected_layers:
        raise AaError("selected_layers must be a non-empty canonical tuple")
    known = set(LAYER_NAMES)
    if any(layer not in known for layer in selected_layers):
        raise AaError(f"selected_layers contains unknown layer(s): {selected_layers!r}")
    if tuple(layer for layer in LAYER_NAMES if layer in selected_layers) != selected_layers:
        raise AaError(f"selected_layers must be in canonical order: {selected_layers!r}")
    if len(set(selected_layers)) != len(selected_layers):
        raise AaError(f"selected_layers must not contain duplicates: {selected_layers!r}")
    return selected_layers


def execute_attempt(
    sample: DatasetSample,
    attempt_dir: Path,
    *,
    suite: str,
    sut_dir: Path,
    adapter: AgentInvoker | object,
    entrypoint: str = "full",
    runtime_factory: Callable[..., object] | None = None,
    expected_outputs: list[str] | None = None,
    fixtures_root: Path | None = None,
    executor_type: str = "workflow-run",
    run_mode: str | None = None,
    selected_layers: tuple[LayerName, ...] | None = None,
    run_tests: bool | None = None,
    **rejected: object,
) -> ExecutionResult:
    if rejected:
        unexpected = ", ".join(sorted(rejected))
        raise AaError(
            f"execute_attempt rejected unresolved selection kwargs: {unexpected}; "
            "pass selected_layers from normalize_selected_layers"
        )
    if executor_type in _IN_PROCESS_TYPES:
        return execute_in_process(sample, attempt_dir, suite=suite)

    change_id = sample.input.get("change_id")
    if not change_id:
        raise AaError(f"sample {sample.id} missing input.change_id")
    assert_change_id_safe(change_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)

    resolved_run_mode = _expand_sample_input_vars(run_mode, sample) if run_mode else "full"
    resolved_layers = _require_selected_layers(selected_layers)
    resolved_test_types = list(resolved_layers)
    resolved_run_tests = True if run_tests is None else bool(run_tests)

    fixture_tier = sample.input.get("fixture_tier")
    import_manifest_path: Path | None = None
    if fixture_tier:
        if fixtures_root is None:
            raise AaError(f"sample {sample.id} has fixture_tier but fixtures_root was not provided")
        fixture_id = sample.input.get("fixture_id")
        if not fixture_id:
            raise AaError(f"sample {sample.id} has fixture_tier but no explicit fixture_id")
        from assurance_agent.eval.fixtures import seed_change

        seeded = seed_change(
            sut_sandbox=sut_dir,
            change_id=str(change_id),
            tier_name=str(fixture_tier),
            fixtures_root=fixtures_root,
            fixture_id=str(fixture_id),
            entrypoint=entrypoint,
        )
        import_manifest_path = seeded.import_manifest_path

    evidence_dir = attempt_dir / EVIDENCE_SUBDIR
    evidence_dir.mkdir(parents=True, exist_ok=True)

    def _infra_fail(message: str) -> ExecutionResult:
        (attempt_dir / "stdout.log").write_text("", encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(message + "\n", encoding="utf-8")
        envelope = ExecutionEvidenceV1(
            schema_version="1",
            selected_layers=list(resolved_layers),
            selection_normalizer_version=SELECTION_NORMALIZER_VERSION,
            write_policy_schema_version=WRITE_POLICY_SCHEMA_VERSION,
            change_id=str(change_id),
            change_repo_path=None,
            root_invocation_id=None,
            exit_code=1,
            reason=message,
            run_mode=resolved_run_mode,
            infrastructure_error=True,
        )
        (attempt_dir / "execution.json").write_bytes(canonical_json_bytes(envelope))
        return ExecutionResult(
            sample_id=sample.id,
            attempt=0,
            executor="workflow-run",
            status="error",
            exit_code=1,
            error=message,
            selected_layers=resolved_layers,
            selection_normalizer_version=SELECTION_NORMALIZER_VERSION,
            extra={"infrastructure_error": True},
        )

    # Capture before-manifest independently after fixture seeding and before runtime.
    try:
        # Temporary policy placeholder so capture_write_scan_before persists a file;
        # replaced below once D17 resolves the active change path.
        from assurance_agent.eval.write_scan import WritePolicyV1

        placeholder = WritePolicyV1(
            schema_version="1",
            write_policy_schema_version="write_policy/v1",
            mode="denylist",
            patterns=[".aa/memory/**"],
            selected_layers=list(resolved_layers),
            change_repo_path=None,
        )
        before_manifest = capture_write_scan_before(attempt_dir, sut_dir, placeholder)
    except AaError as exc:
        return _infra_fail(str(exc))

    # D17: copy exact config, build/replay change-location evidence from before leaves.
    config_path = sut_dir / CONFIG_RELPATH
    change_repo_path: str | None = None
    policy = None
    try:
        if not config_path.is_file():
            raise ChangeLocationEvidenceError(f"{CONFIG_RELPATH} missing")
        config_bytes = config_path.read_bytes()
        (evidence_dir / "change-location-config.yaml").write_bytes(config_bytes)
        _roots, probes = probe_live_candidates(sut_dir, str(change_id), config_bytes)
        del _roots
        leaves = manifest_leaf_paths(before_manifest)
        location = build_change_location_evidence(
            change_id=str(change_id),
            config_bytes=config_bytes,
            probes=probes,
            before_manifest_leaves=leaves,
        )
        replay_change_location_evidence(
            evidence=location,
            config_bytes=config_bytes,
            before_manifest_leaves=leaves,
        )
        (evidence_dir / "change-location.json").write_bytes(canonical_json_bytes(location))
        change_repo_path = location.resolved_change_repo_path
        policy = build_write_policy_v1(
            run_mode=resolved_run_mode,
            selected_layers=resolved_layers,
            change_repo_path=change_repo_path,
        )
        (evidence_dir / WRITE_POLICY_JSON).write_bytes(canonical_json_bytes(policy))
    except (
        ChangeLocationEvidenceError,
        ChangeLocationError,
        ChangeNotFoundError,
        AaError,
        ValueError,
    ) as exc:
        # Persist infrastructure evidence but no writable policy/root.
        policy = None
        change_repo_path = None
        location_error = str(exc)
    else:
        location_error = None

    if policy is None:
        # Still capture after-manifest for forensics; no runtime credit.
        try:
            from assurance_agent.eval.write_scan import WritePolicy

            capture_write_scan_after(
                attempt_dir,
                sut_dir,
                WritePolicy(mode="denylist", patterns=()),
                before_manifest,
            )
        except AaError:
            pass
        return _infra_fail(location_error or "change location / write policy unavailable")

    params: dict[str, object] = {
        "run_mode": resolved_run_mode,
        "test_types": resolved_test_types,
        "run_tests": resolved_run_tests,
    }
    invoker = _as_invoker(adapter)
    factory = runtime_factory or build_graph_runtime
    reason = "ok"
    exit_code = EXIT_COMPLETED
    root_invocation_id: str | None = None
    try:
        bundle = factory(project_root=sut_dir, change_id=str(change_id), adapter=invoker)
        context = runtime_context_for(sut_dir, str(change_id), params)
        runtime = bundle.runtime  # type: ignore[attr-defined]
        compiled = bundle.compiled  # type: ignore[attr-defined]
        if import_manifest_path is not None and import_manifest_path.is_file():
            manifest = parse_import_manifest(import_manifest_path.read_text(encoding="utf-8"))
            imported = runtime.import_checkpoint(compiled, manifest, context)
            root_invocation_id = imported.invocation_id
            status_view = runtime.status(imported.invocation_id)
            exit_code = _exit_for_status(status_view.status)
            reason = status_view.terminal_reason or status_view.status
        else:
            result = runtime.run(compiled, entrypoint, context)
            root_invocation_id = getattr(result, "invocation_id", None)
            exit_code = result.exit_code
            reason = result.reason
    except (GraphRuntimeError, AaError, ValueError, OSError) as exc:
        exit_code = EXIT_ERROR
        reason = str(exc)
        root_invocation_id = None

    post_error: str | None = None
    try:
        capture_write_scan_after(attempt_dir, sut_dir, policy, before_manifest)
    except AaError as exc:
        post_error = str(exc)

    # Prefer D17-resolved path for artifact copy; fall back to configured default.
    change_dir = sut_dir / (change_repo_path or f"qa/changes/{change_id}")
    raw_output = attempt_dir / "raw-output"
    _copy_change_artifacts(change_dir, raw_output)
    _copy_sut_tests(sut_dir, raw_output)

    export_slice_ref: BoundArtifactRefV1 | None = None
    export_manifest_ref: BoundArtifactRefV1 | None = None
    source_ledger_sha256: str | None = None
    source_ledger_size: int | None = None
    source_event_count: int | None = None
    source_seq_pairs: list[tuple[int, str]] = []
    if root_invocation_id is not None and change_dir.is_dir():
        try:
            export_dir = evidence_dir / EVIDENCE_EXPORT_DIR
            _slice, _manifest, export_slice_ref, export_manifest_ref, source_seq_pairs = (
                export_root_execution_closure(
                    change_dir=change_dir,
                    root_invocation_id=root_invocation_id,
                    selected_layers=resolved_layers,
                    export_dir=export_dir,
                )
            )
            source_ledger_sha256 = _slice.source_ledger_sha256
            source_ledger_size = _slice.source_ledger_size
            source_event_count = _slice.source_event_count
        except EvidenceExportError:
            # Keep root id; missing/invalid export refs fail evidence integrity at score time.
            export_slice_ref = None
            export_manifest_ref = None
            source_ledger_sha256 = None
            source_ledger_size = None
            source_event_count = None
            source_seq_pairs = []
        except Exception:  # noqa: BLE001 — export must not crash the attempt envelope
            export_slice_ref = None
            export_manifest_ref = None
            source_ledger_sha256 = None
            source_ledger_size = None
            source_event_count = None
            source_seq_pairs = []

    def _bind(name: str) -> BoundArtifactRefV1 | None:
        path = evidence_dir / name
        if not path.is_file():
            return None
        data = path.read_bytes()
        return BoundArtifactRefV1(
            relative_path=f"{EVIDENCE_SUBDIR}/{name}",
            sha256=sha256_bytes(data),
            size=len(data),
        )

    runtime_params = {
        "run_mode": resolved_run_mode,
        "test_types": resolved_test_types,
        "run_tests": resolved_run_tests,
    }
    status = "ok" if exit_code == 0 else "error"
    if post_error:
        status = "error"
    envelope = ExecutionEvidenceV1(
        schema_version="1",
        selected_layers=list(resolved_layers),
        selection_normalizer_version=SELECTION_NORMALIZER_VERSION,
        write_policy_schema_version=WRITE_POLICY_SCHEMA_VERSION,
        change_id=str(change_id),
        change_repo_path=change_repo_path,
        root_invocation_id=root_invocation_id,
        exit_code=exit_code,
        reason=reason if not post_error else f"{reason}; {post_error}",
        run_mode=resolved_run_mode,
        runtime_params=runtime_params,
        infrastructure_error=bool(post_error),
        change_location_config=_bind("change-location-config.yaml"),
        change_location=_bind("change-location.json"),
        write_manifest_before=_bind(WRITE_MANIFEST_BEFORE),
        write_manifest_after=_bind(WRITE_MANIFEST_AFTER),
        write_diff=_bind(WRITE_DIFF_JSON),
        write_policy=_bind(WRITE_POLICY_JSON),
        source_ledger_sha256=source_ledger_sha256,
        source_ledger_size=source_ledger_size,
        source_event_count=source_event_count,
        root_slice=export_slice_ref,
        export_manifest=export_manifest_ref,
        source_seq_pairs=[[seq, digest] for seq, digest in source_seq_pairs],
    )

    (attempt_dir / "stdout.log").write_text(reason + "\n", encoding="utf-8")
    (attempt_dir / "stderr.log").write_text((post_error or "") + ("\n" if post_error else ""), encoding="utf-8")
    (attempt_dir / "execution.json").write_bytes(canonical_json_bytes(envelope))

    missing = [rel for rel in (expected_outputs or []) if not (raw_output / rel).exists()]
    if missing or post_error:
        status = "error"

    return ExecutionResult(
        sample_id=sample.id,
        attempt=0,
        executor="workflow-run",
        status=status,
        exit_code=exit_code,
        error=None if status == "ok" else (post_error or reason or f"missing outputs: {missing}"),
        selected_layers=resolved_layers,
        selection_normalizer_version=SELECTION_NORMALIZER_VERSION,
        extra={
            "missing_outputs": missing,
            "runtime_params": runtime_params,
            "root_invocation_id": root_invocation_id,
            "change_repo_path": change_repo_path,
        },
    )
