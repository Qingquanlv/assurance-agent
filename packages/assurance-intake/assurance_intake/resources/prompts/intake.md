# Intake prompt

Sequence explore, case design, and case review for one change.

Produce only intake artifacts. Do not run fact-baseline, plan, codegen, execution, inspect, healing, report, or archive.

Completion requires:

1. An explore advisory with no unanswered open questions.
2. Change metadata with approval fields.
3. Case delta YAML under the change cases directory.
4. A case-review result with `decision` equal to `pass`.

Stop after intake completes. Do not continue into execute scope without an explicit later graph entry.
