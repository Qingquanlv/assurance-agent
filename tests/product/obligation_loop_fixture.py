from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.obligations import (
    ObligationAssessmentRowV1,
    ObligationAssessmentV1,
    ObligationEvidenceFactsV1,
)
from assurance_quality.contracts.obligations import obligation_gate
from assurance_quality.operations.obligations import (
    decide_obligation,
    derive_obligation_gate_facts,
)

_PLAN = "c" * 64
_CORRECT = "correct"
_TEST_SOURCE = """from urllib.error import HTTPError
from urllib.request import Request, urlopen
import os


def _status(password: str) -> int:
    request = Request(os.environ["LOCKOUT_URL"], data=password.encode("utf-8"), method="POST")
    try:
        with urlopen(request, timeout=2) as response:
            return int(response.status)
    except HTTPError as error:
        return int(error.code)


def test_account_locks_after_five_failures() -> None:
    for _ in range(5):
        assert _status("wrong") == 401
    assert _status("correct") == 423
"""


class LockoutExperimentReceipt(TypedDict):
    test_source_digest: str
    lockout_enabled: bool
    runtime_observations_ref: EvidenceArtifactRefV1


class LockoutCycleResult(TypedDict):
    assessment: ObligationAssessmentV1
    public_status: str
    artifact_refs: tuple[EvidenceArtifactRefV1, ...]
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


def _write_ref(root: Path, relative: str, payload: bytes) -> EvidenceArtifactRefV1:
    path = root.joinpath(*Path(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(payload).hexdigest())


def _run_case(url: str, source: Path) -> bool:
    spec = importlib.util.spec_from_file_location("lockout_case", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("lockout test source could not be loaded")
    module = importlib.util.module_from_spec(spec)
    previous = os.environ.get("LOCKOUT_URL")
    os.environ["LOCKOUT_URL"] = url
    try:
        spec.loader.exec_module(module)
        module.test_account_locks_after_five_failures()
        return True
    except AssertionError:
        return False
    finally:
        if previous is None:
            os.environ.pop("LOCKOUT_URL", None)
        else:
            os.environ["LOCKOUT_URL"] = previous


def run_lockout_cycle(
    tmp_path: Path,
    *,
    lockout_enabled: bool,
    omit_locked_observation: bool = False,
) -> LockoutCycleResult:
    test_path = tmp_path / "qa" / "tests" / "api" / "test_lockout.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _TEST_SOURCE.encode("utf-8")
    test_path.write_bytes(encoded)
    test_digest = hashlib.sha256(encoded).hexdigest()
    observations: list[dict[str, object]] = []
    with LockoutServer(lockout_enabled) as server:
        origin = f"{urlparse(server.url).scheme}://{urlparse(server.url).netloc}"
        passed = _run_case(server.url, test_path)
        observations.extend(
            {"observation_key": f"failed-login-{index}", "actual": 401, "expected": 401}
            for index in range(1, 6)
        )
        locked = {
            "observation_key": "locked-valid-password",
            "actual": 423 if lockout_enabled else 200,
            "expected": 423,
        }
        if not omit_locked_observation:
            observations.append(locked)
        del origin
    observation_ref = _write_ref(
        tmp_path,
        "qa/results/execution/epochs/0/batches/B-lockout/runtime-observations.json",
        (json.dumps({"observations": observations}, indent=2) + "\n").encode("utf-8"),
    )
    complete = not omit_locked_observation
    eligible = (not passed) and complete
    facts = ObligationEvidenceFactsV1.model_validate(
        {
            "expectation_confirmed": True,
            "eligible_counterexample": eligible,
            "subject_valid": True,
            "method_valid": True,
            "prerequisites_valid": True,
            "observations_complete": complete,
            "required_reviews_passed": True,
            "supporting_evidence_current": complete and passed,
            "counterexample_evidence_current": eligible,
            "counterexample_refs": (observation_ref,) if eligible else (),
        }
    )
    verdict = decide_obligation(facts)
    plan_ref = EvidenceArtifactRefV1(
        path=f"qa/results/plan/{_PLAN}/resolved-assurance-plan.json",
        digest=_PLAN,
    )
    row = ObligationAssessmentRowV1(
        plan_digest=_PLAN,
        mrc_id="MRC-LOCKOUT-1",
        verdict=verdict,
        evidence_refs=(observation_ref,),
        gap_codes=() if verdict != "inconclusive" else ("obligation_observation_missing",),
    )
    assessment = ObligationAssessmentV1(plan_ref=plan_ref, rows=(row,))
    gate = obligation_gate(derive_obligation_gate_facts(assessment, required_ids=((_PLAN, "MRC-LOCKOUT-1"),)))
    public_status = "achieved" if gate == "satisfied" and passed and complete else "not-achieved"
    return {
        "assessment": assessment,
        "public_status": public_status,
        "artifact_refs": (observation_ref,),
        "experiment_receipt": {
            "test_source_digest": test_digest,
            "lockout_enabled": lockout_enabled,
            "runtime_observations_ref": observation_ref,
        },
    }


__all__ = [
    "LockoutCycleResult",
    "LockoutExperimentReceipt",
    "LockoutServer",
    "run_lockout_cycle",
]
