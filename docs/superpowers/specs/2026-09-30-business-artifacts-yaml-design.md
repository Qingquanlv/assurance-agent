# Business artifacts as YAML

Status: proposed; cross-package migration is not implemented or approved yet.

## Agreed boundary

Business documents produced by the workflow use YAML. Pydantic models in installed
capability wheels remain the only contract source. Agent request/reply transport,
published JSON Schema, runtime checkpoints/receipts, and semantic canonical encoding
remain JSON. `qa/requirement.md` remains plain text.

This changes the document format, not authority: OpenCode still writes authorized
raw files; finalizers authenticate and validate those files; the Kernel seals the
actual bytes. It does not revive the cancelled 2026-09-01 structured-artifact
materializer design, load executable contracts from the SUT, or add a codec registry.

Test source, images, third-party runner evidence (JUnit XML, pytest JSON, Playwright
traces), and packaged skill Markdown are not business documents to convert.
Organization memory/configuration formats are outside this migration.

## Baseline corrections and immediate repairs

The current generation graph registers codegen and codegen-review jobs, not the old
standalone plan jobs. `PlanFinalizeHandler` remains an exported helper but is not a
registered full-workflow handler. Its empty-Markdown guard is maintenance of that
helper, not proof that current full needs or validates the old plan package.

Before format migration, repair the existing trust boundaries:

- Intake validates a minimal `.qa.yaml` containing the locked `change_id`.
- Case-design validates the complete existing `QaYaml`, including change identity.
  Its first invalid result still requests one bounded repair; subsequent invalid
  results fail. Unchanged baseline files remain subject to validation.
- Case-review validates the persisted review against `CaseReviewResultV1` and
  compares it to the typed Agent reply before trusting its reference. Host-derived
  coverage projection remains separate; the raw review is not rewritten.

## Migration choice

Recommended: one coordinated cutover for new runs. Do not rewrite existing sealed
files or their digest references, and do not resume an old JSON run using new YAML
contracts. Old runs require their original authenticated deployment. New runs get
new wheel/code/graph revisions and bindings through the existing deployment process.

Alternative: an explicit dual-reader, versioned migration for unfinished runs. That
requires a separate compatibility design and recovery tests; do not implement it
as `try YAML, then JSON` or a silent suffix fallback.

The user has been asked whether unfinished-run compatibility is required. Until
confirmed, the recommendation above is a proposed boundary, not an implemented
guarantee. No real project or archived run is migrated by this change.

## Document owners and coordinated consumers

| Owner | Business artifacts to migrate | Consumers that must change together |
| --- | --- | --- |
| Intake prepare/explore | explore context, exploration draft/official document, impact inventory | resolve-plan, case-design, generation, quality |
| Intake resolve-plan | `resolved-assurance-plan.yaml` under its plan digest directory | every plan-ref authenticator and graph handoff |
| Intake case-design | `.qa.yaml` and case files stay YAML; proposal and minimum-coverage matrix become YAML | case repair locators, review, quality, archive readers |
| Intake case-review | review, summary, selection, reviewed-case manifest and per-round histories | generation approval, rework and epoch transitions |
| Generation | generated-files manifests, codegen reviews/summaries, cycle mapping and obligation-method document | execution, quality, healing and retro |
| Execution | owned execute/run-result summaries and observation documents | quality, healing, report and retro |
| Quality | owned surface/fact baseline, inspection, trace, coverage, metrics, issue and report documents | all downstream gates, report rendering, diagnostic workflow |
| Healing | fix proposal and owned repair documents | apply/validate and subsequent quality assessment |
| Improvement | retro context, candidates, reconciliation, status and business improvement ledger | retro display, next-run context, archive readers |

Keep current document granularity: do not add back old plan nodes, merge review
summary files, rename graph nodes, change routing, or restructure packages here.
The normal achieved branch and diagnostic branch remain different: retro is on the
diagnostic path, not a mandatory step of every successful full run. Archive remains
an independent entry; update shared readers only, not its entrypoint topology.

## Model and byte rules

1. Reuse each existing document model. For untyped containers, add a closed model in
   the owning `contracts/`; keep readers/writers in `operations/`.
2. Markdown business documents become structured models, not `{content: <old md>}`.
   Proposal fields preserve current repairable sections with typed field locators;
   report fields include its bound identities/digests, findings, conclusions and
   recommendations. Separate summaries get explicit summary models.
3. The obligation-method envelope validates its schema version, frozen plan binding,
   selected requirements, method plans and semantic reviews as a whole. Execution
   validates that envelope before choosing an observation method.
4. Parse safe, single-document YAML, reject duplicate mapping keys and unsupported
   tags, then call the owning Pydantic model with the same validation context as
   today. Use JSON-compatible scalar semantics so dates/identifiers do not change
   meaning through implicit YAML coercion. Reuse installed PyYAML, without a new
   dependency or configurable serializer framework.
5. Agent-authored YAML keeps its original bytes. Compare a persisted typed document
   to the typed reply where both represent the same document; do not silently
   overwrite it with a reserialized reply. Keep raw and host-derived projections
   explicit, especially case-review coverage.
6. Host-authored YAML uses a fixed deterministic encoding from
   `model_dump(mode="json", by_alias=True)`: UTF-8, sorted mapping keys, block style,
   Unicode allowed and one final LF. Evidence references always hash the
   actual written/read bytes. Internal semantic digests still hash canonical JSON;
   do not replace canonical JSON globally. References inside those projections may
   legitimately change semantic digests during cutover.
7. Frozen plan validation must distinguish its semantic `plan_digest` from
   `plan_ref.digest` of YAML bytes. Update `plan_artifact_ref`, codecs and output
   model invariants together; never require YAML bytes to equal canonical JSON.
8. Keep authorization, symlink/single-link checks, baseline digest authentication,
   write-set sealing and retry classification. All new path suffixes participate
   in resource claims, repair scopes, output routes, binding locks and graph state.

Use one small business-neutral module, `graph_engine/artifact_yaml.py`, for parsing
YAML data and encoding YAML bytes. Graph-engine already depends on PyYAML. This
module performs no file I/O, knows no business models, and changes no Kernel stage.
Capabilities keep model validation and authorized file access in `operations/`;
`contracts/` continues to contain models and policy, not readers or writers. Add no
model registry, pluggable backends or materialization stage.

## Verification and release conditions

- Trace actual registered producers to every reader, including retry baselines,
  downstream adapters, display/export commands, fixtures, skills and generated
  schema prompt notes. A renamed output without its reader is not a completed step.
- Regression tests reject malformed/duplicate-key YAML, wrong identities, missing
  model fields, reply/file mismatches, changed digests and unauthorized repair fields.
- A deterministic full-flow harness covers prepare → case → generation → execution
  → assessment/report; separate tests cover healing, coverage-epoch reentry and
  diagnostic retro. Test resume within the same new deployment, with no external
  model service required.
- Assert the new-run business output inventory is YAML except requirement text;
  explicitly retain transport/schema/runtime JSON and third-party evidence formats.
- Run Ruff, Pyright, import contracts, full pytest, rebuilt wheels and the three
  committed-source isolation smoke scripts before claiming release readiness.
- Do not present this proposed migration or the immediate validation repairs as a
  completed YAML cutover. Written-spec approval precedes the implementation plan.
