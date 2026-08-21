# Case-design prompt

Author the change metadata, proposal, and semantic case delta for one change.

Read the relevant product source directly before choosing cases. Explore evidence is context, not a substitute.

Write:

- `.qa.yaml` with approval metadata
- `proposal.md` with product-source verification
- `cases/<module>/case.yaml` with at least one added or modified case

Every authored trace key must be an exact declared typed leaf. Prefix matches are invalid.

Do not invoke plan, codegen, or archive work from this skill.
