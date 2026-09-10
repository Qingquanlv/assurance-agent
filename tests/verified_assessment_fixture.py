from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime
import hashlib
import json


from tests.verified_generation_fixture import accepted_verified_execution_input
from graph_engine.attempts import AttemptKey, BusinessActivation
from graph_engine.plugin_api import TaskOutcome
from graph_engine.canonical import JSONValue, canonical_json_bytes
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from assurance_quality.contracts.assessment import MaterializeAssessmentInputV1

EXECUTION_ID = "12345678-1234-4123-8123-123456789abc"


def _actuals(plan) -> dict[str, object]:
    values = {
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
        "trace.drained": True,
    }
    return {key: values[key] for key in plan.required}


def _evidence(plan, *, actuals=None, states=None, host="complete", collector="not_required"):
    from assurance_execution.contracts.verification import ObservationV1, VerificationEvidenceV1

    actuals = _actuals(plan) if actuals is None else actuals
    states = states or {}
    observations = []
    for obligation in plan.required:
        state = states.get(obligation, "observed")
        observations.append(
            ObservationV1.model_validate(
                {
                    "execution_id": EXECUTION_ID,
                    "obligation_id": obligation,
                    "state": state,
                    "actual": actuals.get(obligation),
                    "evidence_ref": (
                        {"path": f"qa/changes/{plan.change_id}/execution/fact.json", "digest": "a" * 64}
                        if state == "observed"
                        else None
                    ),
                    "reason": None if state == "observed" else "not_available",
                }
            )
        )
    return VerificationEvidenceV1.model_validate(
        {
            "execution_id": EXECUTION_ID,
            "manifest_digest": "b" * 64,
            "receipt_ref": {
                "path": f"qa/changes/{plan.change_id}/execution/process.json",
                "digest": "c" * 64,
            },
            "observations": tuple(observations),
            "host_completion": {
                "state": host,
                "reason": None if host in {"complete", "not_required"} else "host_failed",
            },
            "collector_completion": {
                "state": collector,
                "reason": None if collector in {"complete", "not_required"} else "collector_failed",
            },
            "state": (
                "collected"
                if host in {"complete", "not_required"} and collector in {"complete", "not_required"}
                else "incomplete"
            ),
        }
    )


def _write(root: Path, relative: str, payload: bytes):
    from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(payload).hexdigest())


def _otlp_bytes(
    execution_id: str,
    username: str,
    *,
    write: bool = True,
    completed: bool = True,
    helper: bool = False,
) -> bytes:
    def attr(key: str, value: object) -> dict[str, object]:
        return {"key": key, "value": {"stringValue": str(value)}}

    def span(
        span_id: str, name: str, kind: int, parent: str = "", attrs: dict[str, str] | None = None
    ) -> dict:
        return {
            "traceId": "a" * 32,
            "spanId": span_id,
            "parentSpanId": parent,
            "name": name,
            "kind": kind,
            "status": {"code": 0},
            "attributes": [attr(key, value) for key, value in (attrs or {}).items()],
        }

    identity = {"aa.execution_id": execution_id}
    groups = [
        (
            "assurance.execution.http-driver",
            [
                span(
                    "b" * 16,
                    "POST /api/v1/user/create",
                    3,
                    attrs={**identity, "http.url": "http://127.0.0.1:1234/api/v1/user/create"},
                )
            ],
        ),
        (
            "opentelemetry.instrumentation.fastapi",
            [
                span(
                    "c" * 16,
                    "POST /api/v1/user/create",
                    2,
                    "b" * 16,
                    {**identity, "http.route": "/api/v1/user/create"},
                )
            ],
        ),
    ]
    parent = "c" * 16
    if helper:
        groups.append(
            (
                "app.users.helpers",
                [span("f" * 16, "persist_user", 1, parent, identity)],
            )
        )
        parent = "f" * 16
    if write:
        groups.append(
            (
                "opentelemetry.instrumentation.tortoiseorm",
                [
                    span(
                        "d" * 16,
                        "user INSERT",
                        3,
                        parent,
                        {
                            **identity,
                            "aa.db.table": "user",
                            "aa.db.operation": "INSERT",
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
                    span(
                        "e" * 16,
                        "user.create.completed",
                        1,
                        "c" * 16,
                        {**identity, "user.username": username},
                    )
                ],
            )
        )
    return (
        json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                attr("service.name", "user-oracle-sut"),
                                attr("service.instance.id", "sut"),
                            ]
                        },
                        "scopeSpans": [{"scope": {"name": scope}, "spans": spans} for scope, spans in groups],
                    }
                ]
            }
        )
        + "\n"
    ).encode()


def _materialization_request(
    tmp_path: Path,
    *,
    authenticated: bool,
    http_unknown: bool = False,
    row_count: int = 1,
    http_status: int = 200,
    wrong_email: bool = False,
    process_reason: str | None = None,
    trace: bool = False,
    drop_write_span: bool = False,
    early_completed: bool = False,
    helper: bool = False,
    truncated_otlp: bool = False,
    producer_seal: bool = False,
    missing_export: bool = False,
) -> tuple[MaterializeAssessmentInputV1, object | None, str | None]:
    from assurance_execution.contracts.verification import (
        VerifiedProcessLimitsV1,
    )
    from assurance_execution.operations.verified_execution import ActionJournal, collect_facts
    from assurance_generation.contracts.execution_plan import CaseExecutionPlanSetV1
    from assurance_quality.contracts.assessment import MaterializeAssessmentInputV1
    from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
    from assurance_execution.contracts.verification import (
        VerificationManifestV1,
        VerifiedExecutionAuthorityV1,
        VerifiedExecutionResultV1,
    )
    from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1

    prepared = accepted_verified_execution_input(
        tmp_path, validation_profile="api_db_trace.v1" if trace else "api_db.v1"
    )
    generation = prepared.generation_result
    assert generation is not None and generation.case_execution_plan_ref is not None
    plan = CaseExecutionPlanSetV1.model_validate_json(
        (tmp_path / generation.case_execution_plan_ref.path).read_bytes()
    ).cases[0]
    execution_id = EXECUTION_ID
    run_root = tmp_path / "run"
    run_root.mkdir()
    sqlite = tmp_path / "verified.sqlite3"
    identity = sqlite.stat()
    manifest = VerificationManifestV1.model_validate(
        {
            "execution_id": execution_id,
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
            "validation_profile": plan.validation_profile,
            "sut": {
                "instance_id": "sut",
                "base_url": "http://127.0.0.1:1234",
                "sqlite_path": str(sqlite),
            },
            "sqlite": {"path": str(sqlite), "device": identity.st_dev, "inode": identity.st_ino},
            "inputs": plan.inputs,
            "evidence_root": f"qa/changes/{plan.change_id}/execution/{execution_id}",
        }
    )
    manifest_ref = _write(
        tmp_path,
        f"qa/changes/{plan.change_id}/execution/{execution_id}/manifest.json",
        canonical_json_bytes(cast(JSONValue, manifest.model_dump(mode="json"))),
    )
    authority = None
    authority_handle = None
    journal: ActionJournal | None = None
    if authenticated:
        from assurance_execution.contracts.verification import (
            ManagedSutAuthorityV1,
            VerifiedProcessReceiptV1,
        )

        token = bytes(range(32))
        token_path = run_root / ".ownership-token"
        token_path.write_bytes(token)
        token_path.chmod(0o600)
        token_stat = token_path.stat()
        authority_document = ManagedSutAuthorityV1.model_validate(
            {
                "run_root": str(run_root),
                "ownership_token": {
                    "path": str(token_path),
                    "device": token_stat.st_dev,
                    "inode": token_stat.st_ino,
                    "digest": f"sha256:{hashlib.sha256(token).hexdigest()}",
                },
                "prepare_receipt_digest": "7" * 64,
                "start_receipt_digest": "8" * 64,
                "authorization_scope_digest": manifest.authorization_scope_digest,
                "activity_receipt_digest": manifest.activity_receipt_digest,
            }
        )

        class _Secrets:
            def resolve(self, handle: str) -> bytes:
                assert handle == "sut.authority"
                return canonical_json_bytes(cast(JSONValue, authority_document.model_dump(mode="json")))

        authority = _Secrets()
        authority_handle = "sut.authority"
        journal = ActionJournal(tmp_path / manifest.evidence_root, manifest, token)
        journal.write("action_started", {"execution_id": execution_id, "state": "started"})
        database_identity = {
            "path": str(sqlite),
            "device": identity.st_dev,
            "inode": identity.st_ino,
        }
        database_metadata = {"size": identity.st_size, "mtime_ns": identity.st_mtime_ns}
        journal.write(
            "action_terminal",
            {
                "initial": {
                    "state": "observed",
                    "rows": [],
                    "reason": None,
                    "database_identity": database_identity,
                    "database_metadata": database_metadata,
                },
                "http": (
                    {"state": "timeout", "reason": "http_terminal_unknown"}
                    if http_unknown
                    else {"state": "observed", "status": http_status, "code": http_status}
                ),
                "oracle": {
                    "state": "observed",
                    "rows": [
                        {
                            "username": plan.inputs["username"],
                            "email": "wrong@example.test" if wrong_email else plan.inputs["email"],
                            "is_active": plan.inputs["is_active"],
                            "is_superuser": plan.inputs["is_superuser"],
                            "dept_id": plan.inputs["dept_id"],
                        }
                    ]
                    * row_count,
                    "reason": None,
                    "database_identity": database_identity,
                    "database_metadata": database_metadata,
                },
            },
        )
        journal.write(
            "process_terminal",
            VerifiedProcessReceiptV1(
                command=("pytest",),
                limits=VerifiedProcessLimitsV1(),
                exit_code=0,
                report={},
                reason=process_reason,
                request_count=1,
                stderr="",
                cleanup_confirmed=True,
            ).model_dump(mode="json"),
        )
        if trace:
            from assurance_execution.operations.telemetry import seal_telemetry_artifacts
            from assurance_execution.operations.verified_execution import _complete_trace_evidence

            raw = _otlp_bytes(
                execution_id,
                str(plan.inputs["username"]),
                write=not drop_write_span,
                completed=True,
                helper=helper,
            )
            if missing_export:
                _complete_trace_evidence(journal, plan, manifest, run_root)
            elif producer_seal:
                otel = run_root / "otel"
                otel.mkdir(parents=True, exist_ok=True)
                (otel / "traces.jsonl").write_bytes(raw)
                (otel / "flush-receipt.json").write_text(json.dumps({"state": "flushed"}), encoding="utf-8")
                finished = __import__("subprocess").Popen(["true"])
                finished.wait()
                (otel / "collector-process.json").write_text(
                    json.dumps({"pid": finished.pid, "otlp_endpoint": "http://127.0.0.1:1"}),
                    encoding="utf-8",
                )
                _complete_trace_evidence(journal, plan, manifest, run_root)
            elif truncated_otlp:
                raw = raw[:-12]
                (journal.root / "telemetry.otlp.jsonl").write_bytes(raw)
                completion = {
                    "schema_version": "1",
                    "execution_id": execution_id,
                    "sut_instance_id": "sut",
                    "driver_flush": {"state": "complete"},
                    "sut_flush": {"state": "complete"},
                    "collector_drain": {"state": "complete"},
                    "archive": {
                        "state": "complete",
                        "path": "telemetry.otlp.jsonl",
                        "digest": hashlib.sha256(raw).hexdigest(),
                        "size": len(raw),
                    },
                    "state": "complete",
                }
                (journal.root / "telemetry-completion.json").write_bytes(
                    (json.dumps(completion, indent=2, sort_keys=True) + "\n").encode()
                )
            else:
                source = tmp_path / "sealed-traces.jsonl"
                source.write_bytes(raw)
                seal_telemetry_artifacts(
                    evidence_root=journal.root,
                    source_otlp=source,
                    execution_id=execution_id,
                    sut_instance_id="sut",
                    driver_flush={"state": "complete"},
                    sut_flush={"state": "complete"},
                    collector_drain={"state": "complete"},
                )
        action_ref = next(
            EvidenceArtifactRefV1(
                path=f"{manifest.evidence_root}/{name}.json",
                digest=hashlib.sha256((journal.root / f"{name}.json").read_bytes()).hexdigest(),
            )
            for name in ("action_terminal",)
        )
        process_ref = EvidenceArtifactRefV1(
            path=f"{manifest.evidence_root}/process_terminal.json",
            digest=hashlib.sha256((journal.root / "process_terminal.json").read_bytes()).hexdigest(),
        )
        names = ["action_started.json", "action_terminal.json", "process_terminal.json"]
        if trace:
            names.append("telemetry-completion.json")
            if (journal.root / "telemetry.otlp.jsonl").is_file():
                names.append("telemetry.otlp.jsonl")
        raw_refs = tuple(
            sorted(
                (
                    EvidenceArtifactRefV1(
                        path=f"{manifest.evidence_root}/{name}",
                        digest=hashlib.sha256((journal.root / name).read_bytes()).hexdigest(),
                    )
                    for name in names
                ),
                key=lambda item: (item.path, item.digest),
            )
        )
    else:
        process_ref = _write(
            tmp_path,
            f"qa/changes/{plan.change_id}/execution/{execution_id}/process_terminal.json",
            b"process\n",
        )
        action_ref = process_ref
        raw_refs = (process_ref,)
    evidence = _evidence(plan).model_copy(
        update={
            "manifest_digest": manifest_ref.digest,
            "receipt_ref": process_ref,
            "observations": tuple(
                item.model_copy(update={"evidence_ref": action_ref}) for item in _evidence(plan).observations
            ),
        }
    )
    if journal is not None:
        from assurance_execution.contracts.verification import EvidenceCompletionV1

        observations = tuple(collect_facts(journal, plan))
        reason = process_reason or (
            "required_facts_missing" if any(item.state != "observed" for item in observations) else None
        )
        from assurance_execution.operations.verified_execution import _collector_completion

        collector = _collector_completion(journal, plan)
        incomplete = bool(reason) or collector.state not in {"complete", "not_required"}
        evidence = evidence.model_copy(
            update={
                "observations": observations,
                "host_completion": (
                    EvidenceCompletionV1(state="error", reason=reason)
                    if reason
                    else EvidenceCompletionV1(state="complete")
                ),
                "collector_completion": collector,
                "state": "incomplete" if incomplete else "collected",
            }
        )
    outcome = TaskOutcome.succeeded(evidence.model_dump(mode="json"))
    if authenticated:
        assert journal is not None
        journal.write("outcome", outcome.model_dump(mode="json"))
        outcome_ref = EvidenceArtifactRefV1(
            path=f"{manifest.evidence_root}/outcome.json",
            digest=hashlib.sha256((journal.root / "outcome.json").read_bytes()).hexdigest(),
        )
    else:
        outcome_ref = _write(
            tmp_path,
            f"qa/changes/{plan.change_id}/execution/{execution_id}/outcome.json",
            canonical_json_bytes(cast(JSONValue, outcome.model_dump(mode="json"))),
        )
    authority_result = VerifiedExecutionAuthorityV1(
        validation_profile=plan.validation_profile,
        change_id=plan.change_id,
        case_id=plan.case_id,
        reviewed_case=plan.reviewed_case,
        coverage_epoch=plan.coverage_epoch,
        repair_round=0,
        plan_digest=plan.plan_digest,
        plan_ref=plan.plan_ref,
        case_execution_plan_ref=generation.case_execution_plan_ref,
        case_execution_plan_digest=generation.case_execution_plan_ref.digest,
        spec_digest=plan.spec_digest,
        execution_id=execution_id,
        attempt_key=manifest.attempt_key,
        batch_id="verified-batch",
        mapping_digest=generation.mapping_ref.digest,
        manifest_ref=manifest_ref,
        evidence_ref=outcome_ref,
        raw_evidence_refs=raw_refs,
        executed_at=datetime(2026, 9, 6, tzinfo=UTC),
        completion_status=evidence.state,
        evidence=evidence,
    )
    if authenticated:
        assert journal is not None
        journal.write("execution_terminal", authority_result.model_dump(mode="json"))
        execution_authority_ref = EvidenceArtifactRefV1(
            path=f"{manifest.evidence_root}/execution_terminal.json",
            digest=hashlib.sha256((journal.root / "execution_terminal.json").read_bytes()).hexdigest(),
        )
    else:
        execution_authority_ref = _write(
            tmp_path,
            f"{manifest.evidence_root}/execution_terminal.json",
            b"authority\n",
        )
    verified = VerifiedExecutionResultV1(
        **authority_result.model_dump(mode="python"),
        execution_authority_ref=execution_authority_ref,
    )
    encoded = (json.dumps(verified.model_dump(mode="json"), indent=2) + "\n").encode()
    index_ref = _write(tmp_path, f"qa/changes/{plan.change_id}/execution/execute-result.json", encoded)
    cycle = VerifiedExecutionCycleResultV1.model_validate(
        {
            **verified.model_dump(mode="json", exclude={"evidence"}),
            "mapping_ref": generation.mapping_ref.model_dump(mode="json"),
            "execution_index_ref": index_ref.model_dump(mode="json"),
            "source_refs": [item.model_dump(mode="json") for item in generation.source_refs],
            "receipt": {"receipt_id": "kernel", "receipt_digest": "9" * 64},
        }
    )
    request = MaterializeAssessmentInputV1(
        plan_digest=plan.plan_digest,
        plan_ref=plan.plan_ref,
        reviewed_case=plan.reviewed_case,
        generation=generation,
        execution=cycle,
        policy_resource_id="assurance.product.configuration.product-policy",
        policy_sha256=prepared.plan_ref.digest,  # replaced below from the frozen plan
        execution_at=cycle.executed_at,
    )
    from assurance_intake.contracts.plan import decode_plan

    root_plan = decode_plan((tmp_path / prepared.plan_ref.path).read_bytes(), prepared.plan_ref)
    request = request.model_copy(update={"policy_sha256": root_plan.policy_digest})

    return request, authority, authority_handle
