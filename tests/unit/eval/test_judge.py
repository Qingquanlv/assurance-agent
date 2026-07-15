from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from assurance_agent.eval.judge import LlmRequest, build_judge_prompt, call_llm, run_judge
from assurance_agent.eval.types import DatasetSample, JudgeConfig
from assurance_agent.exceptions import AaError


def test_call_llm_posts_expected_payload_and_parses_list_content() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hello"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    req = LlmRequest(model="judge-x", messages=[{"role": "user", "content": "hi"}], temperature=0.0)
    out = call_llm(req, api_url="https://api.test/v1/messages", api_key="secret", client=client)
    assert out == "hello"
    assert captured["url"] == "https://api.test/v1/messages"
    assert captured["auth"] == "Bearer secret"
    assert captured["body"] == {
        "model": "judge-x",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.0,
    }


def test_call_llm_parses_plain_string_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": "plain-text"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    req = LlmRequest(model="judge-x", messages=[{"role": "user", "content": "hi"}])
    assert call_llm(req, api_url="https://api.test", client=client) == "plain-text"


def test_call_llm_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    req = LlmRequest(model="judge-x", messages=[])
    with pytest.raises(AaError, match="judge API"):
        call_llm(req, api_url="https://api.test", client=client)


def test_build_judge_prompt_includes_prd_and_expected(tmp_path: Path) -> None:
    sample = DatasetSample(id="J-1", suite="case-generation",
                           input={"prd": "Build users CRUD"},
                           expected={"atoms": ["list", "create"]})
    prompt = build_judge_prompt(sample, tmp_path)
    assert "Build users CRUD" in prompt
    assert "list" in prompt and "create" in prompt


def test_run_judge_mock_mode_uses_sample_label(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AA_JUDGE_MOCK", "1")
    sample = DatasetSample(id="J-1", suite="case-generation", input={}, expected={},
                           mock_judge_label="covered")
    cfg = JudgeConfig(model="judge-x")
    out = run_judge(sample, tmp_path, cfg, target_model="target-y")
    assert out.label == "covered"
    assert out.needs_human_review is False


def test_run_judge_fail_closed_when_model_equals_target(tmp_path: Path) -> None:
    sample = DatasetSample(id="J-1", suite="case-generation", input={}, expected={})
    cfg = JudgeConfig(model="same-model")
    with pytest.raises(AaError, match="must differ"):
        run_judge(sample, tmp_path, cfg, target_model="same-model")


def test_run_judge_parses_real_response_and_flags_low_confidence(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"label": "partial", "reason": "half", "evidence_refs": ["cases/x"],
                   "confidence": 0.4}
        return httpx.Response(200, json={"content": [{"type": "text",
                                                      "text": json.dumps(payload)}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sample = DatasetSample(id="J-1", suite="case-generation",
                           input={"prd": "x"}, expected={"atoms": ["a"]})
    cfg = JudgeConfig(model="judge-x", api_url="https://api.test", confidence_threshold=0.6)
    out = run_judge(sample, tmp_path, cfg, target_model="target-y", client=client)
    assert out.label == "partial"
    assert out.confidence == pytest.approx(0.4)
    assert out.needs_human_review is True   # 0.4 < 0.6
