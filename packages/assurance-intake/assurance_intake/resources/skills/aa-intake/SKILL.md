# Intake

Capability-owned intake skill. Do not select a provider, model, or adapter.
Do not look up a global skill catalog.

`aa-intake` sequences design-time clarification:

```text
explore -> case-design -> case-review -> case-fix loop
```

It does not run fact-baseline, plan, codegen, execution, inspect, healing, report, or archive.

## Scope

Use the explore, case-design, and case-review skills for their business contracts.
This skill only sequences them and states completion.

## Intake runbook

1. Explore: ask or resolve per-pitfall questions according to the locked interaction mode. Write `explore/advisory.json`. Open questions answered without user confirmation fail interactive intake.
2. Case design: clarify scope in interactive mode and require explicit approval before writing files. Write `.qa.yaml` (including approval), `proposal.md`, and `cases/<module>/case.yaml`. Case design is not complete without `.qa.yaml.approval`.
3. Case review: write `review/case-review.json`. That JSON is the release gate.
4. Case-fix loop: if the gate needs_fix, repeat case design and case review up to the locked attempt limit. reject or human_review_required stops for a human decision.

## Human review

If the case-review gate returns `needs_human_review` or `reject`, stop and ask the user. Never edit `review/case-review.json` to `decision: pass`.

## Completion

Intake is complete only when:

1. `explore/advisory.json` exists and has no unanswered open questions;
2. `.qa.yaml` contains approval metadata (`approved_by`, `approved_approach`, `approved_at`);
3. the change cases directory contains generated case delta YAML;
4. `review/case-review.json` exists with `decision` equal to `pass`.

When complete, stop. Do not automatically continue into execute scope.
