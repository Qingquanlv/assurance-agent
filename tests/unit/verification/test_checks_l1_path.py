""".aa/ is the only valid L1 path (spec C2)."""

from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.l1_path import check_l1_path


def _ctx(plan: str) -> CheckContext:
    return CheckContext(plan_texts={"plans/api-plan.md": plan}, cases=(), data_knowledge={})


def test_canonical_l1_path_passes() -> None:
    result = check_l1_path(_ctx("Data knowledge: `.aa/data-knowledge.yaml`\n"))
    assert result.status == "pass"
    assert result.findings == ()


def test_alternate_hidden_directory_path_fails_with_locator() -> None:
    result = check_l1_path(_ctx("line one\nread `qa/.knowledge/data-knowledge.yaml`\n"))
    assert result.status == "fail"
    assert len(result.findings) == 1
    assert result.findings[0].locator == "plans/api-plan.md:2"
    assert result.findings[0].expected == ".aa/data-knowledge.yaml"


def test_bare_filename_mention_is_not_flagged() -> None:
    assert check_l1_path(_ctx("promote leaves into data-knowledge.yaml\n")).status == "pass"


def test_l2_proposal_filename_is_not_flagged() -> None:
    plan = "write `plans/data-knowledge.proposal.api.yaml`\n"
    assert check_l1_path(_ctx(plan)).status == "pass"


def test_refs_list_every_scanned_plan() -> None:
    ctx = CheckContext(
        plan_texts={"plans/api-plan.md": "", "plans/api-codegen-plan.md": ""},
        cases=(),
        data_knowledge={},
    )
    assert check_l1_path(ctx).refs == ("plans/api-codegen-plan.md", "plans/api-plan.md")
