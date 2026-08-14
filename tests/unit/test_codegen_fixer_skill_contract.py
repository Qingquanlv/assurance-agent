from assurance_agent import resources


def test_codegen_fixer_skills_disambiguate_reason_by_outcome() -> None:
    for name in ("aa-api-codegen-fixer", "aa-e2e-codegen-fixer"):
        text = resources.read_text("skills", name, "SKILL.md")

        assert 'For `outcome: "applied"`' in text
        assert "set `reason` to JSON `null`" in text
        assert 'For `outcome: "no_op"` or `"skipped"`' in text
        assert "leave\n  `claimed_modified_paths` empty" in text
