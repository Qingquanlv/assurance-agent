from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assurance_execution.contracts.telemetry import (
    TelemetryCompletionV1,
    check_trace_requirements,
    parse_otlp_records,
)
from assurance_execution.contracts.verification import VerificationManifestV1
from assurance_execution.operations.telemetry import (
    load_otlp_records,
    seal_telemetry_artifacts,
)
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
from graph_engine.attempts import AttemptKey, BusinessActivation
from tests.verified_generation_fixture import accepted_verified_execution_input

EXECUTION_ID = "12345678-1234-4123-8123-123456789abc"
OTHER_EXECUTION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TRACE_ID = "a" * 32
DRIVER = "b" * 16
SERVER = "c" * 16
WRITE = "d" * 16
DONE = "e" * 16
HELPER = "f" * 16
ORACLE = "1" * 16

_KIND = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3}
_STATUS = {"UNSET": 0, "OK": 1, "ERROR": 2}


def _attr(key: str, value: object) -> dict[str, object]:
    if isinstance(value, bool):
        typed = {"boolValue": value}
    elif isinstance(value, int) and not isinstance(value, bool):
        typed = {"intValue": str(value)}
    else:
        typed = {"stringValue": str(value)}
    return {"key": key, "value": typed}


def _span(
    *,
    span_id: str,
    name: str,
    kind: str,
    parent: str = "",
    status: str = "UNSET",
    attrs: dict[str, object] | None = None,
    trace_id: str = TRACE_ID,
) -> dict[str, object]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "kind": _KIND[kind],
        "status": {"code": _STATUS[status]},
        "attributes": [_attr(key, value) for key, value in (attrs or {}).items()],
    }


def _record(
    groups: list[tuple[str, list[dict[str, object]]]],
    *,
    service: str = "user-oracle-sut",
    instance: str = "sut",
) -> dict[str, object]:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attr("service.name", service),
                        _attr("service.instance.id", instance),
                    ]
                },
                "scopeSpans": [{"scope": {"name": scope}, "spans": spans} for scope, spans in groups],
            }
        ]
    }


def _identity(execution_id: str = EXECUTION_ID) -> dict[str, object]:
    return {"aa.execution_id": execution_id}


def correlated_otlp(
    *,
    execution_id: str = EXECUTION_ID,
    instance: str = "sut",
    helper: bool = False,
    write: bool = True,
    http: bool = True,
    completed: bool = True,
    write_table: str = "user",
    write_op: str = "INSERT",
    username: str = "oracle_user",
) -> bytes:
    identity = _identity(execution_id)
    groups: list[tuple[str, list[dict[str, object]]]] = []
    driver = [
        _span(
            span_id=DRIVER,
            name="POST /api/v1/user/create",
            kind="CLIENT",
            attrs={**identity, "http.method": "POST", "http.url": "http://127.0.0.1:1234/api/v1/user/create"},
        )
    ]
    groups.append(("assurance.execution.http-driver", driver))
    if http:
        groups.append(
            (
                "opentelemetry.instrumentation.fastapi",
                [
                    _span(
                        span_id=SERVER,
                        parent=DRIVER,
                        name="POST /api/v1/user/create",
                        kind="SERVER",
                        attrs={**identity, "http.route": "/api/v1/user/create"},
                    )
                ],
            )
        )
    parent = SERVER if http else DRIVER
    if helper:
        groups.append(
            (
                "app.users.helpers",
                [
                    _span(
                        span_id=HELPER,
                        parent=parent,
                        name="persist_user",
                        kind="INTERNAL",
                        attrs=identity,
                    )
                ],
            )
        )
        parent = HELPER
    if write:
        groups.append(
            (
                "opentelemetry.instrumentation.tortoiseorm",
                [
                    _span(
                        span_id=WRITE,
                        parent=parent,
                        name=f"{write_table} {write_op}",
                        kind="CLIENT",
                        attrs={
                            **identity,
                            "aa.db.table": write_table,
                            "aa.db.operation": write_op,
                            "aa.db.semconv": "1.24.0",
                        },
                    )
                ],
            )
        )
    if completed:
        groups.append(
            (
                "app.api.v1.users.users",
                [
                    _span(
                        span_id=DONE,
                        parent=SERVER if http else parent,
                        name="user.create.completed",
                        kind="INTERNAL",
                        attrs={**identity, "user.username": username},
                    )
                ],
            )
        )
    return (json.dumps(_record(groups, instance=instance)) + "\n").encode()


def oracle_select_otlp(*, execution_id: str = EXECUTION_ID) -> bytes:
    return (
        json.dumps(
            _record(
                [
                    (
                        "assurance.execution.oracle",
                        [
                            _span(
                                span_id=ORACLE,
                                name="oracle.user.select",
                                kind="CLIENT",
                                attrs={
                                    **_identity(execution_id),
                                    "aa.role": "oracle",
                                    "aa.db.table": "user",
                                    "aa.db.operation": "SELECT",
                                },
                            )
                        ],
                    )
                ],
                service="oracle",
            )
        )
        + "\n"
    ).encode()


def complete_telemetry(*, digest: str, size: int, drain: str = "complete", reason: str | None = None) -> dict:
    failed = drain != "complete"
    return {
        "schema_version": "1",
        "execution_id": EXECUTION_ID,
        "sut_instance_id": "sut",
        "driver_flush": {"state": "complete"},
        "sut_flush": {"state": "complete"},
        "collector_drain": {
            "state": drain,
            "reason": reason if failed else None,
        },
        "archive": {
            "state": "complete" if not failed else "incomplete",
            "reason": reason if failed else None,
            "path": "telemetry.otlp.jsonl",
            "digest": digest,
            "size": size,
        },
        "state": "incomplete" if failed else "complete",
    }


def _trace_plan(root: Path):
    prepared = accepted_verified_execution_input(root, validation_profile="api_db_trace.v1")
    ref = prepared.verification.case_execution_plan_ref  # type: ignore[union-attr]
    plan = CaseExecutionPlanSetV1.model_validate_json((root / ref.path).read_bytes()).cases[0]
    return plan, prepared


def _manifest(root: Path, plan, prepared) -> VerificationManifestV1:
    sqlite = root / "verified.sqlite3"
    if not sqlite.exists():
        sqlite.write_bytes(b"sqlite")
    stat = sqlite.stat()
    generation = prepared.generation_result
    assert generation is not None
    return VerificationManifestV1.model_validate(
        {
            "execution_id": EXECUTION_ID,
            "change_id": plan.change_id,
            "case_id": plan.case_id,
            "nodeid": "tests/api/test_user_create.py::test_tc_user_create_001__create",
            "invocation_id": "invocation",
            "task_id": "task",
            "graph_instance_id": "graph",
            "attempt_key": AttemptKey(digest="4" * 64),
            "business_activation": BusinessActivation.for_trigger("coverage.2.execute"),
            "coverage_epoch": plan.coverage_epoch,
            "repair_round": 0,
            "authorization_scope_digest": "5" * 64,
            "activity_receipt_digest": "6" * 64,
            "plan_ref": plan.plan_ref.path,
            "plan_digest": plan.plan_digest,
            "case_execution_plan_ref": generation.case_execution_plan_ref.path,
            "case_execution_plan_digest": generation.case_execution_plan_ref.digest,
            "spec_digest": plan.spec_digest,
            "mapping_digest": generation.mapping_ref.digest,
            "sut_digest": plan.sut_digest,
            "technical_config_digest": plan.technical_config_digest,
            "validation_profile": "api_db_trace.v1",
            "sut": {
                "instance_id": "sut",
                "base_url": "http://127.0.0.1:1234",
                "sqlite_path": str(sqlite),
            },
            "sqlite": {"path": str(sqlite), "device": stat.st_dev, "inode": stat.st_ino},
            "inputs": plan.inputs,
            "evidence_root": f"qa/changes/{plan.change_id}/execution/{EXECUTION_ID}",
        }
    )


def test_load_otlp_records_returns_deduped_execution_spans(tmp_path: Path) -> None:
    raw = correlated_otlp()
    path = tmp_path / "telemetry.otlp.jsonl"
    path.write_bytes(raw + raw)
    records = load_otlp_records(path, EXECUTION_ID)
    ids = {(item["trace_id"], item["span_id"]) for item in records}
    assert (TRACE_ID, DRIVER) in ids
    assert (TRACE_ID, WRITE) in ids
    assert len(ids) == len(records)


def test_load_otlp_records_rejects_conflicting_duplicate_span(tmp_path: Path) -> None:
    first = correlated_otlp()
    conflict = correlated_otlp(username="other")
    path = tmp_path / "telemetry.otlp.jsonl"
    path.write_bytes(first + conflict)
    with pytest.raises(ValueError, match="conflict"):
        load_otlp_records(path, EXECUTION_ID)


def test_load_otlp_records_rejects_truncated_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.otlp.jsonl"
    path.write_bytes(correlated_otlp()[:-12])
    with pytest.raises(ValueError, match="truncat"):
        load_otlp_records(path, EXECUTION_ID)


def test_seal_writes_otlp_and_completion_only_after_export(tmp_path: Path) -> None:
    source = tmp_path / "otel" / "traces.jsonl"
    source.parent.mkdir()
    source.write_bytes(correlated_otlp())
    evidence = tmp_path / "qa" / "changes" / "CH-USER-001" / "execution" / EXECUTION_ID
    sealed = seal_telemetry_artifacts(
        evidence_root=evidence,
        source_otlp=source,
        execution_id=EXECUTION_ID,
        sut_instance_id="sut",
        driver_flush={"state": "complete"},
        sut_flush={"state": "complete"},
        collector_drain={"state": "complete"},
    )
    otlp = evidence / "telemetry.otlp.jsonl"
    completion = evidence / "telemetry-completion.json"
    assert otlp.read_bytes() == source.read_bytes()
    assert not (evidence / "complete").exists()
    document = TelemetryCompletionV1.model_validate_json(completion.read_bytes())
    assert document.state == "complete"
    assert document.archive.digest == hashlib.sha256(otlp.read_bytes()).hexdigest()
    assert document.archive.size == otlp.stat().st_size
    assert sealed["otlp_path"].endswith("telemetry.otlp.jsonl")


def test_seal_does_not_mark_complete_when_drain_times_out(tmp_path: Path) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_bytes(correlated_otlp())
    evidence = tmp_path / "evidence"
    seal_telemetry_artifacts(
        evidence_root=evidence,
        source_otlp=source,
        execution_id=EXECUTION_ID,
        sut_instance_id="sut",
        driver_flush={"state": "complete"},
        sut_flush={"state": "complete"},
        collector_drain={"state": "timeout", "reason": "drain_timeout"},
    )
    document = TelemetryCompletionV1.model_validate_json(
        (evidence / "telemetry-completion.json").read_bytes()
    )
    assert document.state == "incomplete"
    assert document.collector_drain.state == "timeout"
    assert document.collector_drain.reason == "drain_timeout"


def test_driver_injects_w3c_context_and_execution_id() -> None:
    from assurance_execution.operations.telemetry import driver_trace_headers

    headers = driver_trace_headers(EXECUTION_ID)
    assert "traceparent" in headers
    assert headers["aa-execution-id"] == EXECUTION_ID
    assert headers["traceparent"].startswith("00-")


def test_sut_request_hook_only_copies_test_identity_fields() -> None:
    from assurance_execution.contracts.telemetry import apply_sut_request_identity

    captured: dict[str, str] = {}

    class _Span:
        def set_attribute(self, key: str, value: str) -> None:
            captured[key] = value

    apply_sut_request_identity(
        _Span(),
        {
            "aa-execution-id": EXECUTION_ID,
            "authorization": "secret",
            "token": "nope",
            "x-other": "ignored",
        },
    )
    assert captured == {"aa.execution_id": EXECUTION_ID}


def test_oracle_observer_span_cannot_satisfy_user_write(tmp_path: Path) -> None:
    plan, prepared = _trace_plan(tmp_path)
    manifest = _manifest(tmp_path, plan, prepared)
    spans = parse_otlp_records(oracle_select_otlp())
    digest = hashlib.sha256(oracle_select_otlp()).hexdigest()
    completion = TelemetryCompletionV1.model_validate(
        complete_telemetry(digest=digest, size=len(oracle_select_otlp()))
    )
    observations = {
        item.obligation_id: item for item in check_trace_requirements(plan, manifest, spans, completion)
    }
    assert observations["trace.user_write"].state == "missing"


def _load_user_oracle_harness():
    import importlib.util

    path = Path(__file__).resolve().parents[4] / "benchmark" / "assurance-product" / "user_oracle_harness.py"
    spec = importlib.util.spec_from_file_location("user_oracle_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_driver_client_span_is_sealed_from_attempt_collector(tmp_path: Path) -> None:
    from assurance_execution.operations.telemetry import (
        collector_otlp_endpoint,
        drain_owned_collector,
        flush_driver_provider,
        load_otlp_records,
        start_driver_client_span,
    )

    _reset_otel_provider()
    harness = _load_user_oracle_harness()
    run_root = tmp_path / "run"
    run_root.mkdir()
    receipt = harness.start_collector(run_root=run_root, execution_id=EXECUTION_ID)
    try:
        endpoint = collector_otlp_endpoint(run_root)
        assert endpoint == receipt["otlp_endpoint"]
        span, headers = start_driver_client_span(
            EXECUTION_ID,
            "http://127.0.0.1:1234/api/v1/user/create",
            otlp_endpoint=endpoint,
            sut_instance_id="sut",
        )
        assert "traceparent" in headers
        span.end()
        driver_flush = flush_driver_provider()
        harness.flush_otel(run_root=run_root)
        drain = drain_owned_collector(run_root)
        evidence = tmp_path / "evidence"
        seal_telemetry_artifacts(
            evidence_root=evidence,
            source_otlp=Path(receipt["otlp_path"]),
            execution_id=EXECUTION_ID,
            sut_instance_id="sut",
            driver_flush=driver_flush,
            sut_flush={"state": "complete"},
            collector_drain=drain,
        )
        sealed = evidence / "telemetry.otlp.jsonl"
        records = load_otlp_records(sealed, EXECUTION_ID)
        driver = next(
            item
            for item in records
            if item.get("kind") == "CLIENT"
            and item.get("instrumentation") == "assurance.execution.http-driver"
            and item.get("name") == "POST /api/v1/user/create"
        )
        server_raw = (
            json.dumps(
                _record(
                    [
                        (
                            "opentelemetry.instrumentation.fastapi",
                            [
                                _span(
                                    span_id=SERVER,
                                    parent=str(driver["span_id"]),
                                    name="POST /api/v1/user/create",
                                    kind="SERVER",
                                    trace_id=str(driver["trace_id"]),
                                    attrs={**_identity(), "http.route": "/api/v1/user/create"},
                                )
                            ],
                        )
                    ]
                )
            )
            + "\n"
        ).encode()
        plan, prepared = _trace_plan(tmp_path)
        manifest = _manifest(tmp_path, plan, prepared)
        completion = TelemetryCompletionV1.model_validate_json(
            (evidence / "telemetry-completion.json").read_bytes()
        )
        observations = {
            item.obligation_id: item
            for item in check_trace_requirements(
                plan,
                manifest,
                (*records, *parse_otlp_records(server_raw)),
                completion,
            )
        }
        assert observations["trace.http"].state == "observed"
    finally:
        if (run_root / "otel" / "collector-process.json").is_file():
            try:
                harness.stop_collector(run_root=run_root)
            except Exception:
                drain_owned_collector(run_root)


def test_journal_refs_include_sealed_telemetry(tmp_path: Path) -> None:
    from assurance_execution.operations.verified_execution import ActionJournal, _journal_refs

    plan, prepared = _trace_plan(tmp_path)
    manifest = _manifest(tmp_path, plan, prepared)
    evidence = tmp_path / manifest.evidence_root
    raw = correlated_otlp()
    source = tmp_path / "traces.jsonl"
    source.write_bytes(raw)
    seal_telemetry_artifacts(
        evidence_root=evidence,
        source_otlp=source,
        execution_id=EXECUTION_ID,
        sut_instance_id="sut",
        driver_flush={"state": "complete"},
        sut_flush={"state": "complete"},
        collector_drain={"state": "complete"},
    )
    journal = ActionJournal(evidence, manifest, bytes(range(32)))
    journal.write("action_started", {"execution_id": EXECUTION_ID, "state": "started"})
    refs = _journal_refs(journal)
    names = {Path(item.path).name for item in refs}
    assert "telemetry.otlp.jsonl" in names
    assert "telemetry-completion.json" in names
    assert "action_started.json" in names


def _reset_otel_provider() -> None:
    import opentelemetry.trace as trace_api
    from opentelemetry.util._once import Once

    from assurance_execution.operations import telemetry as telemetry_ops

    trace_api._TRACER_PROVIDER = None
    trace_api._TRACER_PROVIDER_SET_ONCE = Once()
    telemetry_ops._DRIVER_EXPORT_ENDPOINT = None


def _user_sqlite(path: Path) -> None:
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE "user" (username TEXT, email TEXT, is_active INT, is_superuser INT, dept_id INT)'
        )
        connection.commit()


def _action_manifest(tmp_path: Path):
    plan, prepared = _trace_plan(tmp_path)
    sqlite = tmp_path / "verified.sqlite3"
    _user_sqlite(sqlite)
    manifest = _manifest(tmp_path, plan, prepared)
    return plan, manifest


def test_execute_frozen_action_keeps_driver_distinct_from_oracle(tmp_path: Path, monkeypatch) -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import ReadableSpan

    from assurance_execution.operations import verified_execution
    from assurance_execution.operations.sqlite_oracle import observe_user
    from assurance_execution.operations.verified_execution import ActionJournal, execute_frozen_action

    _reset_otel_provider()
    plan, manifest = _action_manifest(tmp_path)
    journal = ActionJournal(tmp_path / "evidence", manifest, bytes(range(32)))

    async def fake_post(*_args, **_kwargs):
        return {"state": "observed", "status": 200, "code": 200}

    monkeypatch.setattr(verified_execution, "_post_once", fake_post)
    execute_frozen_action(
        plan,
        manifest,
        journal,
        json.dumps({"token": "token", "user_password": "secret"}).encode(),
    )
    provider = trace.get_tracer_provider()
    resource = getattr(provider, "resource", None)
    attributes = getattr(resource, "attributes", {})
    assert attributes.get("service.name") == "assurance-execution-driver"
    assert attributes.get("service.instance.id") == "sut"
    driver = trace.get_tracer("assurance.execution.http-driver").start_span("identity-check")
    try:
        assert isinstance(driver, ReadableSpan)
        assert driver.resource.attributes.get("service.name") == "assurance-execution-driver"
        assert driver.instrumentation_scope is None or driver.instrumentation_scope.name == (
            "assurance.execution.http-driver"
        )
    finally:
        driver.end()
    oracle = observe_user(Path(manifest.sqlite.path), manifest.inputs.username, manifest.inputs.email)
    assert oracle["state"] == "observed"
    assert attributes.get("service.name") != "oracle"


def test_client_span_ends_after_post_action_oracle(tmp_path: Path, monkeypatch) -> None:
    from assurance_execution.operations import verified_execution
    from assurance_execution.operations.sqlite_oracle import observe_user
    from assurance_execution.operations.telemetry import start_driver_client_span
    from assurance_execution.operations.verified_execution import ActionJournal, execute_frozen_action

    _reset_otel_provider()
    plan, manifest = _action_manifest(tmp_path)
    journal = ActionJournal(tmp_path / "evidence", manifest, bytes(range(32)))
    sequence: list[str] = []
    original_observe = observe_user

    def tracking_observe(*args, **kwargs):
        result = original_observe(*args, **kwargs)
        sequence.append("oracle")
        return result

    def tracking_start(*args, **kwargs):
        span, headers = start_driver_client_span(*args, **kwargs)
        original_end = span.end

        def end(*end_args, **end_kwargs):
            sequence.append("span_end")
            return original_end(*end_args, **end_kwargs)

        span.end = end  # type: ignore[method-assign]
        sequence.append("span_start")
        return span, headers

    async def fake_post(*_args, **_kwargs):
        sequence.append("http")
        return {"state": "observed", "status": 200, "code": 200}

    monkeypatch.setattr(verified_execution, "observe_user", tracking_observe)
    monkeypatch.setattr(verified_execution, "start_driver_client_span", tracking_start)
    monkeypatch.setattr(verified_execution, "_post_once", fake_post)
    execute_frozen_action(
        plan,
        manifest,
        journal,
        json.dumps({"token": "token", "user_password": "secret"}).encode(),
    )
    assert sequence.count("oracle") == 2
    assert sequence.index("span_end") > max(index for index, item in enumerate(sequence) if item == "oracle")
    assert sequence.index("http") < sequence.index("span_end")


def test_missing_collector_file_seals_incomplete_completion(tmp_path: Path) -> None:
    from assurance_execution.operations.verified_execution import ActionJournal, _complete_trace_evidence

    plan, prepared = _trace_plan(tmp_path)
    manifest = _manifest(tmp_path, plan, prepared)
    journal = ActionJournal(tmp_path / manifest.evidence_root, manifest, bytes(range(32)))
    run_root = tmp_path / "run"
    run_root.mkdir()
    _complete_trace_evidence(journal, plan, manifest, run_root)
    completion_path = journal.root / "telemetry-completion.json"
    assert completion_path.is_file()
    document = TelemetryCompletionV1.model_validate_json(completion_path.read_bytes())
    assert document.state == "incomplete"
    assert document.archive.state == "incomplete"
    assert document.archive.reason is not None


def test_complete_trace_evidence_embeds_stages_without_extra_json(tmp_path: Path) -> None:
    from assurance_execution.operations.verified_execution import ActionJournal, _complete_trace_evidence

    plan, prepared = _trace_plan(tmp_path)
    manifest = _manifest(tmp_path, plan, prepared)
    journal = ActionJournal(tmp_path / manifest.evidence_root, manifest, bytes(range(32)))
    run_root = tmp_path / "run"
    otel = run_root / "otel"
    otel.mkdir(parents=True)
    (otel / "traces.jsonl").write_bytes(correlated_otlp())
    (otel / "flush-receipt.json").write_text(json.dumps({"state": "flushed"}), encoding="utf-8")
    finished = __import__("subprocess").Popen(["true"])
    finished.wait()
    (otel / "collector-process.json").write_text(
        json.dumps({"pid": finished.pid, "otlp_endpoint": "http://127.0.0.1:1"}),
        encoding="utf-8",
    )
    _complete_trace_evidence(journal, plan, manifest, run_root)
    document = TelemetryCompletionV1.model_validate_json(
        (journal.root / "telemetry-completion.json").read_bytes()
    )
    extra = {
        path.name for path in journal.root.glob("*.json") if path.name not in {"telemetry-completion.json"}
    }
    assert extra == set()
    assert document.driver_flush.state == "complete"
    assert document.sut_flush.state == "complete"
    assert document.collector_drain.state == "complete"
    assert document.driver_flush.receipt_ref is None
    assert document.sut_flush.receipt_ref is None
    assert document.collector_drain.receipt_ref is None
    assert document.archive.path == "telemetry.otlp.jsonl"
    assert document.archive.digest


def test_truncated_otlp_is_incomplete_on_producer(tmp_path: Path) -> None:
    from assurance_execution.operations.verified_execution import ActionJournal, collect_facts

    plan, prepared = _trace_plan(tmp_path)
    manifest = _manifest(tmp_path, plan, prepared)
    journal = ActionJournal(tmp_path / manifest.evidence_root, manifest, bytes(range(32)))
    journal.write(
        "action_terminal",
        {
            "initial": {"state": "observed", "rows": []},
            "http": {"state": "observed", "status": 200, "code": 200},
            "oracle": {
                "state": "observed",
                "rows": [manifest.inputs.model_dump(mode="json")],
            },
        },
    )
    truncated = correlated_otlp()[:-12]
    (journal.root / "telemetry.otlp.jsonl").write_bytes(truncated)
    (journal.root / "telemetry-completion.json").write_bytes(
        json.dumps(
            complete_telemetry(digest=hashlib.sha256(truncated).hexdigest(), size=len(truncated)),
            indent=2,
        ).encode()
    )
    facts = {item.obligation_id: item for item in collect_facts(journal, plan)}
    for obligation in ("trace.http", "trace.user_write", "trace.user_completed", "trace.drained"):
        assert facts[obligation].state == "missing"
        assert facts[obligation].reason == "otlp_truncated"


def test_oracle_select_does_not_steal_global_provider_on_setup_failure(tmp_path: Path) -> None:
    import inspect

    from opentelemetry import trace

    from assurance_execution.operations import sqlite_oracle
    from assurance_execution.operations.sqlite_oracle import observe_user

    source = inspect.getsource(sqlite_oracle._oracle_select)
    assert "except Exception" not in source
    _reset_otel_provider()
    sqlite = tmp_path / "user.sqlite3"
    _user_sqlite(sqlite)
    before = trace.get_tracer_provider()
    result = observe_user(sqlite, "nobody", "nobody@example.test")
    assert result["state"] == "observed"
    assert result["rows"] == []
    after = trace.get_tracer_provider()
    assert after is before or not hasattr(after, "add_span_processor")
    resource = getattr(after, "resource", None)
    attributes = getattr(resource, "attributes", {})
    assert attributes.get("service.name") != "oracle"
