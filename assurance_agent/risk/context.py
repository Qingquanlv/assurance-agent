"""Explore context aggregation (spec 第 5 节 risk).

净室重写 TS src/risk/{context_builder,git_diff,module_map,case_loader,
archive_sampler,pass_rate,historical_issues}.ts 的行为规则，聚合到本模块。
与 TS 的有意差异（已文档化，不依赖 M5）：archive 采样直接读
qa/archive/<id>/execution/runs/<batch>/{api,e2e}-result.json（按 mtime 取最新
batch，附 legacy execution/ 回退），不走 M5 的 execution-evidence primary-mode
完整性检查。generated_at 支持注入（now 参数）以便 golden 测试确定性。
"""

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, computed_field

from assurance_agent.artifacts.models.issues import Problem, ProblemProjection
from assurance_agent.change_location import archive_root
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.risk.safety import RiskSafetyError, resolve_requirement_path
from assurance_agent.workflow.issues.projection import dump_projection

Confidence = str  # "high" | "medium" | "low"
_CONF_RANK = {"high": 3, "medium": 2, "low": 1}
_PASS_RATE_FAIL_THRESHOLD = 0.85
_WINDOW_K = 3
_LAYERS = ["api", "e2e"]
_XFAIL_RATIONALE = "MVP conservatively treats xfailed/xpassed as failed (non-green or expectation mismatch)."


# ---------- models ----------
class GitDiffResult(BaseModel):
    changed_files: list[str]
    no_git: bool
    degraded_reasons: list[str]


class ModuleImpact(BaseModel):
    name: str
    confidence: Confidence
    matched_rules: list[str]
    changed_files: list[str]
    reason: str | None = None


class CaseSignal(BaseModel):
    case_id: str
    module: str
    priority: str | None = None
    automation_status: str | None = None
    flaky: bool = False


class TestHealthEntry(BaseModel):
    module: str
    layer: str
    runs_sampled: int
    pass_rate: float
    recent_fail_case_ids: list[str]
    evidence_id: str


class HistoricalIssue(BaseModel):
    problem_id: str
    classification: str
    status: str
    severity: str
    affected_surface: dict[str, str] | None = None
    first_seen_change_id: str
    first_seen_occurrence_id: str
    last_seen_change_id: str
    last_seen_occurrence_id: str
    occurrence_count: int
    evidence_id: str
    module: str | None = None
    endpoint: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        return self.problem_id


class EvidenceEntry(BaseModel):
    id: str
    type: str  # test_pass_rate | historical_issue | code_change
    module: str | None = None
    layer: str | None = None
    value: float | None = None
    runs_sampled: int | None = None
    below_fail_threshold: bool | None = None
    endpoint: str | None = None
    issue_id: str | None = None
    confidence: Confidence | None = None
    changed_files: list[str] | None = None
    source: str
    projection_digest: str | None = None
    occurrence_ids: list[str] | None = None


class ImpactBlock(BaseModel):
    diff_base: str
    changed_files: list[str]
    modules: list[ModuleImpact]
    affected_case_ids: list[str]
    affected_cases_by_module: dict[str, list[str]]
    affected_test_files: list[str]


class RiskContext(BaseModel):
    schema_version: str = "1.0"
    change_id: str
    generated_at: str
    requirement_summary: str | None = None
    aggregation_policy: dict
    archive_window: dict
    staleness: dict
    impact: ImpactBlock
    case_signals: list[CaseSignal]
    test_health: list[TestHealthEntry]
    historical_issues: list[HistoricalIssue]
    evidence: list[EvidenceEntry]
    degraded: bool
    degraded_reasons: list[str]
    no_git: bool | None = None


# ---------- helpers: case id ----------
def canonicalize_case_id(raw: str) -> str:
    return re.sub(r"-", "_", raw.strip().upper())


# ---------- git diff ----------
def _run_git(project_root: Path, args: list[str]) -> tuple[bool, str, str]:
    try:
        proc = subprocess.run(["git", *args], cwd=project_root, capture_output=True, text=True, shell=False)
    except FileNotFoundError:
        return False, "", "git not found"
    return proc.returncode == 0, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def get_changed_files(project_root: Path, diff_base: str) -> GitDiffResult:
    if not (project_root / ".git").exists():
        return GitDiffResult(
            changed_files=[], no_git=True, degraded_reasons=["no_git: project root is not a git repository"]
        )
    degraded: list[str] = []
    ok, base, err = _run_git(project_root, ["merge-base", "HEAD", diff_base])
    if not ok or not base:
        degraded.append(f"git merge-base HEAD {diff_base} failed: {err or 'unknown error'}")
        ok2, out2, err2 = _run_git(project_root, ["diff", "--name-only", diff_base])
        if not ok2:
            degraded.append(f"git diff --name-only {diff_base} failed: {err2 or 'unknown error'}")
            return GitDiffResult(changed_files=[], no_git=False, degraded_reasons=degraded)
        return GitDiffResult(changed_files=_parse_name_only(out2), no_git=False, degraded_reasons=degraded)
    ok3, out3, err3 = _run_git(project_root, ["diff", "--name-only", base, "HEAD"])
    if not ok3:
        degraded.append(f"git diff failed: {err3 or 'unknown error'}")
        return GitDiffResult(changed_files=[], no_git=False, degraded_reasons=degraded)
    return GitDiffResult(changed_files=_parse_name_only(out3), no_git=False, degraded_reasons=degraded)


def _parse_name_only(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.split("\n") if line.strip()]


# ---------- module map ----------
_DEFAULT_RULES = [
    {
        "pattern": "backend/**",
        "modules": ["backend"],
        "confidence": "low",
        "reason": "default backend path mapping",
    },
    {
        "pattern": "frontend/**",
        "modules": ["frontend"],
        "confidence": "low",
        "reason": "default frontend path mapping",
    },
]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _load_module_rules(project_root: Path) -> list[dict]:
    map_path = project_root / ".aa" / "module-map.yaml"
    if not map_path.is_file():
        return _DEFAULT_RULES
    raw = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    rules = raw.get("rules") if isinstance(raw, dict) else None
    return rules if rules else _DEFAULT_RULES


def _merge_conf(a: Confidence, b: Confidence) -> Confidence:
    return a if _CONF_RANK.get(a, 0) >= _CONF_RANK.get(b, 0) else b


def _match_modules(changed_files: list[str], rules: list[dict]) -> list[ModuleImpact]:
    by_name: dict[str, ModuleImpact] = {}
    for file in changed_files:
        norm = file.replace("\\", "/")
        for rule in rules:
            if not _glob_to_regex(rule["pattern"]).match(norm):
                continue
            for mod in rule["modules"]:
                existing = by_name.get(mod)
                if existing is None:
                    by_name[mod] = ModuleImpact(
                        name=mod,
                        confidence=rule["confidence"],
                        matched_rules=[rule["pattern"]],
                        changed_files=[file],
                        reason=rule.get("reason"),
                    )
                else:
                    if rule["pattern"] not in existing.matched_rules:
                        existing.matched_rules.append(rule["pattern"])
                    if file not in existing.changed_files:
                        existing.changed_files.append(file)
                    existing.confidence = _merge_conf(existing.confidence, rule["confidence"])
    return sorted(by_name.values(), key=lambda m: m.name)


# ---------- case loader ----------
def _load_cases(project_root: Path) -> list[dict]:
    cases_root = project_root / "qa" / "cases"
    if not cases_root.is_dir():
        return []
    by_id: dict[str, dict] = {}
    for path in sorted(cases_root.rglob("*")):
        if not path.is_file():
            continue
        if not (path.name == "case.yaml" or path.name.endswith(".case.yaml")):
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        default_module = _infer_module(path, cases_root)
        for item in _collect_case_items(doc):
            if not isinstance(item, dict):
                continue
            case_id = item.get("case_id") or item.get("id")
            if not isinstance(case_id, str):
                continue
            automation = item.get("automation")
            by_id[case_id] = {
                "case_id": case_id,
                "module": item["module"] if isinstance(item.get("module"), str) else default_module,
                "priority": item.get("priority") if isinstance(item.get("priority"), str) else None,
                "flaky": item.get("flaky") is True,
                "automation_required": isinstance(automation, dict) and automation.get("required") is True,
            }
    return [by_id[k] for k in sorted(by_id)]


def load_known_case_ids(project_root: Path) -> list[str]:
    """All case ids declared under qa/cases (TS loadCasesFromQa(...).map(case_id))."""
    return [c["case_id"] for c in _load_cases(project_root)]


def _infer_module(path: Path, cases_root: Path) -> str:
    rel = path.parent.relative_to(cases_root).parts
    return rel[0] if rel else "unknown"


def _collect_case_items(doc: dict) -> list:
    items: list = []
    for key in ("cases", "added", "modified"):
        if isinstance(doc.get(key), list):
            items.extend(doc[key])
    return items or [doc]


def _resolve_affected(
    modules: list[ModuleImpact], cases: list[dict]
) -> tuple[list[str], dict[str, list[str]], list[dict]]:
    names = {m.name for m in modules}
    by_module: dict[str, list[str]] = {}
    ids: list[str] = []
    signals: list[dict] = []
    for c in cases:
        if c["module"] not in names:
            continue
        ids.append(c["case_id"])
        signals.append(c)
        by_module.setdefault(c["module"], []).append(c["case_id"])
    for k in by_module:
        by_module[k].sort()
    return sorted(set(ids)), by_module, signals


# ---------- archive sampling ----------
def _read_archived_at_ms(archive_path: Path) -> float:
    summary = archive_path / "archive-summary.md"
    if summary.is_file():
        m = re.search(r"archived_at:\s*['\"]?([^'\"\n]+)", summary.read_text(encoding="utf-8"), re.IGNORECASE)
        if m:
            try:
                return datetime.fromisoformat(m.group(1).strip().replace("Z", "+00:00")).timestamp() * 1000
            except ValueError:
                pass
    return archive_path.stat().st_mtime * 1000


def _latest_batch(archive_path: Path) -> dict | None:
    runs_dir = archive_path / "execution" / "runs"
    if runs_dir.is_dir():
        best: dict | None = None
        for entry in runs_dir.iterdir():
            if not entry.is_dir():
                continue
            api = entry / "api-result.json"
            e2e = entry / "e2e-result.json"
            sample = {
                "batch_id": entry.name,
                "batch_mtime_ms": entry.stat().st_mtime * 1000,
                "api_result_path": api if api.is_file() else None,
                "e2e_result_path": e2e if e2e.is_file() else None,
            }
            if best is None or sample["batch_mtime_ms"] > best["batch_mtime_ms"]:
                best = sample
        return best
    legacy = archive_path / "execution"
    if (legacy / "api-result.json").is_file():
        e2e = legacy / "e2e-result.json"
        return {
            "batch_id": "legacy",
            "batch_mtime_ms": legacy.stat().st_mtime * 1000,
            "api_result_path": legacy / "api-result.json",
            "e2e_result_path": e2e if e2e.is_file() else None,
        }
    return None


def _sample_archives(project_root: Path, depth: int) -> list[dict]:
    # Honor a configured qa.archive root; degrade to the default layout when the
    # project has no .aa/config.yaml (risk can run in a bare directory).
    try:
        root = archive_root(project_root)
    except ConfigNotFoundError:
        root = project_root / "qa" / "archive"
    if not root.is_dir():
        return []
    entries = [
        {"archive_id": e.name, "archive_path": e, "archived_at_ms": _read_archived_at_ms(e)}
        for e in root.iterdir()
        if e.is_dir()
    ]
    entries.sort(key=lambda a: a["archived_at_ms"], reverse=True)
    entries = entries[:depth]
    for e in entries:
        e["latest_batch"] = _latest_batch(e["archive_path"])
    return entries


def _read_layer_cases(path: Path | None, layer: str) -> list[dict]:
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if data.get("target") != layer:
        return []
    cases = data.get("cases")
    return cases if isinstance(cases, list) else []


# ---------- pass rate ----------
def _normalize_status(status: str) -> str:
    s = status.lower()
    if s == "passed":
        return "passed"
    if s in ("skipped", "not_run"):
        return "skipped"
    if s in ("xfailed", "xpassed", "failed"):
        return "failed"
    return "ignored"


def _aggregate_case_pass_rate(samples: list[list[dict]]) -> dict[str, dict]:
    counts: dict[str, dict[str, int]] = {}
    for batch in samples:
        for row in batch:
            norm = _normalize_status(str(row.get("status", "")))
            if norm in ("skipped", "ignored"):
                continue
            key = canonicalize_case_id(str(row.get("case_id", "")))
            cur = counts.setdefault(key, {"passed": 0, "executed": 0})
            cur["executed"] += 1
            if norm == "passed":
                cur["passed"] += 1
    return {
        k: {
            "passed": v["passed"],
            "executed": v["executed"],
            "rate": v["passed"] / v["executed"] if v["executed"] else 0.0,
        }
        for k, v in counts.items()
    }


def _module_pass_rate(case_ids: list[str], case_rates: dict[str, dict]) -> dict:
    passed = executed = 0
    for cid in case_ids:
        r = case_rates.get(canonicalize_case_id(cid))
        if not r:
            continue
        passed += r["passed"]
        executed += r["executed"]
    return {"passed": passed, "executed": executed, "rate": passed / executed if executed else 0.0}


def _recent_fail_case_ids(batches: list[dict], layer: str) -> list[str]:
    ordered_batches = sorted(batches, key=lambda b: b["batch_mtime_ms"], reverse=True)[:_WINDOW_K]
    seen: set[str] = set()
    out: list[str] = []
    for batch in ordered_batches:
        fp = batch.get("api_result_path") if layer == "api" else batch.get("e2e_result_path")
        for row in _read_layer_cases(fp, layer):
            if _normalize_status(str(row.get("status", ""))) != "failed":
                continue
            key = canonicalize_case_id(str(row.get("case_id", "")))
            if key in seen:
                continue
            seen.add(key)
            out.append(str(row.get("case_id", "")))
    return out


# ---------- structured problem projection ----------
def _sanitize_evidence_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).upper()


def _problem_evidence_id(problem_id: str) -> str:
    return "EV-PROBLEM-" + _sanitize_evidence_token(problem_id)


def _projection_digest(projection: ProblemProjection) -> str:
    return "sha256:" + hashlib.sha256(dump_projection(projection)).hexdigest()


def _is_merge_alias(problem: Problem) -> bool:
    resolution = problem.resolution
    return resolution is not None and resolution.disposition.startswith("merged_into:")


def _derive_surface_conveniences(
    affected_surface: dict[str, str] | None,
) -> tuple[str | None, str | None]:
    if not affected_surface:
        return None, None
    kind = affected_surface.get("kind")
    value = affected_surface.get("value")
    if not isinstance(value, str) or not value:
        return None, None
    if kind == "module":
        return value, None
    if kind == "endpoint":
        return None, value
    return None, None


def _load_problem_projection(
    project_root: Path,
) -> tuple[ProblemProjection | None, str | None]:
    path = project_root / "qa" / "issues" / "problems.json"
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ProblemProjection.model_validate(data), None
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return None, f"corrupt_problems_projection: {path}: {exc}"


def _problems_to_historical_issues(
    projection: ProblemProjection,
) -> tuple[list[HistoricalIssue], list[EvidenceEntry]]:
    digest = _projection_digest(projection)
    source = "qa/issues/problems.json"
    issues: list[HistoricalIssue] = []
    evidence: list[EvidenceEntry] = []
    for problem in projection.problems:
        if _is_merge_alias(problem):
            continue
        module, endpoint = _derive_surface_conveniences(None)
        ev_id = _problem_evidence_id(problem.problem_id)
        issues.append(
            HistoricalIssue(
                problem_id=problem.problem_id,
                classification=problem.assessment.classification,
                status=problem.status,
                severity=problem.assessment.severity,
                affected_surface=None,
                first_seen_change_id=problem.first_seen.change_id,
                first_seen_occurrence_id=problem.first_seen.occurrence_id,
                last_seen_change_id=problem.last_seen.change_id,
                last_seen_occurrence_id=problem.last_seen.occurrence_id,
                occurrence_count=len(problem.occurrences),
                evidence_id=ev_id,
                module=module,
                endpoint=endpoint,
            )
        )
        evidence.append(
            EvidenceEntry(
                id=ev_id,
                type="historical_issue",
                module=module,
                endpoint=endpoint,
                issue_id=problem.problem_id,
                source=source,
                projection_digest=digest,
                occurrence_ids=list(problem.occurrences),
            )
        )
    issues.sort(key=lambda h: h.problem_id)
    evidence.sort(key=lambda e: e.id)
    return issues, evidence


# ---------- aggregation entrypoint ----------
def build_risk_context(
    *,
    change_id: str,
    project_root: Path,
    diff_base: str = "main",
    archive_depth: int = 10,
    staleness_days: int = 30,
    requirement_path: str | None = None,
    now: str | None = None,
) -> RiskContext:
    degraded_reasons: list[str] = []
    evidence: list[EvidenceEntry] = []
    test_health: list[TestHealthEntry] = []

    requirement_summary: str | None = None
    if requirement_path:
        req = resolve_requirement_path(project_root, requirement_path)
        if not req.is_file():
            raise RiskSafetyError(f"--requirement is not a file: {req}")
        requirement_summary = req.read_text(encoding="utf-8")[:2000]

    git = get_changed_files(project_root, diff_base)
    degraded_reasons.extend(git.degraded_reasons)

    modules = _match_modules(git.changed_files, _load_module_rules(project_root))
    all_cases = _load_cases(project_root)
    affected_ids, affected_by_module, signals = _resolve_affected(modules, all_cases)

    for mod in modules:
        if not mod.changed_files:
            continue
        ev_id = f"EV-DIFF-{mod.name.upper()}-{mod.confidence.upper()}"
        evidence.append(
            EvidenceEntry(
                id=ev_id,
                type="code_change",
                module=mod.name,
                confidence=mod.confidence,
                changed_files=mod.changed_files,
                source="git diff",
            )
        )

    archives = _sample_archives(project_root, archive_depth)
    batches = [a["latest_batch"] for a in archives if a.get("latest_batch")]

    for layer in _LAYERS:
        samples = [
            _read_layer_cases(b.get("api_result_path") if layer == "api" else b.get("e2e_result_path"), layer)
            for b in batches
        ]
        samples = [s for s in samples if s]
        case_rates = _aggregate_case_pass_rate(samples)
        recent_fails = _recent_fail_case_ids(batches, layer)
        for mod in modules:
            case_ids = affected_by_module.get(mod.name, [])
            if not case_ids:
                continue
            keys = {canonicalize_case_id(c) for c in case_ids}
            rate = _module_pass_rate(case_ids, case_rates)
            if rate["executed"] == 0:
                continue
            ev_id = f"EV-TEST-HEALTH-{mod.name.upper()}-{layer.upper()}"
            fails = [f for f in recent_fails if canonicalize_case_id(f) in keys]
            test_health.append(
                TestHealthEntry(
                    module=mod.name,
                    layer=layer,
                    runs_sampled=rate["executed"],
                    pass_rate=rate["rate"],
                    recent_fail_case_ids=fails,
                    evidence_id=ev_id,
                )
            )
            evidence.append(
                EvidenceEntry(
                    id=ev_id,
                    type="test_pass_rate",
                    module=mod.name,
                    layer=layer,
                    value=rate["rate"],
                    runs_sampled=rate["executed"],
                    below_fail_threshold=rate["rate"] < _PASS_RATE_FAIL_THRESHOLD,
                    source="qa/archive",
                )
            )

    projection, corrupt_reason = _load_problem_projection(project_root)
    if corrupt_reason:
        degraded_reasons.append(corrupt_reason)
        hist_issues: list[HistoricalIssue] = []
    elif projection is None or not projection.problems:
        degraded_reasons.append("no_history: qa/issues/problems.json is missing or empty")
        hist_issues = []
    else:
        hist_issues, hist_evidence = _problems_to_historical_issues(projection)
        evidence.extend(hist_evidence)

    newest_ms = archives[0]["archived_at_ms"] if archives else None
    oldest_ms = archives[-1]["archived_at_ms"] if archives else None
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    stale = newest_ms is not None and now_ms - newest_ms > staleness_days * 86_400_000

    if not archives:
        degraded_reasons.append("no_archives: qa/archive is empty or missing")
    if not git.changed_files:
        degraded_reasons.append("no_diff: no changed files vs diff base")
    if not all_cases:
        degraded_reasons.append("no_cases: qa/cases is empty or missing")

    case_signals = [
        CaseSignal(
            case_id=c["case_id"],
            module=c["module"],
            priority=c.get("priority"),
            automation_status="automated" if c["automation_required"] else "manual",
            flaky=c["flaky"],
        )
        for c in signals
    ]

    ctx = RiskContext(
        change_id=change_id,
        generated_at=now or datetime.now(timezone.utc).isoformat(),
        requirement_summary=requirement_summary,
        aggregation_policy={
            "archive_depth": archive_depth,
            "archive_order": "archive_created_at_desc",
            "runs_per_archive": "latest_batch_only",
            "skipped_counted_in_denominator": False,
            "layers": _LAYERS,
            "xfail_treated_as": "failed",
            "xfail_rationale": _XFAIL_RATIONALE,
            "recent_fail_batch_window_k": _WINDOW_K,
            "pass_rate_fail_threshold": _PASS_RATE_FAIL_THRESHOLD,
        },
        archive_window={
            "depth": archive_depth,
            "archives_sampled": [a["archive_id"] for a in archives],
            "newest_archive": _format_date(newest_ms),
            "oldest_archive": _format_date(oldest_ms),
        },
        staleness={"max_age_days": staleness_days, "stale": stale},
        impact=ImpactBlock(
            diff_base=diff_base,
            changed_files=git.changed_files,
            modules=modules,
            affected_case_ids=affected_ids,
            affected_cases_by_module=affected_by_module,
            affected_test_files=[],
        ),
        case_signals=case_signals,
        test_health=test_health,
        historical_issues=hist_issues,
        evidence=_dedupe_evidence(evidence),
        degraded=len(degraded_reasons) > 0,
        degraded_reasons=degraded_reasons,
    )
    if git.no_git:
        ctx.no_git = True
    return ctx


def _format_date(ms: float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def _dedupe_evidence(items: list[EvidenceEntry]) -> list[EvidenceEntry]:
    by_id = {e.id: e for e in items}
    return sorted(by_id.values(), key=lambda e: e.id)


def serialize_context(ctx: RiskContext) -> str:
    return json.dumps(ctx.model_dump(exclude_none=False), indent=2, ensure_ascii=False) + "\n"


def write_risk_context(project_root: Path, change_id: str, ctx: RiskContext) -> Path:
    from assurance_agent.risk.paths import context_json_path

    out = context_json_path(project_root, change_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(serialize_context(ctx), encoding="utf-8")
    return out


def validate_context_shape(ctx: RiskContext) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if ctx.schema_version != "1.0":
        errors.append("schema_version must be 1.0")
    if not ctx.change_id:
        errors.append("change_id required")
    return (len(errors) == 0, errors)
