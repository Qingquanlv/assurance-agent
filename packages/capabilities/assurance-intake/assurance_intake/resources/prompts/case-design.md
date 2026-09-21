# Case-design prompt

Author the change metadata, proposal, and semantic case delta for one change.

Read the relevant product source directly before choosing cases. Explore evidence is context, not a substitute.
Consume the graph-provided `exploration` object when it is present. Do not infer
Explore state from `.qa.yaml`.

Write:

- `.qa.yaml` with approval metadata
- `proposal.md` with product-source verification
- every exact path in request `case_delta_paths` (inferred from Explore
  `impact_inventory`; one requirement may lock several modules), each with at
  least one added or modified case

Every authored trace key must be an exact declared typed leaf. Prefix matches are invalid.

Do not invoke plan, codegen, or archive work from this skill.
Do not create data-knowledge proposal files; record unavailable closed keys in
`proposal.md` Data Needs and keep their MRC rows as precisely explained
`skipped_by_scope` rows with `key: null`, the frozen `mrc_id`, and a
`capability_unresolved` skip reason. Continue designing cases for resolved rows.

Product source is verification evidence, not a frozen business oracle. Before
writing outputs, classify every MRC row in one complete pass. For every
closed-category key missing from DataKnowledge, keep its frozen `mrc_id` as a
`key: null` / `skipped_by_scope` row even when the expected behavior is known.
Do not claim that row is covered by an unrelated capability leaf. Narrow every
case and proposal assertion that depends on an unconfirmed oracle. Do not defer
another observable closed-key decision to a later review round.
