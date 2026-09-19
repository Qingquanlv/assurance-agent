"""Stdlib-only pytest collector. Do not import assurance, pydantic, or graph-engine."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.client import HTTPResponse
from typing import Any
from urllib.parse import urlparse


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


class _Observer:
    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.observations: list[dict[str, Any]] = []
        self.errors: list[str] = []

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
        expected = None
        key = None
        mrc_id = ""
        requirement_id = ""
        for requirement in self.context.get("requirements") or ():
            for item in requirement.get("observations") or ():
                for plan in self.context.get("method_plans") or ():
                    for binding in plan.get("observations") or ():
                        if binding.get("observation_id") == observation_id:
                            key = binding.get("observation_key")
                            mrc_id = plan.get("mrc_id") or ""
                            requirement_id = (
                                plan.get("requirement_id") or requirement.get("requirement_id") or ""
                            )
                if item.get("observation_key") == key:
                    expected = item.get("expected")
        if expected is None or key is None:
            raise RuntimeError(f"unknown observation_id: {observation_id}")
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
        passed = evaluate_status(expected=int(expected), actual=actual)
        self.observations.append(
            {
                "observation_id": observation_id,
                "observation_key": key,
                "mrc_id": mrc_id,
                "requirement_id": requirement_id,
                "actual_status": actual,
                "predicate_passed": passed,
            }
        )
        if not passed:
            raise AssertionError(f"assertion_mismatch:{observation_id}")
        return actual


def pytest_configure(config: Any) -> None:
    config._aa_observe = {
        "context": None,
        "observer": None,
        "collected": [],
        "errors": [],
        "tests": {},
        "complete": True,
    }
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
    if report.failed:
        session_config = getattr(report, "session", None)
        config = report.config if hasattr(report, "config") else getattr(session_config, "config", None)
        if config is not None:
            config._aa_observe["errors"].append("collection_failed")
            config._aa_observe["complete"] = False


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    del location


def pytest_runtest_logreport(report: Any) -> None:
    config = report.config if hasattr(report, "config") else None
    if config is None:
        return
    state = config._aa_observe
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


def pytest_fixture_setup(fixturedef: Any, request: Any) -> Any:
    if fixturedef.argname != "aa_observe":
        return None
    observer = request.config._aa_observe.get("observer")
    if observer is None:
        raise RuntimeError("aa_observe context is unavailable")
    return observer
