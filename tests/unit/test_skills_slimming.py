"""C4 试点：两个 skill 收敛为五节 manifest，且被删禁令已有运行时归宿。"""

import re

from assurance_agent import resources

PILOT = ("aa-api-plan", "aa-api-plan-reviewer")
SECTIONS = ("## Purpose", "## Inputs", "## Outputs", "## Boundaries", "## Domain Notes")


def test_pilot_skills_use_exactly_the_five_section_manifest_structure() -> None:
    for name in PILOT:
        text = resources.read_text("skills", name, "SKILL.md")
        headings = tuple(line for line in text.splitlines() if line.startswith("## "))
        assert headings == SECTIONS, name


def test_pilot_skills_drop_procedural_step_numbering() -> None:
    step = re.compile(r"^#{2,4}\s*(Step\s*\d|第\s*\d+\s*步)", re.MULTILINE)
    for name in PILOT:
        assert step.search(resources.read_text("skills", name, "SKILL.md")) is None, name


def test_reviewer_shrank_below_the_baseline() -> None:
    """reviewer 的篇幅主体是规则引擎散文，规则下沉后应大幅缩水。"""
    after = len(resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md").splitlines())
    assert after < 718 * 0.6, f"{after} lines"


def test_api_plan_shrank_only_modestly_because_format_contracts_stay() -> None:
    """Markdown 产物未注册，contract render 覆盖不到，格式契约必须留在 skill 里。"""
    after = len(resources.read_text("skills", "aa-api-plan", "SKILL.md").splitlines())
    assert after < 505, f"{after} lines"


def test_mechanised_prohibitions_are_gone_from_the_reviewer_prose() -> None:
    """这些规则已由 plan checks 和 capabilities gate 强制。"""
    text = resources.read_text("skills", "aa-api-plan-reviewer", "SKILL.md")
    assert "alternate hidden-directory path" not in text
    assert "never narrow, drop, or reinterpret" not in text


def test_api_plan_retains_the_markdown_format_contract_the_checks_parse() -> None:
    """shared_factory / assert_ideal check 直接解析这些表，列名是运行时接口。"""
    text = resources.read_text("skills", "aa-api-plan", "SKILL.md")
    outputs = text[text.index("## Outputs") : text.index("## Boundaries")]
    for column in ("Case ID", "Shared Module", "Function", "Ownership"):
        assert column in outputs, column
    assert "test_<case_id_lowercase>__<desc>" in outputs


def test_api_plan_case_id_column_requires_full_ids() -> None:
    """assert_ideal 零匹配判定按 Case ID 列精确相等，短号表不能替代全 ID。"""
    text = resources.read_text("skills", "aa-api-plan", "SKILL.md")
    assert "full case_id" in text.lower()


def test_domain_notes_retain_factory_adapter_and_live_server_knowledge() -> None:
    text = resources.read_text("skills", "aa-api-plan", "SKILL.md")
    notes = text[text.index("## Domain Notes") :]
    assert "factory" in notes.lower()
    assert "adapter" in notes.lower()
    assert "live-server" in notes.lower()
