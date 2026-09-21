from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

from assurance_execution.contracts.agent import RunTestsInputV1
from assurance_execution.contracts.observations import ObservationBundleV1
from assurance_execution.operations.runner import (
    ProcessReceipt,
    _public_pytest_argv,
    _scrubbed_env,
    run_observed_mapping,
    write_observation_bundle,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.obligations import (
    ObligationAssessmentRowV1,
    ObligationAssessmentV1,
    ObligationEvidenceFactsV1,
    ObligationGateDecision,
    obligation_gate,
)
from assurance_quality.operations.obligations import (
    _facts_for_obligation,
    _gap_codes,
    _method_for_obligation,
    _observations_for_obligation,
    decide_obligation,
    derive_obligation_gate_facts,
)

_PLAN = "c" * 64
_TREE = "a" * 64
_PROFILE = "b" * 64
_CORRECT = "correct"
_BATCH = "B-lockout"
_MRC = "MRC-LOCKOUT-1"
_REQUIREMENT = "REQ-LOCKOUT-1"
_OBSERVATION_ID = "OBS-LOCKOUT-1"
_OBSERVATION_KEY = "locked_valid_password"
_TEST_FILE = "qa/tests/api/test_lockout.py"
_TEST_NODEID = f"{_TEST_FILE}::test_account_locks_after_five_failures"

# The collector owns every observation. The case states the sequence and hands
# the one deciding request to aa_observe; it never asserts a status itself.
_TEST_SOURCE = f"""import os
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def _attempt(password):
    request = Request(os.environ["LOCKOUT_URL"], data=password.encode("utf-8"), method="POST")
    try:
        with urlopen(request, timeout=2) as response:
            return int(response.status)
    except HTTPError as error:
        return int(error.code)


def test_account_locks_after_five_failures(aa_observe):
    for _ in range(5):
        _attempt("wrong")
    aa_observe.request(
        observation_id={_OBSERVATION_ID!r},
        method="POST",
        url=os.environ["LOCKOUT_URL"],
        body=b"{_CORRECT}",
    )
"""


class LockoutExperimentReceipt(TypedDict):
    test_source_digest: str
    lockout_enabled: bool
    runtime_observations_ref: EvidenceArtifactRefV1
    observed_status: int | None


class LockoutCycleResult(TypedDict):
    assessment: ObligationAssessmentV1
    bundle: ObservationBundleV1
    gate_decision: ObligationGateDecision
    experiment_receipt: LockoutExperimentReceipt


class LockoutServer:
    """Loopback lockout fixture. Threshold 5 is fixture-owned, not a product default."""

    def __init__(self, lockout_enabled: bool) -> None:
        self.lockout_enabled = lockout_enabled
        self.failures = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("lockout server is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/login"

    def __enter__(self) -> LockoutServer:
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                password = self.rfile.read(length).decode("utf-8")
                if password != _CORRECT:
                    owner.failures += 1
                    self._reply(401)
                    return
                if owner.lockout_enabled and owner.failures >= 5:
                    self._reply(423)
                    return
                self._reply(200)

            def _reply(self, status: int) -> None:
                body = b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


class _LocalPytestHost:
    """Runs the real collector under this interpreter instead of `uv run --isolated`."""

    def __init__(self, *, lockout_url: str) -> None:
        self._lockout_url = lockout_url

    def spawn(self, argv: tuple[str, ...], cwd: Path) -> ProcessReceipt:
        public = _public_pytest_argv(argv)
        env = _scrubbed_env(argv, cwd)
        env["LOCKOUT_URL"] = self._lockout_url
        runner_root = str(files("assurance_execution").joinpath("resources/runner"))
        env["PYTHONPATH"] = os.pathsep.join(item for item in (runner_root, env.get("PYTHONPATH")) if item)
        command = (sys.executable, "-m", *public[public.index("pytest") :])
        completed = subprocess.run(  # noqa: S603
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        return ProcessReceipt(
            command=argv,
            exit_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            report=None,
        )


def _write(root: Path, relative: str, payload: bytes) -> EvidenceArtifactRefV1:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(payload).hexdigest())


def _observation_method(root: Path, *, omit_binding: bool) -> EvidenceArtifactRefV1:
    document = {
        "requirements": [
            {
                "requirement_id": _REQUIREMENT,
                "profile_id": "api-default",
                "prerequisites": [],
                "observations": [
                    {
                        "observation_key": _OBSERVATION_KEY,
                        "condition": "the valid password after five failures",
                        "predicate": "status_code_eq",
                        "expected": 423,
                        "basis_refs": [],
                    }
                ],
                "semantic_review_required": True,
                "subject_binding_required": False,
            }
        ],
        "method_plans": [
            {
                "mrc_id": _MRC,
                "requirement_id": _REQUIREMENT,
                "profile_id": "api-default",
                "case_ids": ["TC_LOCKOUT_1"],
                "prerequisites": [],
                "steps": [
                    {"step_id": "S1", "purpose": "setup", "action": "fail five logins"},
                    {"step_id": "S2", "purpose": "observe", "action": "log in with the valid password"},
                ],
                "observations": []
                if omit_binding
                else [
                    {
                        "observation_id": _OBSERVATION_ID,
                        "observation_key": _OBSERVATION_KEY,
                        "step_id": "S2",
                        "test_nodeid": _TEST_NODEID,
                        "assertion_id": "A1",
                    }
                ],
            }
        ],
    }
    return _write(
        root,
        "qa/results/plans/obligation-method-plans.json",
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _prepared_obligation() -> dict[str, object]:
    return {
        "mrc_id": _MRC,
        "key": "auth.lockout",
        "proposed_key": None,
        "category": "api",
        "layer": "api",
        "statement": "the account locks after five failed logins",
        "applicability_conditions": [],
        "expected_basis_refs": [
            {
                "source": {
                    "kind": "requirement",
                    "artifact": {"path": "qa/requirement.md", "digest": _TREE},
                    "locator": "L10",
                },
                "source_status": "authenticated",
            }
        ],
        "impact_row_ids": [],
        "required": True,
        "scope_disposition": "included",
        "exclusion_basis": None,
        "open_questions": [],
        "verification_requirements": [
            {
                "requirement_id": _REQUIREMENT,
                "profile_id": "api-default",
                "prerequisites": [],
                "observations": [
                    {
                        "observation_key": _OBSERVATION_KEY,
                        "condition": "the valid password after five failures",
                        "predicate": "status_code_eq",
                        "expected": 423,
                        "basis_refs": [
                            {
                                "kind": "requirement",
                                "artifact": {"path": "qa/requirement.md", "digest": _TREE},
                                "locator": "L10",
                            }
                        ],
                    }
                ],
                "semantic_review_required": True,
                "subject_binding_required": False,
            }
        ],
    }


def _semantic_review() -> dict[str, object]:
    basis = {
        "kind": "requirement",
        "artifact": {"path": "qa/requirement.md", "digest": _TREE},
        "locator": "L10",
    }
    return {
        "frozen_plan_digest": _PLAN,
        "mrc_id": _MRC,
        "requirement_id": _REQUIREMENT,
        "plan_ref": {"path": f"qa/results/plan/{_PLAN}/resolved-assurance-plan.json", "digest": _PLAN},
        "status": "pass",
        "reason": "423 is the locked status quoted from the requirement",
        "source_refs": [basis],
        "expectation_reviews": [
            {
                "observation_key": _OBSERVATION_KEY,
                "status": "pass",
                "reason": "the requirement quotes 423 for a locked account",
                "basis_refs": [basis],
            }
        ],
    }


def run_lockout_cycle(
    tmp_path: Path,
    *,
    lockout_enabled: bool,
    omit_locked_observation: bool = False,
) -> LockoutCycleResult:
    from assurance_generation.contracts.plans import ObligationMethodPlanV1
    from assurance_generation.contracts.reviews import ObligationSemanticReviewV1
    from assurance_intake.contracts.obligations import PreparedObligationV1

    tmp_path.mkdir(parents=True, exist_ok=True)
    encoded = _TEST_SOURCE.encode("utf-8")
    _write(tmp_path, _TEST_FILE, encoded)
    method_ref = _observation_method(tmp_path, omit_binding=omit_locked_observation)
    plan_ref = EvidenceArtifactRefV1(
        path=f"qa/results/plan/{_PLAN}/resolved-assurance-plan.json",
        digest=_PLAN,
    )
    payload = RunTestsInputV1.model_validate(
        {
            "change_id": "CH-LOCKOUT-1",
            "plan_digest": _PLAN,
            "plan_ref": plan_ref.model_dump(mode="json"),
            "batch_id": _BATCH,
            "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
            "mapping": {
                "schema_version": "1",
                "selected": [_TEST_NODEID],
                "mappings": [
                    {
                        "test": _TEST_NODEID,
                        "case_id": "TC_LOCKOUT_1",
                        "capability": "auth.lockout",
                        "layer": "api",
                    }
                ],
            },
            "capability_leafs": ["auth.lockout"],
            "case_ids": ["TC_LOCKOUT_1"],
            "baseline_tree_id": _TREE,
            "runner_profile_digest": _PROFILE,
            "timeout_seconds": 120,
            "method_plan_refs": [method_ref.model_dump(mode="json")],
        }
    )
    with LockoutServer(lockout_enabled) as server:
        origin = urlparse(server.url)
        payload = payload.model_copy(update={"allowed_origins": (f"{origin.scheme}://{origin.netloc}",)})
        _, bundle = run_observed_mapping(payload, tmp_path, _LocalPytestHost(lockout_url=server.url))
    assert bundle is not None
    observations_ref = write_observation_bundle(tmp_path, payload, bundle)

    obligation = PreparedObligationV1.model_validate(_prepared_obligation())
    plans = {
        item.mrc_id: item
        for item in (
            ObligationMethodPlanV1.model_validate(row)
            for row in json.loads((tmp_path / method_ref.path).read_bytes().decode("utf-8"))["method_plans"]
        )
    }
    review = ObligationSemanticReviewV1.model_validate(_semantic_review())
    method = _method_for_obligation(obligation, plans, {(_MRC, _REQUIREMENT): review})
    observations = _observations_for_obligation(obligation, method, bundle)
    facts: ObligationEvidenceFactsV1 = _facts_for_obligation(
        obligation=obligation,
        method=method,
        blocked=None,
        observations=observations,
        subject_matched=bundle.subject.status == "matched",
        counterexample_ref=observations_ref,
    )
    verdict = decide_obligation(facts)
    row = ObligationAssessmentRowV1(
        plan_digest=_PLAN,
        mrc_id=_MRC,
        verdict=verdict,
        evidence_refs=(observations_ref,),
        gap_codes=()
        if verdict == "supported"
        else _gap_codes(obligation=obligation, blocked=None, facts=facts, method=method),
    )
    assessment = ObligationAssessmentV1(plan_ref=plan_ref, rows=(row,))
    gate = obligation_gate(derive_obligation_gate_facts(assessment, required_ids=((_PLAN, _MRC),)))
    observed = next(
        (item.actual_status for item in bundle.observations if item.observation_id == _OBSERVATION_ID),
        None,
    )
    return {
        "assessment": assessment,
        "bundle": bundle,
        # This component experiment does not run the product/report graph and
        # must not fabricate its public achieved status.
        "gate_decision": gate,
        "experiment_receipt": {
            "test_source_digest": hashlib.sha256(encoded).hexdigest(),
            "lockout_enabled": lockout_enabled,
            "runtime_observations_ref": observations_ref,
            "observed_status": observed,
        },
    }


__all__ = [
    "LockoutCycleResult",
    "LockoutExperimentReceipt",
    "LockoutServer",
    "run_lockout_cycle",
]
