"""Stdlib-only pytest collector. Do not import assurance, pydantic, or graph-engine."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.client import HTTPResponse
from typing import Any
from urllib.parse import urlparse

import pytest


def evaluate_status(*, expected: int, actual: int) -> bool:
    return actual == expected


def _load_context() -> dict[str, Any]:
    path = os.environ.get("AA_OBSERVE_CONTEXT")
    if not path:
        raise RuntimeError("AA_OBSERVE_CONTEXT is required")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _origin_allowed(url: str, allowed: list[str]) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin in allowed


class _Binding:
    """The frozen expectation and plan coordinates for one observation id."""

    def __init__(self, *, key: str, expected: int, plan: dict[str, Any], row: dict[str, Any]) -> None:
        self.key = key
        self.expected = expected
        self.mrc_id = str(plan.get("mrc_id") or "")
        self.requirement_id = str(plan.get("requirement_id") or "")
        self.step_id = str(row.get("step_id") or "")
        self.test_nodeid = str(row.get("test_nodeid") or "")
        self.assertion_id = str(row.get("assertion_id") or "")


class _Observer:
    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.observations: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.nodeid = ""
        self._sequence = 0

    def bind_test(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self._sequence = 0

    def _binding(self, observation_id: str) -> _Binding:
        expectations: dict[tuple[str, str], int] = {}
        for requirement in self.context.get("requirements") or ():
            requirement_id = str(requirement.get("requirement_id") or "")
            for item in requirement.get("observations") or ():
                expected = item.get("expected")
                key = item.get("observation_key")
                if expected is not None and key is not None:
                    expectations[(requirement_id, str(key))] = int(expected)
        for plan in self.context.get("method_plans") or ():
            for row in plan.get("observations") or ():
                if row.get("observation_id") != observation_id:
                    continue
                key = str(row.get("observation_key") or "")
                expectation_key = (str(plan.get("requirement_id") or ""), key)
                if expectation_key not in expectations:
                    break
                return _Binding(
                    key=key,
                    expected=expectations[expectation_key],
                    plan=plan,
                    row=row,
                )
        raise RuntimeError(f"unknown observation_id: {observation_id}")

    def request(
        self,
        *,
        observation_id: str,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> int:
        allowed = [str(item) for item in (self.context.get("allowed_origins") or [])]
        if not _origin_allowed(url, allowed):
            raise RuntimeError(f"origin is not approved: {url}")
        binding = self._binding(observation_id)
        timeout = int(self.context.get("timeout_seconds") or 5)
        limit = int(self.context.get("max_response_bytes") or 65536)
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if not isinstance(response, HTTPResponse):
                    raise RuntimeError("response is not HTTP")
                actual = int(response.status)
                response.read(limit)
        except urllib.error.HTTPError as error:
            actual = int(error.code)
        except Exception as error:
            self.errors.append(f"request_error:{observation_id}:{type(error).__name__}")
            raise
        passed = evaluate_status(expected=binding.expected, actual=actual)
        nodeid = self.nodeid or binding.test_nodeid
        self.observations.append(
            {
                "observation_id": observation_id,
                "observation_key": binding.key,
                "mrc_id": binding.mrc_id,
                "requirement_id": binding.requirement_id,
                "test_nodeid": nodeid,
                "assertion_id": binding.assertion_id,
                "step_id": binding.step_id,
                "sequence_id": nodeid,
                "sequence_index": self._sequence,
                "actual_status": actual,
                "predicate_passed": passed,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "prerequisite_refs": [],
            }
        )
        self._sequence += 1
        if not passed:
            raise AssertionError(f"assertion_mismatch:{observation_id}")
        return actual


# A pytest report carries no config back-reference, so the session hooks read
# the state the configure hook installed here.
_STATE: dict[str, Any] | None = None


def pytest_configure(config: Any) -> None:
    global _STATE
    config._aa_observe = {
        "context": None,
        "observer": None,
        "collected": [],
        "errors": [],
        "tests": {},
        "complete": True,
    }
    _STATE = config._aa_observe
    try:
        context = _load_context()
    except Exception as error:
        config._aa_observe["errors"].append(f"context_error:{error}")
        config._aa_observe["complete"] = False
        return
    config._aa_observe["context"] = context
    config._aa_observe["observer"] = _Observer(context)


def pytest_collection_finish(session: Any) -> None:
    state = session.config._aa_observe
    state["collected"] = [item.nodeid for item in session.items]


def pytest_collectreport(report: Any) -> None:
    if report.failed and _STATE is not None:
        _STATE["errors"].append("collection_failed")
        _STATE["complete"] = False


def pytest_runtest_protocol(item: Any, nextitem: Any) -> None:
    del nextitem
    observer = item.config._aa_observe.get("observer")
    if observer is not None:
        observer.bind_test(item.nodeid)
    return None


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    del location


def pytest_runtest_logreport(report: Any) -> None:
    state = _STATE
    if state is None:
        return
    row = state["tests"].setdefault(
        report.nodeid,
        {"nodeid": report.nodeid, "outcome": "passed", "setup": None, "call": None, "teardown": None},
    )
    phase = {
        "outcome": report.outcome,
        "duration": float(getattr(report, "duration", 0.0) or 0.0),
        "longrepr": None if report.passed else str(getattr(report, "longrepr", "") or "")[:200],
    }
    row[report.when] = phase
    if report.failed:
        row["outcome"] = "failed"
    elif report.skipped and row["outcome"] != "failed":
        row["outcome"] = "skipped"


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    state = session.config._aa_observe
    output = os.environ.get("AA_OBSERVE_OUTPUT")
    if not output:
        return
    if os.path.lexists(output):
        state["errors"].append("output_preexisting")
        state["complete"] = False
        return
    tests = list(state["tests"].values())
    summary = {"collected": len(state["collected"]), "passed": 0, "failed": 0, "skipped": 0}
    for item in tests:
        summary[item["outcome"]] = summary.get(item["outcome"], 0) + 1
    identity = (state.get("context") or {}).get("identity") or {
        "plan_digest": "0" * 64,
        "method_plan_refs": [],
        "mapping_digest": "0" * 64,
        "batch_id": "unknown",
        "baseline_tree_id": "0" * 64,
        "runner_profile_digest": "0" * 64,
    }
    document = {
        "protocol_version": "1",
        "collection_token": os.environ.get("AA_OBSERVE_TOKEN", "missing"),
        "identity": identity,
        "complete": bool(state["complete"] and not state["errors"]),
        "pytest_exitstatus": int(exitstatus),
        "collected_nodeids": state["collected"],
        "collection_errors": tuple(state["errors"]),
        "observations": (state["observer"].observations if state.get("observer") else []),
        "report": {"summary": summary, "tests": tests},
    }
    with open(output, "x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, sort_keys=True))
        handle.flush()


@pytest.fixture
def aa_observe(request: Any) -> Any:
    observer = request.config._aa_observe.get("observer")
    if observer is None:
        raise RuntimeError("aa_observe context is unavailable")
    return observer
