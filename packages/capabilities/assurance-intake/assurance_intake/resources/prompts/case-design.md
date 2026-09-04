# Case-design prompt

Author the change metadata, proposal, and semantic case delta for one change.

Read the relevant product source directly before choosing cases. Explore evidence is context, not a substitute.
Consume the graph-provided `exploration` object when it is present. Do not infer
Explore state from `.qa.yaml`.

Write:

- `.qa.yaml` with approval metadata
- `proposal.md` with product-source verification
- every exact path in graph-provided `case_delta_paths`, each with at least one
  added or modified case

Every authored trace key must be an exact declared typed leaf. Prefix matches are invalid.

Do not invoke plan, codegen, or archive work from this skill.
Do not create data-knowledge proposal files; record unavailable closed keys in
`proposal.md` Data Needs and keep their MRC rows as precisely explained
`skipped_by_scope` rows.
