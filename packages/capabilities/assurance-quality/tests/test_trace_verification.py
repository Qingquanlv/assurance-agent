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
from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
from assurance_quality.operations.verification import evaluate_verification
from graph_engine.attempts import AttemptKey, BusinessActivation
from tests.verified_assessment_fixture import _evidence
from tests.verified_generation_fixture import accepted_verified_execution_input

EXECUTION_ID = "12345678-1234-4123-8123-123456789abc"
STALE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TRACE_ID = "a" * 32
DRIVER = "b" * 16
SERVER = "c" * 16
WRITE = "d" * 16
DONE = "e" * 16
HELPER = "f" * 16
ORACLE = "1" * 16
ROLE = "2" * 16

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
) -> bytes:
    payload = {
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
    return (json.dumps(payload) + "\n").encode()


def _identity(execution_id: str = EXECUTION_ID) -> dict[str, object]:
    return {"aa.execution_id": execution_id}


def synthetic_chain(
    *,
    execution_id: str = EXECUTION_ID,
    instance: str = "sut",
    http: bool = True,
    write: bool = True,
    completed: bool = True,
    helper: bool = False,
    broken_parent: bool = False,
    write_table: str = "user",
    write_op: str = "INSERT",
    write_scope: str = "opentelemetry.instrumentation.tortoiseorm",
    write_role: str | None = None,
    username: str = "oracle_user",
    status: str = "UNSET",
) -> bytes:
    identity = _identity(execution_id)
    groups: list[tuple[str, list[dict[str, object]]]] = [
        (
            "assurance.execution.http-driver",
            [
                _span(
                    span_id=DRIVER,
                    name="POST /api/v1/user/create",
                    kind="CLIENT",
                    attrs={
                        **identity,
                        "http.method": "POST",
                        "http.url": "http://127.0.0.1:1234/api/v1/user/create",
                    },
                )
            ],
        )
    ]
    if http:
        groups.append(
            (
                "opentelemetry.instrumentation.fastapi",
                [
                    _span(
                        span_id=SERVER,
                        parent="" if broken_parent else DRIVER,
                        name="POST /api/v1/user/create",
                        kind="SERVER",
                        attrs={**identity, "http.route": "/api/v1/user/create"},
                    )
                ],
            )
        )
    parent = SERVER if http and not broken_parent else DRIVER
    if helper:
        groups.append(
            (
                "app.users.helpers",
                [_span(span_id=HELPER, parent=parent, name="persist_user", kind="INTERNAL", attrs=identity)],
            )
        )
        parent = HELPER
    if write:
        attrs = {
            **identity,
            "aa.db.table": write_table,
            "aa.db.operation": write_op,
            "aa.db.semconv": "1.24.0",
        }
        if write_role is not None:
            attrs["aa.role"] = write_role
        groups.append(
            (
                write_scope,
                [
                    _span(
                        span_id=WRITE,
                        parent=parent,
                        name=f"{write_table} {write_op}",
                        kind="CLIENT",
                        status=status,
                        attrs=attrs,
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
    return _record(groups, instance=instance)


def _plan(root: Path):
    prepared = accepted_verified_execution_input(root, validation_profile="api_db_trace.v1")
    ref = prepared.verification.case_execution_plan_ref  # type: ignore[union-attr]
    return CaseExecutionPlanSetV1.model_validate_json((root / ref.path).read_bytes()).cases[0], prepared


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


def _judge(root: Path, raw: bytes, *, drain: str = "complete"):
    plan, prepared = _plan(root)
    manifest = _manifest(root, plan, prepared)
    spans = parse_otlp_records(raw)
    completion = TelemetryCompletionV1.model_validate(
        {
            "schema_version": "1",
            "execution_id": EXECUTION_ID,
            "sut_instance_id": "sut",
            "driver_flush": {"state": "complete"},
            "sut_flush": {"state": "complete"},
            "file_export": {
                "state": drain,
                "reason": None if drain == "complete" else "drain_timeout",
            },
            "archive": {
                "state": "complete" if drain == "complete" else "incomplete",
                "reason": None if drain == "complete" else "drain_timeout",
                "path": "telemetry.otlp.jsonl",
                "digest": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            },
            "state": "complete" if drain == "complete" else "incomplete",
        }
    )
    observations = check_trace_requirements(plan, manifest, spans, completion)
    return {item.obligation_id: item for item in observations}


def test_correlated_helper_chain_observes_all_trace_obligations(tmp_path: Path) -> None:
    observed = _judge(tmp_path, synthetic_chain(helper=True))
    assert observed["trace.http"].state == "observed"
    assert observed["trace.user_write"].state == "observed"
    assert observed["trace.user_completed"].state == "observed"
    assert observed["trace.exported"].state == "observed"
    assert observed["trace.user_write"].actual is True


def test_missing_business_span_is_not_observed(tmp_path: Path) -> None:
    observed = _judge(tmp_path, synthetic_chain(http=False))
    assert observed["trace.http"].state == "missing"


def test_missing_write_span_is_not_observed(tmp_path: Path) -> None:
    observed = _judge(tmp_path, synthetic_chain(write=False))
    assert observed["trace.user_write"].state == "missing"
    assert observed["trace.http"].state == "observed"


def test_stale_execution_id_does_not_correlate(tmp_path: Path) -> None:
    observed = _judge(tmp_path, synthetic_chain(execution_id=STALE_ID))
    assert observed["trace.http"].state == "missing"
    assert observed["trace.user_write"].state == "missing"
    assert observed["trace.user_completed"].state == "missing"


def test_same_business_key_wrong_instance_does_not_correlate(tmp_path: Path) -> None:
    observed = _judge(tmp_path, synthetic_chain(instance="other-sut"))
    assert observed["trace.http"].state == "missing"
    assert observed["trace.user_write"].state == "missing"


def test_broken_parent_chain_does_not_correlate(tmp_path: Path) -> None:
    observed = _judge(tmp_path, synthetic_chain(broken_parent=True))
    assert observed["trace.http"].state == "missing" or observed["trace.user_write"].state == "missing"


def test_oracle_select_cannot_satisfy_user_write(tmp_path: Path) -> None:
    raw = _record(
        [
            (
                "assurance.execution.oracle",
                [
                    _span(
                        span_id=ORACLE,
                        name="oracle.user.select",
                        kind="CLIENT",
                        attrs={
                            **_identity(),
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
    observed = _judge(tmp_path, raw)
    assert observed["trace.user_write"].state == "missing"


def test_role_or_audit_table_write_cannot_satisfy_user_write(tmp_path: Path) -> None:
    observed = _judge(tmp_path, synthetic_chain(write_table="role"))
    assert observed["trace.user_write"].state == "missing"


def test_equivalent_insert_and_unset_status_still_match(tmp_path: Path) -> None:
    observed = _judge(
        tmp_path,
        synthetic_chain(write_op="INSERT", status="UNSET", helper=True),
    )
    assert observed["trace.user_write"].state == "observed"
    assert observed["trace.user_write"].actual is True


def test_parse_rejects_conflicting_duplicate_span_ids() -> None:
    first = synthetic_chain()
    second = synthetic_chain(username="other")
    with pytest.raises(ValueError, match="conflict"):
        parse_otlp_records(first + second)


def test_quality_evaluator_uses_replayed_trace_facts_not_a_second_matcher(tmp_path: Path) -> None:
    plan, prepared = _plan(tmp_path)
    manifest = _manifest(tmp_path, plan, prepared)
    raw = synthetic_chain(write=False)
    spans = parse_otlp_records(raw)
    completion = TelemetryCompletionV1.model_validate(
        {
            "schema_version": "1",
            "execution_id": EXECUTION_ID,
            "sut_instance_id": "sut",
            "driver_flush": {"state": "complete"},
            "sut_flush": {"state": "complete"},
            "file_export": {"state": "complete"},
            "archive": {
                "state": "complete",
                "path": "telemetry.otlp.jsonl",
                "digest": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            },
            "state": "complete",
        }
    )
    replayed = check_trace_requirements(plan, manifest, spans, completion)
    actuals = {
        "initial.user_absent": 0,
        "action.finished": True,
        "oracle.executed": True,
        "api.http_status": 200,
        "api.code": 200,
        "user.row_count": 1,
        "user.username": plan.inputs["username"],
        "user.email": plan.inputs["email"],
        "user.is_active": plan.inputs["is_active"],
        "user.is_superuser": plan.inputs["is_superuser"],
        "user.dept_id": plan.inputs["dept_id"],
        "trace.http": True,
        "trace.user_write": True,
        "trace.user_completed": True,
        "trace.exported": True,
    }
    states = {item.obligation_id: item.state for item in replayed if item.obligation_id.startswith("trace.")}
    for item in replayed:
        if item.state == "observed":
            actuals[item.obligation_id] = item.actual
    evidence = _evidence(plan, actuals=actuals, states=states, telemetry="complete")
    verdict = evaluate_verification(plan, evidence, completion_status="collected")
    assert verdict.verdict == "INCOMPLETE"
    assert verdict.by_id("trace.user_write").evidence_status == "missing"
    assert verdict.by_id("user.row_count").business_status == "satisfied"
