"""Resident content checks for migrated skills + opencode assets (spec 8/9/4a)."""
import re
from pathlib import Path

from assurance_agent import resources
from assurance_agent.artifacts.models import (
    Advisory,
    ApplySummary,
    CaseYaml,
    ExecutionManifest,
    FactBaselineFull,
    FactBaselineUnavailable,
    FailureAnalysis,
    FixProposal,
    QaYaml,
    QualityGateResult,
    QualityReport,
    Review,
    SafetyCheck,
    WorkflowState,
)
from pydantic import BaseModel

RESIDUE_RE = re.compile(r"(?i)(?<![a-z])aws")
AWS_RESIDUE_ALLOWLIST: set[str] = set()
AA_REF_RE = re.compile(r"\b(aa-[a-z0-9-]+|writing-skills)\b")
AA_REF_ALLOWLIST = {"aa-full", "aa-api", "aa-e2e"}  # schema / memory path prefixes, not skills
FILE_SUFFIXES = {"json", "ts", "yaml", "yml", "md", "schema", "py"}
ALLOWLIST_FILE = Path(__file__).resolve().parents[1] / "data" / "skill_field_allowlist.txt"


def _walk_resource_files(*rel: str):
    """Yield (relpath_tuple) for every file under _resources/<rel> (recursive)."""
    for name in resources.iter_children(*rel):
        child = (*rel, name)
        try:
            resources.iter_children(*child)  # dir -> recurse
        except (NotADirectoryError, ValueError):
            yield child
        else:
            yield from _walk_resource_files(*child)


def _all_text_files(*rel: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for parts in _walk_resource_files(*rel):
        name = parts[-1]
        if name.endswith((".md", ".sh", ".mjs", ".ts", ".html", ".txt", ".yaml", ".yml", ".json", ".dot")):
            out["/".join(parts)] = resources.read_text(*parts)
    return out


def _skill_names() -> set[str]:
    return set(resources.iter_children("skills"))


def _agent_names() -> set[str]:
    return {n[:-3] for n in resources.iter_children("opencode", "agents") if n.endswith(".md")}


def _model_fields(*models: type[BaseModel]) -> set[str]:
    out: set[str] = set()
    for model in models:
        for field_name, field in model.model_fields.items():
            out.add(field_name)
            if field.alias:
                out.add(field.alias)
    return out


ARTIFACT_ROOTS: dict[str, set[str]] = {
    "review": _model_fields(Review),
    "fix_proposal": _model_fields(FixProposal),
    "failure_analysis": _model_fields(FailureAnalysis),
    "execution_manifest": _model_fields(ExecutionManifest),
    "quality_gate_result": _model_fields(QualityGateResult),
    "quality_report": _model_fields(QualityReport),
    "advisory": _model_fields(Advisory),
    "fact_baseline": _model_fields(FactBaselineFull, FactBaselineUnavailable),
    "workflow_state": _model_fields(WorkflowState),
    "safety_check": _model_fields(SafetyCheck),
    "apply_summary": _model_fields(ApplySummary),
    "case_yaml": _model_fields(CaseYaml),
    "qa_yaml": _model_fields(QaYaml),
}


def _load_field_allowlist() -> set[str]:
    entries: set[str] = set()
    for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def test_thirty_three_skills_present() -> None:
    names = _skill_names()
    assert len(names) == 33
    assert "writing-skills" in names
    assert "aa-workflow" in names
    assert "aa-dashboard" in names
    assert not any(n.startswith("aws-") for n in names)


def test_no_aws_residue_in_skills_and_opencode() -> None:
    offenders: list[str] = []
    files = {**_all_text_files("skills"), **_all_text_files("opencode")}
    for relpath, text in files.items():
        for match in RESIDUE_RE.finditer(text):
            token = text[match.start() : match.start() + 3]
            if token not in AWS_RESIDUE_ALLOWLIST:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{relpath}:{line}:{token}")
    assert offenders == [], f"aws residue found: {offenders[:20]}"


def test_cross_skill_references_resolve() -> None:
    valid = _skill_names() | _agent_names() | AA_REF_ALLOWLIST
    offenders: list[str] = []
    files = {**_all_text_files("skills"), **_all_text_files("opencode", "agents")}
    for relpath, text in files.items():
        for match in AA_REF_RE.finditer(text):
            token = match.group(1).rstrip(".")
            if token.endswith(".md"):
                token = token[:-3]
            if token not in valid:
                offenders.append(f"{relpath}: {token}")
    assert offenders == [], f"dangling aa-* references: {sorted(set(offenders))[:20]}"


def test_skill_field_references_subset_of_models() -> None:
    allowlist = _load_field_allowlist()
    offenders: list[str] = []
    for relpath, text in _all_text_files("skills").items():
        for root, fields in ARTIFACT_ROOTS.items():
            for match in re.finditer(rf"(?<![\w.]){root}\.([a-z_][a-z0-9_]*)", text):
                first = match.group(1)
                if first in FILE_SUFFIXES:
                    continue
                if first in fields:
                    continue
                if f"{root}.{first}" in allowlist:
                    continue
                offenders.append(f"{relpath}: {root}.{first}")
    assert offenders == [], f"skill fields not in M2 models: {sorted(set(offenders))[:20]}"


def test_agents_preserve_permission_floor() -> None:
    """No runtime agent grants an allow-rule for aa gate/status or workflow-state writes (spec 9).

    Agents legitimately MENTION `aa gate check` / workflow-state.yaml in prose (the
    prohibition text), so we assert on permission *allow-rules* only, not substrings.
    """
    allow_gate = re.compile(r'"[^"]*aa (gate|status)[^"]*"\s*:\s*allow')
    allow_state = re.compile(r'"[^"]*workflow-state\.yaml"\s*:\s*allow')
    agents = [n for n in resources.iter_children("opencode", "agents") if n.endswith(".md")]
    assert len(agents) == 6
    for name in agents:
        text = resources.read_text("opencode", "agents", name)
        assert allow_gate.search(text) is None, name
        assert allow_state.search(text) is None, name
