from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.eval.scorers import shared
from assurance_agent.eval.types import DatasetSample, SampleScore


def _load_cases(attempt_dir: Path) -> list[dict] | None:
    cases_path = attempt_dir / "raw-output" / "cases.yaml"
    if not cases_path.is_file():
        return None
    try:
        data = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    cases = data.get("cases")
    return cases if isinstance(cases, list) else None


def _load_judge(attempt_dir: Path) -> dict | None:
    judge_path = attempt_dir / "judge-result.json"
    if not judge_path.is_file():
        return None
    try:
        data = json.loads(judge_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def score(sample: DatasetSample, attempt_dir: Path) -> SampleScore:
    expected = sample.expected
    cases = _load_cases(attempt_dir)
    judge = _load_judge(attempt_dir)

    schema_valid_rate = 1.0 if cases is not None else 0.0
    hallucination_rate = 0.0
    path_coverage_rate = 1.0
    risk_coverage_rate = 1.0
    traceability_rate = 1.0
    precision = recall = f1 = 0.0

    if cases:
        forbidden = [str(x) for x in expected.get("forbidden_cases", [])]
        if forbidden:
            hallucinated = sum(
                1
                for case in cases
                if isinstance(case, dict)
                and any(f.lower() in str(case.get("title", "")).lower() for f in forbidden)
            )
            hallucination_rate = hallucinated / len(cases)

        required_paths = [str(x) for x in expected.get("required_paths", [])]
        if required_paths:
            covered: set[str] = set()
            for case in cases:
                if not isinstance(case, dict):
                    continue
                for target in case.get("automation_targets", []) or []:
                    covered.add(str(target))
            path_coverage_rate = sum(1 for p in required_paths if p in covered) / len(required_paths)

        risk_ids = [str(x) for x in expected.get("risk_ids", [])]
        if risk_ids:
            covered_risks: set[str] = set()
            for case in cases:
                if not isinstance(case, dict):
                    continue
                for risk in case.get("risk_ids", []) or []:
                    covered_risks.add(str(risk))
            risk_coverage_rate = sum(1 for r in risk_ids if r in covered_risks) / len(risk_ids)

        with_trace = sum(
            1
            for case in cases
            if isinstance(case, dict) and str(case.get("traceability", "")).strip()
        )
        traceability_rate = with_trace / len(cases) if cases else 0.0
    elif expected.get("risk_ids"):
        risk_coverage_rate = 0.0

    human_label = expected.get("human_label")
    if human_label and judge and not judge.get("needs_human_review"):
        is_human_positive = human_label == "covered"
        is_judge_positive = judge.get("label") == "covered"
        tp = fp = fn = 0
        if is_human_positive and is_judge_positive:
            tp = 1
        elif not is_human_positive and is_judge_positive:
            fp = 1
        elif is_human_positive and not is_judge_positive:
            fn = 1
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    return SampleScore(
        sample_id=sample.id,
        status="ok",
        metrics={
            "evidence_integrity": shared.score_evidence_integrity(attempt_dir),
            "schema_valid_rate": schema_valid_rate,
            "hallucination_rate": hallucination_rate,
            "path_coverage_rate": path_coverage_rate,
            "risk_coverage_rate": risk_coverage_rate,
            "traceability_rate": traceability_rate,
            "requirement_precision": precision,
            "requirement_recall": recall,
            "requirement_f1": f1,
        },
    )
