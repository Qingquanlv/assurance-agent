from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, Field, ValidationError

from assurance_agent.eval.types import DatasetSample, JudgeConfig, JudgeOutput
from assurance_agent.exceptions import AaError

_TIMEOUT = 120.0


class LlmRequest(BaseModel):
    model: str
    messages: list[dict[str, str]] = Field(default_factory=list)
    temperature: float = 0.0


def _parse_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise AaError("judge API returned non-object body")
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            return first["text"]
    raise AaError("judge API returned unparseable content")


def call_llm(
    request: LlmRequest,
    *,
    api_url: str,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    owns = client is None
    http = client or httpx.Client(timeout=_TIMEOUT)
    try:
        response = http.post(api_url, json=request.model_dump(), headers=headers)
    except httpx.HTTPError as err:
        raise AaError(f"judge API request failed: {err}") from err
    finally:
        if owns:
            http.close()
    if response.status_code >= 400:
        raise AaError(f"judge API returned {response.status_code}: {response.text[:200]}")
    return _parse_content(response.json())


def _read_project_ref(project_root: Path | None, ref: object, *, label: str) -> str:
    if not isinstance(ref, str) or not ref:
        return ""
    if project_root is None:
        raise AaError(f"{label} requires project_root: {ref}")
    root = project_root.resolve()
    candidate = (root / ref).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as err:
        raise AaError(f"{label} escapes project root: {ref}") from err
    if not candidate.is_file():
        raise AaError(f"{label} not found: {candidate}")
    return candidate.read_text(encoding="utf-8")


def build_judge_prompt(
    sample: DatasetSample,
    attempt_dir: Path,
    *,
    config: JudgeConfig | None = None,
    project_root: Path | None = None,
) -> str:
    prd_ref = sample.input.get("prd_ref")
    prd = _read_project_ref(project_root, prd_ref, label="judge PRD reference") if prd_ref else ""
    if not prd:
        prd = str(sample.input.get("prd", ""))
    contract = (
        _read_project_ref(project_root, config.prompt_ref, label="judge prompt reference")
        if config is not None and config.prompt_ref
        else "You are grading whether the generated QA cases cover the PRD."
    )
    atoms = sample.expected.get("atoms", [])
    produced_parts: list[str] = []
    cases_dir = attempt_dir / "raw-output" / "cases"
    if cases_dir.is_dir():
        case_files = sorted(cases_dir.rglob("*.yaml")) + sorted(cases_dir.rglob("*.yml"))
        for path in case_files:
            rel = path.relative_to(attempt_dir / "raw-output").as_posix()
            produced_parts.append(f"### {rel}\n{path.read_text(encoding='utf-8')}")
    produced = "\n\n".join(produced_parts)
    return (
        f"{contract.strip()}\n\n"
        f"## PRD\n{prd}\n\n"
        f"## Expected coverage atoms\n{json.dumps(atoms, ensure_ascii=False)}\n\n"
        f"## Produced case files\n{produced}\n\n"
        "Reply with a JSON object: "
        '{"label": "covered|partial|missing|hallucinated", "reason": str, '
        '"evidence_refs": [str], "confidence": number between 0 and 1}.'
    )


def _mock_output(sample: DatasetSample) -> JudgeOutput:
    raw = sample.mock_judge_label or "covered"
    allowed = {"covered", "partial", "missing", "hallucinated"}
    label = cast(
        Literal["covered", "partial", "missing", "hallucinated"],
        raw if raw in allowed else "covered",
    )
    return JudgeOutput(label=label, reason="mock", evidence_refs=[], confidence=1.0, needs_human_review=False)


def run_judge(
    sample: DatasetSample,
    attempt_dir: Path,
    config: JudgeConfig,
    *,
    target_model: str,
    project_root: Path | None = None,
    client: httpx.Client | None = None,
) -> JudgeOutput:
    if config.model == target_model:
        raise AaError(f"judge model must differ from target model: {config.model}")
    prompt = build_judge_prompt(sample, attempt_dir, config=config, project_root=project_root)
    if os.environ.get("AA_JUDGE_MOCK"):
        return _mock_output(sample)
    api_url = config.api_url or os.environ.get("AA_JUDGE_API_URL")
    if not api_url:
        raise AaError("judge API url not configured (AA_JUDGE_API_URL or JudgeConfig.api_url)")
    api_key = os.environ.get(config.api_key_env)
    request = LlmRequest(
        model=config.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=config.temperature,
    )
    text = call_llm(request, api_url=api_url, api_key=api_key, client=client)
    try:
        parsed = json.loads(text)
        output = JudgeOutput.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as err:
        raise AaError(f"judge output invalid: {err}") from err
    output.needs_human_review = output.confidence < config.confidence_threshold
    return output
