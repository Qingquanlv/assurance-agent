# Phase 5 handoff — Phase 4 capability extraction

Phase 4 extracted six vertical Assurance wheels. This file is the exact contract for Phase 5 product assembly. It is not a cutover. `aa` still defaults to the legacy product. No production Assurance `ProductManifest` or full graph exists.

## Engine contract

- `ENGINE_API_VERSION` is `"2.0"`.
- Prepare `binding_data` is exactly `{execution, request_policy_digest, request_config_digest}`.
- No fallback, candidate model list, or ambient default.
- Capability wheels declare `bindings == ()`. Phase 5 owns adapter bindings.

## Six wheels

| Wheel | Filename | Version | Entry point | Source / declaration | Python / plugin deps |
|---|---|---|---|---|---|
| `assurance.intake` / `assurance-intake` | `assurance_intake-0.1.0-py3-none-any.whl` | `0.1.0` | `graph_engine.plugins` `intake` = `assurance_intake.plugin:IntakePlugin` | `packages/assurance-intake/` · `assurance_intake/plugin-declaration.json` | graph-engine, agent-runtime-contracts, pydantic>=2.7, pyyaml>=6.0 (no Assurance wheel deps) |
| `assurance.generation` / `assurance-generation` | `assurance_generation-0.1.0-py3-none-any.whl` | `0.1.0` | `graph_engine.plugins` `generation` = `assurance_generation.plugin:GenerationPlugin` | `packages/assurance-generation/` · `assurance_generation/plugin-declaration.json` | assurance-intake==0.1.0, graph-engine, agent-runtime-contracts, pydantic>=2.7 |
| `assurance.execution` / `assurance-execution` | `assurance_execution-0.1.0-py3-none-any.whl` | `0.1.0` | `graph_engine.plugins` `execution` = `assurance_execution.plugin:ExecutionPlugin` | `packages/assurance-execution/` · `assurance_execution/plugin-declaration.json` | assurance-intake==0.1.0, assurance-generation==0.1.0, graph-engine, agent-runtime-contracts, pydantic>=2.7 |
| `assurance.healing` / `assurance-healing` | `assurance_healing-0.1.0-py3-none-any.whl` | `0.1.0` | `graph_engine.plugins` `healing` = `assurance_healing.plugin:HealingPlugin` | `packages/assurance-healing/` · `assurance_healing/plugin-declaration.json` | assurance-intake==0.1.0, assurance-generation==0.1.0, assurance-execution==0.1.0, graph-engine, agent-runtime-contracts, pydantic>=2.7 |
| `assurance.quality` / `assurance-quality` | `assurance_quality-0.1.0-py3-none-any.whl` | `0.1.0` | `graph_engine.plugins` `quality` = `assurance_quality.plugin:QualityPlugin` | `packages/assurance-quality/` · `assurance_quality/plugin-declaration.json` | assurance-intake==0.1.0, assurance-generation==0.1.0, assurance-execution==0.1.0, assurance-healing==0.1.0, graph-engine, agent-runtime-contracts, pydantic>=2.7 |
| `assurance.improvement` / `assurance-improvement` | `assurance_improvement-0.1.0-py3-none-any.whl` | `0.1.0` | `graph_engine.plugins` `improvement` = `assurance_improvement.plugin:ImprovementPlugin` | `packages/assurance-improvement/` · `assurance_improvement/plugin-declaration.json` | assurance-intake==0.1.0, assurance-generation==0.1.0, assurance-execution==0.1.0, assurance-healing==0.1.0, assurance-quality==0.1.0, graph-engine, agent-runtime-contracts, pydantic>=2.7 |

Descriptor plugin dependencies (acyclic, `==0.1.0`):

- `assurance.intake` → (none)
- `assurance.generation` → `assurance.intake==0.1.0`
- `assurance.execution` → `assurance.intake==0.1.0`, `assurance.generation==0.1.0`
- `assurance.healing` → `assurance.intake==0.1.0`, `assurance.generation==0.1.0`, `assurance.execution==0.1.0`
- `assurance.quality` → `assurance.intake==0.1.0`, `assurance.generation==0.1.0`, `assurance.execution==0.1.0`, `assurance.healing==0.1.0`
- `assurance.improvement` → `assurance.intake==0.1.0`, `assurance.generation==0.1.0`, `assurance.execution==0.1.0`, `assurance.healing==0.1.0`, `assurance.quality==0.1.0`

## Public IDs (live `descriptor()` / `contribute()`)

Production logical binding IDs: **none**. Every wheel contributes `bindings == ()`.

### `assurance.intake`

Handlers:
- `assurance.intake.case-design.finalize`
- `assurance.intake.case-design.prepare`
- `assurance.intake.case-review.finalize`
- `assurance.intake.case-review.prepare`
- `assurance.intake.explore.finalize`
- `assurance.intake.explore.prepare`
- `assurance.intake.intake.finalize`
- `assurance.intake.intake.prepare`

Validators:
- `assurance.intake.validator.case-candidate.v1`
- `assurance.intake.validator.case-references.v1`

Schemas (SHA-256 of contributed schema bytes):
- `assurance.intake.schema.case-authoring.v1` — `e55a271a27c6697255a01854ae9d0cebea9b0a0d69514888ca71a3459512abe9`
- `assurance.intake.schema.case-review.v1` — `8d261dddb8da0b3795256387d7f9ff3d1287fb913c1a0059064c80e36a263678`
- `assurance.intake.schema.case.v1` — `e193309535656b4171a3ce0bf7be6995a20497c76fac81be0aa159f967fcff02`
- `assurance.intake.schema.qa-change.v1` — `b8ef66b4d8e9a81850e28e47524d4aac5c6095b0270162a6754568b738c03bb8`

Resources (SHA-256 of contributed resource bytes):
- `assurance.intake.persona.doc-author.v1` — `799b15e121241b8384ea9fed7f2f417df941a08ca1b35652e58fa97b5552f2e9`
- `assurance.intake.persona.explorer.v1` — `211248d0d15f2c9978fce17cfae8eac27ad48b440f8d7bede926edc8855d3be2`
- `assurance.intake.persona.intake-host.v1` — `adf713857d7b302b2dca8eb950a14f0e0718cc16df909649484ea84944c8edbb`
- `assurance.intake.persona.reviewer.v1` — `83918002a094044bbabdf2f1a7289fdd3336aa7cd5b7617794d6165bd8b5d9ad`
- `assurance.intake.prompt.case-design.v1` — `03d6e1fcaa9b1dbd37316f3d7971e71cf1d0f33e7cc7dc3b6ed5d125a819e335`
- `assurance.intake.prompt.case-review.v1` — `fca808ad225516b704e2696512199188bc127e64896ef3630d94f7a32233ba54`
- `assurance.intake.prompt.explore.v1` — `5c32cf9911994c9cb055c6b728579f8367c7e8ed4eb57f33d712024c7243c371`
- `assurance.intake.prompt.intake.v1` — `bb997362cd1906b855920eeb67c3ecf978ca315bfb4f5bcf22571b74bc2692b9`
- `assurance.intake.result.case-design.v1` — `e55a271a27c6697255a01854ae9d0cebea9b0a0d69514888ca71a3459512abe9`
- `assurance.intake.result.case-review.v1` — `8d261dddb8da0b3795256387d7f9ff3d1287fb913c1a0059064c80e36a263678`
- `assurance.intake.result.explore.v1` — `51390f20dfa885b472ed9bac57c37d92908afa9000301dd27a80df93e444c91f`
- `assurance.intake.result.intake.v1` — `51390f20dfa885b472ed9bac57c37d92908afa9000301dd27a80df93e444c91f`
- `assurance.intake.skill.aa-case-design.case-delta-reviewer-prompt.v1` — `91ace65b4314b22fbe56cc089e1a2287be0632c7ef126bcce8ea22a576acdbe0`
- `assurance.intake.skill.aa-case-design.v1` — `c5ec1320c59aa2435f6ce8d302b120ab2dbbb5902636f8314eae1cb9d7d2273a`
- `assurance.intake.skill.aa-case-design.visual-companion.v1` — `282c8aa425174ce5e4a001bce0c1bde35d641af0c58ea0c8106e710d64356894`
- `assurance.intake.skill.aa-case-reviewer.v1` — `3d8fb2d2366ee30ade8b6dda99f172c5bedcba9687d2395caef7b0183d95ddcb`
- `assurance.intake.skill.aa-explore.v1` — `5b417f6a05540bbb4388b2558f150b70acd07d68933d119dbf4ef8d1d2f30572`
- `assurance.intake.skill.aa-intake.v1` — `8e83108508465225083c5f165172fa9efee2eb7be95be30745a28655d2ccd69e`

Effects: none.

### `assurance.generation`

Handlers:
- `assurance.generation.api.codegen-fix.finalize`
- `assurance.generation.api.codegen-fix.prepare`
- `assurance.generation.api.codegen.finalize`
- `assurance.generation.api.codegen.prepare`
- `assurance.generation.api.plan-review.finalize`
- `assurance.generation.api.plan-review.prepare`
- `assurance.generation.api.plan.finalize`
- `assurance.generation.api.plan.prepare`
- `assurance.generation.e2e.codegen-fix.finalize`
- `assurance.generation.e2e.codegen-fix.prepare`
- `assurance.generation.e2e.codegen.finalize`
- `assurance.generation.e2e.codegen.prepare`
- `assurance.generation.e2e.plan-review.finalize`
- `assurance.generation.e2e.plan-review.prepare`
- `assurance.generation.e2e.plan.finalize`
- `assurance.generation.e2e.plan.prepare`
- `assurance.generation.fuzz.codegen.finalize`
- `assurance.generation.fuzz.codegen.prepare`
- `assurance.generation.fuzz.plan-review.finalize`
- `assurance.generation.fuzz.plan-review.prepare`
- `assurance.generation.fuzz.plan.finalize`
- `assurance.generation.fuzz.plan.prepare`
- `assurance.generation.performance.codegen.finalize`
- `assurance.generation.performance.codegen.prepare`
- `assurance.generation.performance.plan-review.finalize`
- `assurance.generation.performance.plan-review.prepare`
- `assurance.generation.performance.plan.finalize`
- `assurance.generation.performance.plan.prepare`

Validators:
- `assurance.generation.validator.api-plan.v1`
- `assurance.generation.validator.codegen-fix-candidate.v1`
- `assurance.generation.validator.codegen-mapping.v1`
- `assurance.generation.validator.e2e-plan.v1`
- `assurance.generation.validator.fuzz-plan.v1`
- `assurance.generation.validator.generated-files.v1`
- `assurance.generation.validator.performance-plan.v1`
- `assurance.generation.validator.plan-mechanical.v1`

Schemas (SHA-256 of contributed schema bytes):
- `assurance.generation.schema.codegen-mapping.v1` — `9d623c3f691070097c9d3f120877db09596ef1dbdd060814e82d16e9876a44d1`
- `assurance.generation.schema.discovery-campaign.v1` — `2a7c580ae79697388b2e169c1c3c96d5200f23fbeec7080a24f53b8c500e1e31`
- `assurance.generation.schema.generated-files.v1` — `01243f769b92aff0b396bf068ce606008565945e81593bb9940240bd7d497757`
- `assurance.generation.schema.plan-check.v1` — `07e34ca9d819ec2679bbdc78906bdf298f364999ccf37c86870b88047ef548cd`
- `assurance.generation.schema.plan-review.v1` — `8ce89f2fd86034117786c0c3c0110ccd0736595fd2047e743502e9f874889de4`

Resources (SHA-256 of contributed resource bytes):
- `assurance.generation.persona.reviewer.v1` — `69680ef67e729a614ce9101b3cfb20d41a07fd8c0ce3f6f09a1dd4b71bd41313`
- `assurance.generation.persona.test-author.v1` — `697c7e7003d9184dae43f617dc80cb0e20a50a4a1c00009701f1e2467e4c89b3`
- `assurance.generation.result.codegen-fix.v1` — `fd07493fe181eefe05e27c45c06d25b686ac62abfbd58020b0cb3305f8c21350`
- `assurance.generation.result.codegen.v1` — `ce96d47a95d8b49905cf387333b6bfe52b21ded1bc9727e69111b2be7a3c64a6`
- `assurance.generation.result.plan-review.v1` — `8ce89f2fd86034117786c0c3c0110ccd0736595fd2047e743502e9f874889de4`
- `assurance.generation.result.plan.v1` — `d85b705f93450201a3dd02a2f8305003f4ca94a1ca93a81c2cac9cc846e7bcf1`
- `assurance.generation.skill.aa-api-codegen-fixer.v1` — `1066febc536e7b447c9e363e501cec811b4f7f53289e77e9d59471864d13201a`
- `assurance.generation.skill.aa-api-codegen.v1` — `6fbb90a6addd098eeaf1bfa0d26fdb4e4c0647af8eb01b8b294c2d2051fa38cf`
- `assurance.generation.skill.aa-api-plan-reviewer.v1` — `ce458924bfe9bc7531dd678c45a329b21fa85ddd2966829cf3fc4f22424fa96d`
- `assurance.generation.skill.aa-api-plan.v1` — `323ae5064cf1f9e7345ee36317eba55be2517a954608b06a9937afa2cfe41e80`
- `assurance.generation.skill.aa-e2e-codegen-fixer.v1` — `cc3155cef9c03257b3b9491c8d8305afc3b41b6deb411e35ec9accbe5b570545`
- `assurance.generation.skill.aa-e2e-codegen.v1` — `972d51027d4ea64a3011367653f2bffa336272fefeb47747e401950d1da6ccb9`
- `assurance.generation.skill.aa-e2e-plan-reviewer.v1` — `815f7874331ec9f6a4d3f8c591856972868cce1e918f38ae219fdb5129eadfed`
- `assurance.generation.skill.aa-e2e-plan.v1` — `248a329c6a5108370d5ea2535be06fa5fa1d92214b877a6f942e11221c04b007`
- `assurance.generation.skill.aa-fuzz-codegen.v1` — `20c58af8a891baad5f5295fb48241e8aba70e06266f274b56052759686bd6e0d`
- `assurance.generation.skill.aa-fuzz-plan-reviewer.v1` — `f5932e6a4dc6c1bc432424871d44fb0724c6fcc0374b2f7d05e9b11a07321571`
- `assurance.generation.skill.aa-fuzz-plan.v1` — `9080d55904f37adf98b3f167de4d8e4be2fabb2d696100d1fb57b14839c7b918`
- `assurance.generation.skill.aa-performance-codegen.v1` — `c46096e3b573515cf20518658b9c5f911a159c9bd262e99ed9c469bf985b26f7`
- `assurance.generation.skill.aa-performance-plan-reviewer.v1` — `26f4344a59c0381000115651689ceff2b59a82a528184f2092ae8e6e71d30804`
- `assurance.generation.skill.aa-performance-plan.v1` — `cba0c194a75e6bc9072cc0ddeaf4a0c6fc99adc85c5db024f06706efe3d5674e`

Effects: none.

### `assurance.execution`

Handlers:
- `assurance.execution.execute.finalize`
- `assurance.execution.execute.prepare`
- `assurance.execution.normalize`
- `assurance.execution.run-tests`
- `assurance.execution.run-tests-and-collect-pr-metrics`
- `assurance.execution.run.finalize`
- `assurance.execution.run.prepare`
- `assurance.execution.select`

Validators:
- `assurance.execution.validator.closed-mapping.v1`
- `assurance.execution.validator.evidence.v1`

Schemas (SHA-256 of contributed schema bytes):
- `assurance.execution.schema.closed-mapping.v1` — `c1fdcd451d93e49897a8f4b7ba68f1415daa32febae0972c97ab40e88755788b`
- `assurance.execution.schema.execution-evidence.v1` — `c072c8f5f120b9311c80e4dc4dfae3dad00ab53bb9afb1b4f4e0b9fd80bfced3`
- `assurance.execution.schema.execution-manifest.v1` — `0023fbeabe2d18b0c744c2f2b8e400471e3b395f16f8dfdd94aee19d6deda06a`
- `assurance.execution.schema.selected-targets.v1` — `170c5d3741bc6a36ab621d1330b5bc635cd0325d8657e59c3bfdc2ec35c9e12a`

Resources (SHA-256 of contributed resource bytes):
- `assurance.execution.persona.executor.v1` — `6c946ede1ff8cca4aae577fc322fb8910b29c079113b1b266bd4065470b8bf51`
- `assurance.execution.result.execution.v1` — `c072c8f5f120b9311c80e4dc4dfae3dad00ab53bb9afb1b4f4e0b9fd80bfced3`
- `assurance.execution.skill.aa-execute.v1` — `55759e44197af1c74a6be2cc838bc32f72a7614e3fddcb777c54c67b1b801c94`
- `assurance.execution.skill.aa-run.v1` — `c3d8b6895bb3f19aee5b5a790cc311a6d9230670bc01a58b63eba45edd59ee54`

Effects: none.

### `assurance.healing`

Handlers:
- `assurance.healing.allocate-coverage-repair-attempt`
- `assurance.healing.allocate-healing-attempt`
- `assurance.healing.combine-fixer-safety`
- `assurance.healing.compute-coverage-repair-safety`
- `assurance.healing.coverage-repair.finalize`
- `assurance.healing.coverage-repair.prepare`
- `assurance.healing.fix-proposal.finalize`
- `assurance.healing.fix-proposal.prepare`
- `assurance.healing.fixer-authority-ready`
- `assurance.healing.fixer-dispatch`
- `assurance.healing.project-episode`
- `assurance.healing.record-codegen-fix-apply`
- `assurance.healing.record-coverage-repair-status`
- `assurance.healing.record-fixer-approval`
- `assurance.healing.record-healing-status`

Validators:
- `assurance.healing.validator.override.v1`
- `assurance.healing.validator.repair-candidate.v1`
- `assurance.healing.validator.test-tree.v1`

Schemas (SHA-256 of contributed schema bytes):
- `assurance.healing.schema.allocation-intent.v2` — `dbd980c555b0bafa38e59c565f3da7eac505ae0b98bd8cfb8d8c00a5002aef84`
- `assurance.healing.schema.allocation-receipt.v2` — `ae557d2183dff15c740fd2262dae40521e97f09736c3172a2ca9392be975d8d4`
- `assurance.healing.schema.coverage-repair.v1` — `51bdecc0456894314c5cae7c3d508481cdb7b43834ca1c761c3e759bcc1aa422`
- `assurance.healing.schema.fix-proposal.v1` — `45baef416a046697f7c9f7634c16f42a2fd765429a44be8e816119d7567020e4`
- `assurance.healing.schema.heal-apply-intent.v2` — `1addb595efda8616f8e9fbaea045677abb9f733ad7bdc7d292dc6a719acad047`
- `assurance.healing.schema.heal-apply-receipt.v2` — `3cb28116878785cdab7e4a9ccf40b584c611c417a3faa7b4a08481bec7b1d8e7`
- `assurance.healing.schema.healing-safety.v1` — `de2609cbef7eff4f3fb644b21bfd688e6dbc5819618a14bf0bdb07ac1176ec47`
- `assurance.healing.schema.healing-status.v1` — `6b7c39e35175fff1918929cbd51076b55d29173125905a95f9e04b6b688d9f27`
- `assurance.healing.schema.proposal-approved-intent.v1` — `24f3d28027912dab7a3f699f82b1bd6dd6f745b3986bd0eacdde7a8230a54e0d`
- `assurance.healing.schema.proposal-approved-receipt.v1` — `01b1eebdd76326a97e32f7de1b15cf63ada1b769f4140fcaa36946fd770b7f4d`

Resources (SHA-256 of contributed resource bytes):
- `assurance.healing.persona.fix-proposer.v1` — `c11ce06c5c034671e1e08b63057c7f67643d581e25b174df785ea4011ca60a15`
- `assurance.healing.policy.test-change-policy.v1` — `27b115dde78b6c59f1571d7e6c07e36fb56860d9709806bf8b347e9e88990708`
- `assurance.healing.result.coverage-repair.v1` — `3d69e6a60c43add6e4149d133b392111a5c3ff0b400f9c07745becdd0aa7a706`
- `assurance.healing.result.fix-proposal.v1` — `d20e03e0e60203b851fa919ddf6591cea818076323288985c4a2f8a6fed7dc8a`
- `assurance.healing.skill.aa-coverage-repair.v1` — `8e8148acf896e7c7e31957f5352022b2c2a3a37abc601f2b8b0df0e29ae0f511`
- `assurance.healing.skill.aa-fix-proposal.v1` — `bfdc736a82af1b75f367969f2ff96b9e6abf8bc6efb3705247b4e63af1b4a656`

Effects:
- `assurance.healing.effect.allocation.v2` intent `assurance.healing.schema.allocation-intent.v2` receipt `assurance.healing.schema.allocation-receipt.v2`
- `assurance.healing.effect.heal-apply.v2` intent `assurance.healing.schema.heal-apply-intent.v2` receipt `assurance.healing.schema.heal-apply-receipt.v2`
- `assurance.healing.effect.proposal-approved.v1` intent `assurance.healing.schema.proposal-approved-intent.v1` receipt `assurance.healing.schema.proposal-approved-receipt.v1`

### `assurance.quality`

Handlers:
- `assurance.quality.aggregate-nightly-metrics`
- `assurance.quality.apply-problem-review`
- `assurance.quality.build-coverage-gap-signals`
- `assurance.quality.collect-adversarial-yield`
- `assurance.quality.collect-diff-coverage`
- `assurance.quality.collect-observations`
- `assurance.quality.collect-pr-metrics-batch`
- `assurance.quality.compute-assertion-strength`
- `assurance.quality.compute-auth-matrix`
- `assurance.quality.compute-baseline-drift`
- `assurance.quality.compute-constraint-coverage`
- `assurance.quality.compute-journey-coverage`
- `assurance.quality.compute-threshold-slack`
- `assurance.quality.dashboard`
- `assurance.quality.derive-plan-layer-applicability`
- `assurance.quality.evaluate-retrospective-shortboards`
- `assurance.quality.fact-baseline.finalize`
- `assurance.quality.fact-baseline.prepare`
- `assurance.quality.generate-report`
- `assurance.quality.inspect`
- `assurance.quality.inspect.finalize`
- `assurance.quality.inspect.prepare`
- `assurance.quality.issue-analysis.finalize`
- `assurance.quality.issue-analysis.prepare`
- `assurance.quality.issue-triage.finalize`
- `assurance.quality.issue-triage.prepare`
- `assurance.quality.load-latest-pr-metrics`
- `assurance.quality.load-problem-review-context`
- `assurance.quality.materialize-c-layer-metrics`
- `assurance.quality.materialize-minimum-coverage`
- `assurance.quality.materialize-pr-metrics`
- `assurance.quality.materialize-quarantine-projection`
- `assurance.quality.materialize-trace-and-coverage-gaps`
- `assurance.quality.materialize-trace-projection`
- `assurance.quality.probe-coverage-repair-need`
- `assurance.quality.reconcile-issues`
- `assurance.quality.record-empty-issue-analysis`
- `assurance.quality.record-issue-analysis-failure`
- `assurance.quality.record-project-sync-pending`
- `assurance.quality.report.finalize`
- `assurance.quality.report.prepare`
- `assurance.quality.run-mutation-sample`
- `assurance.quality.run-nightly-metrics-pipeline`

Validators:
- `assurance.quality.validator.cross-artifact.v1`
- `assurance.quality.validator.issues.v1`
- `assurance.quality.validator.metrics.v1`
- `assurance.quality.validator.problem-apply.v1`
- `assurance.quality.validator.report.v1`
- `assurance.quality.validator.trace.v2`

Schemas (SHA-256 of contributed schema bytes):
- `assurance.quality.schema.adversarial-yield.v1` — `9a94eed6596f9db257d65147d4966092e73dce539db3ca8f29be8ffbaa66f7fd`
- `assurance.quality.schema.assertion-strength.v1` — `c3401289756d52af7cdd504cbdf69be511ecc2365ca19bbe5ed1452191126840`
- `assurance.quality.schema.auth-matrix.v1` — `d710cab2ea180d5362d06b3c1a79ebe6c51515c509f537880a964754813f3acf`
- `assurance.quality.schema.baseline-drift.v1` — `92b3891c54c845dbcb14c0f93f73012f3dbfcf13d72722552b4ec6594ba8a734`
- `assurance.quality.schema.c-layer.v1` — `da38172ed9391601982d3170df4473da58fff4cc3d48263c806e4c8f015b3a42`
- `assurance.quality.schema.constraint-coverage.v1` — `04bb2a00ec337aa3d26f2067dc8b2772f1cf7a912814763ac32423fc917444f2`
- `assurance.quality.schema.coverage-diff.v1` — `e2f71220dc220e3d38a790dcd9a55388938ea846029bbcc0a042d9cff05ff630`
- `assurance.quality.schema.coverage-gaps.v1` — `51bb0de88c1ed4f411df62fd09d6e16414188e4ef439968930784116d0f258d2`
- `assurance.quality.schema.fact-baseline.v1` — `afccbaa73cbfcee3384e178054b55930b00362b4889fb01b7f65b54763f98020`
- `assurance.quality.schema.issue-events.v1` — `45f09cb6f357301494eb75995c3bef7b20cf64d9bf6e464b0724dececc74e3c7`
- `assurance.quality.schema.issues.v1` — `49728b682d1ec26d4efdeea795f6ee0d044c4454f64b467d1693c8eb07e5bda1`
- `assurance.quality.schema.journey-coverage.v1` — `1216ef3605574b10444f412d417d0d723ddb8de578c03f4bef46211fb90895cb`
- `assurance.quality.schema.metrics.v1` — `a20c5bcfb54d3d4daf548939c5079f174c05caba80fd6821700de4edb09f9381`
- `assurance.quality.schema.minimum-coverage.v1` — `366eb9262f190af5165a0d62cd332c980f9135b47dddfb5a4f5fb7b58d22582f`
- `assurance.quality.schema.mutation.v1` — `77cd051117226b66d600b949447386a1382ec7a6495510d5449e61d123c1fb79`
- `assurance.quality.schema.perf-slack.v1` — `4cf76def638529a39479a74f0990d8f87c78885335f2701f3f0282c79c302246`
- `assurance.quality.schema.quality-gate.v2` — `1a5a208dfb92b9a506f5daf8ccb4e84d135f715fa720661cdd662f6534e90898`
- `assurance.quality.schema.quarantine.v1` — `ef9fae65a7b56611624cd884a2a09c38a7edd2d804daf2e924e2443f5bfc19eb`
- `assurance.quality.schema.report.v1` — `1cd3db7297dc4d11b718effffd1bd392366fc59ac718dc83d3d652847f9b26de`
- `assurance.quality.schema.sufficiency.v2` — `db8c82d19c0a603f61ed91a02bd47a080af065df7e34ea2c1b06cfb3a0603186`
- `assurance.quality.schema.trace-sufficiency.v1` — `2974eb78e8c908a576a7aa296b63ca7ddb3e43a549710b86ca395a0fe45d2462`
- `assurance.quality.schema.trace.v2` — `18e3ce418b072832240197d58719f089ee6bd4b25f183b6203bfd1e2b455af43`

Resources (SHA-256 of contributed resource bytes):
- `assurance.quality.persona.explorer.v1` — `fed71ec995f0c93a73a5042e7406830e8840808f55b0dc8601991e01d241eb48`
- `assurance.quality.persona.reporter.v1` — `dd6bfdacb94a287493298f0f76b18d86825713c49a1d7624e9f42fe4627459bf`
- `assurance.quality.persona.reviewer.v1` — `998e02d25f51af3dc9806fa556114e4498cfefcf5cd822abf404f80134590b41`
- `assurance.quality.result.fact-baseline.v1` — `a9ad73d3b558f8a8681a95d06978c681a6cf24b667e3e6581aafe620f752229f`
- `assurance.quality.result.inspection.v1` — `a41cf3cc00a7011eabfe9edb4e4a4f47fb894502b29c1ecebc8ca371247a9440`
- `assurance.quality.result.issue-analysis.v1` — `da3e99b69078a847a988a3c8287df9b60a53d01f492314dcc93623a8eb44465a`
- `assurance.quality.result.issue-triage.v1` — `00d5435265b0f94972f2407e63c7c8764a3aa7bcbf6d8b4f0a4911b1daa1ec12`
- `assurance.quality.result.report.v1` — `7d6b93bd75f560617d2234571815c39c56161fc0334afba5ee84d38d29f97809`
- `assurance.quality.skill.aa-dashboard.v1` — `78eb61a0ec1b8bac7048b6d77dfbcf632b145e377206cf1b9ff8ed9d67b73513`
- `assurance.quality.skill.aa-fact-baseline.v1` — `aaab87b553b90d8e12cf327101ac4fd3cf73a0939bd498488d8620cd3deeb9b7`
- `assurance.quality.skill.aa-inspect.v1` — `cb1ef1dcb0b9c2268f844567e2d69e99ae5eed0635cb5ad12c0d60c8a15c899d`
- `assurance.quality.skill.aa-issue-analyzer.v1` — `77dc8bb96f280d13d95c509f20aa976e570010b6538357196d7e46a5aba9568c`
- `assurance.quality.skill.aa-issue-triage-advisor.v1` — `c199c0844ddb5d2a8c15d04bcf6035b3ccfb20df820860d98c286f9c82d06539`
- `assurance.quality.skill.aa-report-generator.v1` — `239e5db0a7b96b06387ab17a36759df502073cb41c32406cd74c686d982fba99`

Effects: none.

### `assurance.improvement`

Handlers:
- `assurance.improvement.apply-improvement-auto-review`
- `assurance.improvement.apply-improvement-review`
- `assurance.improvement.apply-memory-improvement`
- `assurance.improvement.archive.finalize`
- `assurance.improvement.archive.prepare`
- `assurance.improvement.assemble-retro-context-v3`
- `assurance.improvement.drain-improvement-outbox`
- `assurance.improvement.evaluate-memory-improvement`
- `assurance.improvement.export-change-improvement`
- `assurance.improvement.export-knowledge-improvement`
- `assurance.improvement.finalize-retro-status`
- `assurance.improvement.improvement-review.finalize`
- `assurance.improvement.improvement-review.prepare`
- `assurance.improvement.load-improvement-delivery`
- `assurance.improvement.load-improvement-review-context`
- `assurance.improvement.load-review-subject`
- `assurance.improvement.materialize-empty-retro-analysis`
- `assurance.improvement.project-archive`
- `assurance.improvement.reconcile-improvements`
- `assurance.improvement.record-analysis-failed`
- `assurance.improvement.record-auto-review-orchestration-error`
- `assurance.improvement.record-change-improvement-applied`
- `assurance.improvement.record-improvement-auto-review-error`
- `assurance.improvement.record-knowledge-improvement-applied`
- `assurance.improvement.record-retro-pipeline-failure`
- `assurance.improvement.retro-collect-v3`
- `assurance.improvement.retro-eval-analysis.finalize`
- `assurance.improvement.retro-eval-analysis.prepare`
- `assurance.improvement.retro-evidence-gap-fallback`
- `assurance.improvement.retro-issue-analysis.finalize`
- `assurance.improvement.retro-issue-analysis.prepare`
- `assurance.improvement.retro-workflow-analysis.finalize`
- `assurance.improvement.retro-workflow-analysis.prepare`
- `assurance.improvement.retro.finalize`
- `assurance.improvement.retro.prepare`
- `assurance.improvement.rollback-memory-improvement`
- `assurance.improvement.select-current-retro-auto-review-items`
- `assurance.improvement.summarize-auto-review-batch`
- `assurance.improvement.validate-improvement-review-assessment`

Validators:
- `assurance.improvement.validator.archive-integrity.v1`
- `assurance.improvement.validator.candidates.v3`
- `assurance.improvement.validator.delivery.v1`
- `assurance.improvement.validator.review.v1`

Schemas (SHA-256 of contributed schema bytes):
- `assurance.improvement.schema.declaration-proposal.v1` — `6ba5c5a3ebb7273e7de9e920582b27021228873d100d6beb8ccb64fb06aba583`
- `assurance.improvement.schema.improvement-candidates.v3` — `ba7e544899843786b079598a4a3c60d71446d71f2540a865aeb1cad8d841ed27`
- `assurance.improvement.schema.improvement-delivery.v1` — `ecc4eefb4cf4ca0d30bcb98b447b137325fbdf3fbae34132db63e729fa365448`
- `assurance.improvement.schema.improvement-effect-intent.v1` — `81ee490fac3c270a6ec6fff46f31d13cfb10f620e5f43e91d492b0c39cb7f2c7`
- `assurance.improvement.schema.improvement-effect-receipt.v1` — `8eecb503f0c99cedd047368952ae6702c34a80d09426b09402641669b225dda2`
- `assurance.improvement.schema.improvement-review.v1` — `717eeb8e9425cf767ace97a60c9597b941ad1de2358fd11e538bb4933654354a`
- `assurance.improvement.schema.promotion.v1` — `f883e800f758470ac89f50330b3335c3eaf60cbed63ba1d4ed534ff97190bbcc`
- `assurance.improvement.schema.retro-context.v3` — `ee0ed6a34e8eb49263ea7b4b9f9141e7befc506d8432c5c51dc74ddbb57af63b`
- `assurance.improvement.schema.retro-signals.v3` — `9afacabf58502cdd5eaafd4f4c639d2547847fb1689816e5ad0150bdb81bb154`

Resources (SHA-256 of contributed resource bytes):
- `assurance.improvement.persona.archiver.v1` — `3c62935ed9fba45dbc2ee4e9c7e1a83498921bd81ab859dd797f8e9649c8b209`
- `assurance.improvement.persona.reviewer.v1` — `75405b0db8c78c02174cb775613c193baf27b2126c1ea74cf17cf9a8789aedac`
- `assurance.improvement.prompt.archive-summary.v1` — `814eb3a6a80242ca7d5ce81f6518473d69e2d9fc3fbf44da73094bb7f6e12ff9`
- `assurance.improvement.result.archive.v1` — `a8c6fad6dfab702106c157344b2a0926c889fb8827ae5fd134b251e7207d2fcc`
- `assurance.improvement.result.improvement-review.v1` — `8a67e7e5571e240d3f5e5212b6c90f4960ddad7f87ad6ac49d149ea52edc7055`
- `assurance.improvement.result.retro-analysis.v3` — `c3cb7c1f83958656a5281f7e36483dbd725b211d0d850b4bec69c97fddddd619`
- `assurance.improvement.skill.aa-archive.v1` — `81584644ff9e2f4892174d528e38412573ebb9c42cbb18c64cae9fe129b40ce9`
- `assurance.improvement.skill.aa-improvement-reviewer.v1` — `385747e29e078985a3e21786cf05a003eeef8d5cd1b48c18685771e3a215337c`
- `assurance.improvement.skill.aa-retro-eval-analysis.v1` — `789cae505bbff5ea6282a7ecd0cfec5a5e79e7c5dae4aa24143ec514639094c8`
- `assurance.improvement.skill.aa-retro-issue-analysis.v1` — `b6cd1ab155ccc55cff9c4636fbcdf1da20de2963d627431def7ab845c5285348`
- `assurance.improvement.skill.aa-retro-workflow-analysis.v1` — `eb83ce9fa3a1e664d1a2857c2993b107f2ca6c966dc4f5861eb3f2b270efbc55`
- `assurance.improvement.skill.aa-retro.v1` — `6e43a69013f232a473ccc31a0c516a890c2304eb3a2ce4e1f1c45e507d54195d`

Effects:
- `assurance.improvement.effect.archive.v1` intent `assurance.improvement.schema.improvement-effect-intent.v1` receipt `assurance.improvement.schema.improvement-effect-receipt.v1`
- `assurance.improvement.effect.delivery.v1` intent `assurance.improvement.schema.improvement-effect-intent.v1` receipt `assurance.improvement.schema.improvement-effect-receipt.v1`
- `assurance.improvement.effect.promotion.v1` intent `assurance.improvement.schema.improvement-effect-intent.v1` receipt `assurance.improvement.schema.improvement-effect-receipt.v1`

## Artifact handoff table

Canonical envelope is `{schema_id, digest, family, payload}`. Fixture capability catalog `tests/phase4/fixtures/capability-catalog.v1.json` digest `e66180b1382331d90f58d7f0fd54db5fe853239c902a563c4b5d618ee3681e3a` with exact leafs `auth.session.create`, `capabilities.adapters.create`, `entities.item.create`.

| Seam | Producer | Consumer | Schema IDs | Schema SHA-256 |
|---|---|---|---|---|
| intake reviewed case → generation planning | `CaseYamlAuthoring` | `validate_plan_input` | `assurance.intake.schema.case-authoring.v1` | `e55a271a27c6697255a01854ae9d0cebea9b0a0d69514888ca71a3459512abe9` |
| generation reviewed plan / mapping → execution selection | `PlanResultV1` + `CodegenMapping` | `SelectHandler` | `assurance.generation.schema.plan-review.v1`, `assurance.generation.schema.codegen-mapping.v1` | `8ce89f2fd86034117786c0c3c0110ccd0736595fd2047e743502e9f874889de4`, `9d623c3f691070097c9d3f120877db09596ef1dbdd060814e82d16e9876a44d1` |
| execution evidence → healing proposal and quality trace | `ExecutionEvidenceV1` | `FixProposalFinalizeHandler`, `project_trace` | `assurance.execution.schema.execution-evidence.v1`, `assurance.quality.schema.trace.v2` | `c072c8f5f120b9311c80e4dc4dfae3dad00ab53bb9afb1b4f4e0b9fd80bfced3`, `18e3ce418b072832240197d58719f089ee6bd4b25f183b6203bfd1e2b455af43` |
| healing status → quality inspection / report | `HealingStatusV1` | `InspectHandler` | `assurance.healing.schema.healing-status.v1`, `assurance.quality.schema.quality-gate.v2` | `6b7c39e35175fff1918929cbd51076b55d29173125905a95f9e04b6b688d9f27`, `1a5a208dfb92b9a506f5daf8ccb4e84d135f715fa720661cdd662f6534e90898` |
| quality coverage gap → healing repair brief | `CoverageGapsDocument` | `coverage_gap_to_repair_brief` | `assurance.quality.schema.coverage-gaps.v1`, `assurance.healing.schema.coverage-repair.v1` | `51bb0de88c1ed4f411df62fd09d6e16414188e4ef439968930784116d0f258d2`, `51bdecc0456894314c5cae7c3d508481cdb7b43834ca1c761c3e759bcc1aa422` |
| quality report / issues / metrics → improvement retro | `QualityReport` | `project_archive` | `assurance.quality.schema.report.v1`, `assurance.improvement.schema.retro-context.v3` | `1cd3db7297dc4d11b718effffd1bd392366fc59ac718dc83d3d652847f9b26de`, `ee0ed6a34e8eb49263ea7b4b9f9141e7befc506d8432c5c51dc74ddbb57af63b` |

## Prepare binding-data contract

Every prepare handler accepts one closed mapping and no other keys:

```json
{
  "execution": {
    "provider_model": "<exact pinned model id>",
    "worker_profile": "<exact worker profile>",
    "permission_profile_digest": "<sha256 hex>",
    "limits": { "max_seconds": <int> }
  },
  "request_policy_digest": "<sha256 hex>",
  "request_config_digest": "<sha256 hex>"
}
```

Task 20 fixture literals (test-only six-wheel product, not production):

| Field | Digest |
|---|---|
| `permission_profile_digest` | `3194dadc3ff9c0aab048f1134a4a96f6f224ad79b61a7e72991597f1359e32c8` |
| `request_policy_digest` | `dae6306f35a9520074c1432e73383c30847934a70e973d92f4e86b38f49c5599` |
| `request_config_digest` | `b707f8f476bee6f45fd646aba8a0877896bb5f88ff89213d4c7ae3f5d1004f11` |

Test-only binding aliases (never selected by `aa`):

- `test.assurance.bindings.prepare` → `assurance.intake.case-review.prepare`
- `test.assurance.bindings.execute` → `runtime.opencode.execute` or `runtime.cursor.execute`
- `test.assurance.bindings.finalize` → `assurance.intake.case-review.finalize`

## Logical agent capability IDs requiring Phase 5 adapter bindings

Phase 5 must bind each prepare ID to a Phase 3 runtime execute capability (`runtime.opencode.execute` or `runtime.cursor.execute`) plus the matching finalize ID. Wheels do not choose adapters.

- `assurance.intake.case-design.prepare`
- `assurance.intake.case-review.prepare`
- `assurance.intake.explore.prepare`
- `assurance.intake.intake.prepare`
- `assurance.generation.api.codegen-fix.prepare`
- `assurance.generation.api.codegen.prepare`
- `assurance.generation.api.plan-review.prepare`
- `assurance.generation.api.plan.prepare`
- `assurance.generation.e2e.codegen-fix.prepare`
- `assurance.generation.e2e.codegen.prepare`
- `assurance.generation.e2e.plan-review.prepare`
- `assurance.generation.e2e.plan.prepare`
- `assurance.generation.fuzz.codegen.prepare`
- `assurance.generation.fuzz.plan-review.prepare`
- `assurance.generation.fuzz.plan.prepare`
- `assurance.generation.performance.codegen.prepare`
- `assurance.generation.performance.plan-review.prepare`
- `assurance.generation.performance.plan.prepare`
- `assurance.execution.execute.prepare`
- `assurance.execution.run.prepare`
- `assurance.healing.coverage-repair.prepare`
- `assurance.healing.fix-proposal.prepare`
- `assurance.quality.fact-baseline.prepare`
- `assurance.quality.inspect.prepare`
- `assurance.quality.issue-analysis.prepare`
- `assurance.quality.issue-triage.prepare`
- `assurance.quality.report.prepare`
- `assurance.improvement.archive.prepare`
- `assurance.improvement.improvement-review.prepare`
- `assurance.improvement.retro-eval-analysis.prepare`
- `assurance.improvement.retro-issue-analysis.prepare`
- `assurance.improvement.retro-workflow-analysis.prepare`
- `assurance.improvement.retro.prepare`

## Deliberately absent from Phase 4 (Phase 5 owns)

- Production `ProductManifest` / `assurance-product` distribution
- Production graph, `entrypoints`, and public graph CLI entry
- `aa` composition-root cutover (`aa` still defaults to the legacy product)
- Production binding plugins that map prepare/execute/finalize
- Organization model endpoint, secret port, permission profile, and request-policy files
- Organization `.aa/config.yaml` / `.aa/policy.yaml` / workflow-schema replacement as the new product graph
- Resume of old-runtime invocations on the new engine

## Committed-HEAD wheel smoke

Release authority: `scripts/assurance_capability_wheel_smoke_test.sh`.

- Task 19 (`568d4d2`): committed-HEAD isolation **OK**. Built filenames match the table above. `ENGINE_API_VERSION == "2.0"`. Static declaration equals live descriptor. No `assurance-agent` / `assurance-kernel` in isolated venvs.
- Task 22 re-runs the same script from the Phase 4 close commit after that commit lands. Append exact SHA and output to the local `final-report.md` (gitignored). Do not treat this handoff as claiming that later smoke until the close commit exists.

## Known non-blocking concerns (owner → target phase)

| Concern | Owner | Target |
|---|---|---|
| Quality / improvement filesystem finalize still `del context`; Task 21 path cells recorded `seam="uncovered"` | assurance.quality, assurance.improvement | Phase 5 handler wiring |
| 178 migrate `module` rows and 4 migrate `callable` rows stay `planned` (Task 1 freeze; kernel/legacy sources coexist) | all six (legacy trees) | Phase 6 deletion |
| `semantic_pins` stays `delete_phase6` / `planned` / owner null | none | Phase 6 |
| Dashboard scripts, `failure-classification.yaml`, explore JSON samples were not shipped by `assurance-quality`; reclassified `delete_phase6` | assurance.quality leftover | Phase 6 |
| CI runs capability wheel smoke twice (packaging script + named step) | tooling | Phase 5 CI tidy |
| Packaging smoke fails on dirty tracked files; untracked `.superpowers/` does not | tooling | keep |
| Checked-in six-wheel fixture declarations embed absolute `config_plugin_paths` | tests/phase4 | Phase 5 test fixture cleanup |
| Permission-profile digest in Task 20 is a harness constant | tests/phase4 | Phase 5 org policy |
| Replay / transcript coverage is one adapter each; temp six-wheel workspaces kept on disk | tests/phase4 | Phase 5 |
| Kernel `CompiledWorkflow.schema` field-name warning | graph-engine / assurance-kernel | later engine cleanup |
| Healing does not import quality; quality may import healing contracts | assurance.healing, assurance.quality | keep |
| Recreation scan misses `ClassDef`; catch-all ignores module mappings; fixture `compare` unused; episode uses `from_events` | tests/phase4 hook parity | optional |

Phase 6 deletion inventory: `phase6-deletion.txt` (286 paths). Benchmark datasets and scorers are excluded.
