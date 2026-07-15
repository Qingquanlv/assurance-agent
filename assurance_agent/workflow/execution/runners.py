"""subprocess-driven test runners + result parsing.

pytest (api/e2e/fuzz) uses the --json-report protocol; performance uses Locust's
--csv output. Never fabricates: a missing test dir / runner / traffic yields a
SKIPPED result. subprocess.run is monkeypatched in unit tests.
"""
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.models import CoverageThreshold, PerformanceScenarioVerdict
from assurance_agent.workflow.execution.exec_config import PerfConfig
from assurance_agent.workflow.execution.pytest_parser import parse_pytest_json
from assurance_agent.workflow.execution.results import (
    CoverageResult,
    PerformanceResult,
    PytestTarget,
    ResultSource,
    TargetResult,
)

_RAW = "raw"


def run_pytest_target(
    *,
    project_root: Path,
    batch_dir: Path,
    change_id: str,
    batch_id: str,
    target: PytestTarget,
    test_dir: str,
    cov_package: str | None = None,
) -> TargetResult:
    raw_dir = batch_dir / _RAW
    log_path = raw_dir / f"{target}.log"
    report_path = raw_dir / f"{target}-report.json"

    if not (project_root / test_dir).exists():
        source = ResultSource(framework="pytest", raw_log=str(log_path), report_json=str(report_path))
        reason = f"No test targets found under {test_dir} — {target} SKIPPED."
        return _skipped_target(change_id, batch_id, target, f"uv run pytest {test_dir}", source, reason)

    raw_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "uv", "run", "pytest", test_dir,
        "-p", "no:cacheprovider",
        "--json-report", f"--json-report-file={report_path}",
    ]
    if cov_package:
        cov_json = raw_dir / "coverage.json"
        args += [f"--cov={cov_package}", "--cov-branch", f"--cov-report=json:{cov_json}"]
    command = " ".join(args)

    proc = subprocess.run(args, cwd=str(project_root), capture_output=True, text=True)  # noqa: S603
    log_path.write_text(f"$ {command}\n\n{proc.stdout or ''}\n{proc.stderr or ''}", encoding="utf-8")

    return parse_pytest_json(
        change_id=change_id, batch_id=batch_id, target=target,
        report_path=report_path, raw_log_path=str(log_path), command=command,
    )


def _skipped_target(
    change_id: str, batch_id: str, target: PytestTarget, command: str,
    source: ResultSource, reason: str,
) -> TargetResult:
    from assurance_agent.workflow.execution.results import CaseResult
    return TargetResult(
        change_id=change_id, batch_id=batch_id, target=target, status="skipped",
        command=command, source=source, total=0, passed=0, failed=0, skipped=0,
        cases=[], unmapped_tests=[CaseResult(
            case_id="", status="skipped", file="", test_name=reason,
            duration_ms=0, message=reason, raw_log_ref=source.raw_log,
        )],
    )


def parse_coverage_result(
    *, change_id: str, batch_id: str, batch_dir: Path, threshold: CoverageThreshold,
) -> CoverageResult:
    cov_json = batch_dir / _RAW / "coverage.json"
    if not cov_json.is_file():
        return CoverageResult(
            change_id=change_id, batch_id=batch_id, available=False,
            line_coverage=0.0, branch_coverage=0.0, threshold=threshold,
            status="SKIPPED", skip_reason="coverage.json not produced (pytest-cov unavailable or disabled)",
        )
    try:
        totals = json.loads(cov_json.read_text(encoding="utf-8")).get("totals", {})
    except (OSError, json.JSONDecodeError):
        totals = {}
    return _coverage_from_totals(change_id, batch_id, totals, threshold)


def _coverage_from_totals(
    change_id: str, batch_id: str, totals: dict[str, Any], threshold: CoverageThreshold,
) -> CoverageResult:
    line = float(totals.get("percent_covered", 0.0) or 0.0)
    num_branches = float(totals.get("num_branches", 0) or 0)
    covered_branches = float(totals.get("covered_branches", 0) or 0)
    branch = round(covered_branches / num_branches * 100, 2) if num_branches > 0 else 100.0
    status = "PASS" if line >= threshold.line and branch >= threshold.branch else "PASS_WITH_WARNINGS"
    return CoverageResult(
        change_id=change_id, batch_id=batch_id, available=True,
        line_coverage=line, branch_coverage=branch, threshold=threshold, status=status,
        source={"coverage_json": "raw/coverage.json"},
    )


# ── Performance (Locust) ─────────────────────────────────────────────────────

def run_performance_target(
    *,
    project_root: Path,
    change_dir: Path,
    batch_dir: Path,
    change_id: str,
    batch_id: str,
    perf_config: PerfConfig,
) -> PerformanceResult:
    raw_dir = batch_dir / _RAW
    log_path = raw_dir / "performance.log"

    def skipped(reason: str) -> PerformanceResult:
        raw_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text(reason, encoding="utf-8")
        return PerformanceResult(
            change_id=change_id, batch_id=batch_id, available=False, status="SKIPPED",
            scenarios=[], command="", source={"raw_log": str(log_path)},
        )

    if not perf_config.enabled:
        return skipped("Performance disabled in .aa/config.yaml (performance.enabled=false).")
    scenarios = load_perf_scenarios(change_dir)
    if not scenarios:
        return skipped("No type:Performance cases with thresholds found — performance SKIPPED.")
    perf_dir = project_root / "tests" / "perf"
    locustfiles = sorted(perf_dir.glob("locustfile*.py")) if perf_dir.is_dir() else []
    if not locustfiles:
        return skipped("No locustfiles under tests/perf/ — performance SKIPPED.")

    raw_dir.mkdir(parents=True, exist_ok=True)
    load = perf_config.default_load
    stats: dict[str, dict[str, float]] = {}
    commands: list[str] = []
    any_traffic = False
    for locustfile in locustfiles:
        prefix = raw_dir / f"locust_{locustfile.stem}"
        args = [
            "uv", "run", "locust", "-f", str(locustfile), "--headless",
            "-u", str(load["users"]), "-r", str(load["spawn_rate"]),
            "-t", f"{load['run_time_s']}s", "--host", perf_config.base_url,
            "--csv", str(prefix), "--only-summary",
        ]
        commands.append(" ".join(args))
        proc = subprocess.run(args, cwd=str(project_root), capture_output=True, text=True)  # noqa: S603
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(args)}\n\n{proc.stdout or ''}\n{proc.stderr or ''}\n")
        stats_csv = prefix.with_name(prefix.name + "_stats.csv")
        if stats_csv.is_file():
            for row in parse_locust_stats(stats_csv):
                if row["requests"] > 0:
                    any_traffic = True
                stats[row["name"]] = row

    if not any_traffic:
        return skipped("Locust ran but recorded no successful traffic (environment likely unreachable) — SKIPPED.")

    verdicts = build_scenario_verdicts(scenarios, stats)
    if any(v.verdict == "FAIL" for v in verdicts):
        status = "FAIL"
    elif any(v.verdict == "PASS" for v in verdicts):
        status = "PASS"
    else:
        status = "SKIPPED"
    return PerformanceResult(
        change_id=change_id, batch_id=batch_id, available=True, status=status,
        scenarios=verdicts, command=" && ".join(commands), source={"raw_log": str(log_path)},
    )


def parse_locust_stats(csv_path: Path) -> list[dict[str, Any]]:
    lines = [ln for ln in csv_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = [h.strip().lower() for h in lines[0].split(",")]
    idx_name = header.index("name") if "name" in header else -1
    idx_req = header.index("request count") if "request count" in header else -1
    idx_fail = header.index("failure count") if "failure count" in header else -1
    idx_p95 = header.index("95%") if "95%" in header else -1
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        cols = line.split(",")
        name = cols[idx_name].strip() if idx_name >= 0 and idx_name < len(cols) else ""
        if not name or name.lower() == "aggregated":
            continue
        rows.append({
            "name": name,
            "requests": _to_num(cols, idx_req),
            "failures": _to_num(cols, idx_fail),
            "p95": _to_num(cols, idx_p95),
        })
    return rows


def build_scenario_verdicts(
    scenarios: list[dict[str, Any]], stats_by_name: dict[str, dict[str, float]],
) -> list[PerformanceScenarioVerdict]:
    verdicts: list[PerformanceScenarioVerdict] = []
    for sc in scenarios:
        thr = sc["thresholds"]
        stat = stats_by_name.get(sc["capability"]) or stats_by_name.get(sc["endpoint"])
        if not stat or stat["requests"] == 0:
            verdicts.append(PerformanceScenarioVerdict(
                capability=sc["capability"], endpoint=sc["endpoint"],
                measured_p95_ms=None, threshold_p95_ms=float(thr["p95_ms"]),
                measured_error_rate=None, threshold_error_rate_max=float(thr["error_rate_max"]),
                verdict="SKIPPED",
            ))
            continue
        error_rate = stat["failures"] / stat["requests"]
        passed = stat["p95"] <= thr["p95_ms"] and error_rate <= thr["error_rate_max"]
        verdicts.append(PerformanceScenarioVerdict(
            capability=sc["capability"], endpoint=sc["endpoint"],
            measured_p95_ms=float(stat["p95"]), threshold_p95_ms=float(thr["p95_ms"]),
            measured_error_rate=round(error_rate, 4), threshold_error_rate_max=float(thr["error_rate_max"]),
            verdict="PASS" if passed else "FAIL",
        ))
    return verdicts


def load_perf_scenarios(change_dir: Path) -> list[dict[str, Any]]:
    cases_dir = change_dir / "cases"
    scenarios: list[dict[str, Any]] = []
    if not cases_dir.is_dir():
        return scenarios
    for path in cases_dir.rglob("*.y*ml"):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        for case in _collect_cases(doc):
            parsed = _parse_perf_case(case)
            if parsed:
                scenarios.append(parsed)
    return scenarios


def _collect_cases(doc: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(doc, dict):
        return out
    for key in ("added", "modified", "cases"):
        value = doc.get(key)
        if isinstance(value, list):
            out.extend(item for item in value if isinstance(item, dict))
    return out


def _parse_perf_case(case: dict[str, Any]) -> dict[str, Any] | None:
    if case.get("type") != "Performance":
        return None
    nested = (((case.get("automation") or {}).get("performance") or {}).get("scenario")) or {}
    thresholds = nested.get("thresholds") if nested.get("thresholds") else case.get("thresholds")
    if not isinstance(thresholds, dict) or thresholds.get("p95_ms") is None:
        return None
    perf = case.get("performance") or ((case.get("automation") or {}).get("performance")) or {}
    return {
        "capability": nested.get("capability") or perf.get("capability") or case.get("case_id") or "unknown",
        "endpoint": nested.get("endpoint") or perf.get("endpoint") or "",
        "thresholds": {
            "p95_ms": float(thresholds["p95_ms"]),
            "error_rate_max": float(thresholds.get("error_rate_max", 0.01)),
        },
    }


def _to_num(cols: list[str], idx: int) -> float:
    if idx < 0 or idx >= len(cols):
        return 0.0
    try:
        return float(cols[idx].strip())
    except ValueError:
        return 0.0
