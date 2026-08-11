# OpenCode OpenAI Benchmark Script Design

## Goal

Add a fully independent OpenCode benchmark entrypoint that preserves the existing
workflow lifecycle while replacing the current DeepSeek/GLM phase routing with
OpenAI models:

- `anthropic/deepseek-v4-flash` becomes `openai/gpt-5.6-luna`.
- `anthropic/glm-5.2` becomes `openai/gpt-5.6-terra`.

The existing OpenCode script and `benchmark/vue-fastapi-admin/.aa/config.yaml`
must remain unchanged by the new entrypoint, including while it is running.

## Design

### Model-routing override

`aa` will accept an optional `AA_MODEL_ROUTING_FILE` environment variable. The
referenced YAML file contains one complete `ModelRoutingCfg` document. A relative
path is resolved from the target project root; an absolute path is used directly.

When the variable is unset, `load_config()` behaves exactly as it does today.
When it is set, `load_config()` validates the normal `.aa/config.yaml`, validates
the override with the same `ModelRoutingCfg` schema, and replaces only
`execution.model_routing` in memory. It never writes either file.

Missing, malformed, or schema-invalid override files fail closed with a
`ConfigInvalidError` that identifies `AA_MODEL_ROUTING_FILE`.

### OpenAI routing policy

Add
`benchmark/vue-fastapi-admin/benchmark/opencode-openai-model-routing.yaml` as a
complete routing map. It mirrors every route key in the existing benchmark policy:

- the default and every DeepSeek route use `openai/gpt-5.6-luna`;
- every GLM route uses `openai/gpt-5.6-terra`;
- escalation uses `openai/gpt-5.6-terra` with the existing error kinds;
- `strict_routes` remains enabled.

The non-fast model variants are intentional. A global `OPENCODE_MODEL` override
must remain empty so phase routing is not collapsed to one model.

### Independent benchmark entrypoint

Add
`benchmark/vue-fastapi-admin/benchmark/run-workflow-loop-opencode-openai.sh`,
derived from the existing OpenCode loop. It exports the dedicated routing file
before invoking any `aa` workflow commands and uses an `opencode-openai` identity
for run directories, change IDs, retro IDs, logs, PID files, and summaries.

The new entrypoint retains the original benchmark environment contract,
single-item/full-suite selection, SUT management, workflow retries, archive,
verification, and retro behavior. It may use the same caller-supplied OpenCode
server endpoint because model selection is request-local, but its persisted
benchmark artifacts do not overlap the original script's artifacts.

The script rejects a non-empty `OPENCODE_MODEL`, because that setting would bypass
the Luna/Terra route map and violate the purpose of this entrypoint.

## Verification

Automated checks will cover:

1. loading an override replaces only `execution.model_routing`;
2. an unset override preserves existing configuration behavior;
3. missing and invalid override files fail closed;
4. the OpenAI route file has the same route keys as the current hybrid route map;
5. every DeepSeek-origin route maps to Luna and every GLM-origin route maps to
   Terra, including escalation;
6. the new script has valid Bash syntax, has an independent artifact identity,
   selects the override file, and prevents a global model override;
7. the original OpenCode script and shared `.aa/config.yaml` are unchanged by the
   implementation.

The locally installed OpenCode model catalog has already confirmed both exact
model identifiers exist.

## Non-goals

- Changing the existing DeepSeek/GLM benchmark policy.
- Using the `-fast` Luna or Terra variants.
- Starting or provisioning a separate OpenCode server.
- Running a full, hours-long benchmark as part of implementation verification.
