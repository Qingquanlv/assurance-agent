# Installing AA for OpenCode

> **Naming note:**
> - **AA** = **Assurance Agent**. This is the project name and CLI prefix.
> - `aa-*` is used for all skills and OpenCode agents in this project.
> - `aa` is the project CLI — for example `aa run` and `aa report inspect`.

---

## Prerequisites

- [OpenCode.ai](https://opencode.ai) installed
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Git available in your terminal

---

## Installation

### Option 1: via `aa init` (recommended)

Run in your project directory:

```bash
aa init
```

`aa init` writes `opencode.json` with the plugin entry and copies the AA
skills, agents, tools and plugin into the project. Then restart OpenCode **from
that project directory** and run `skill load aa-workflow` to start the workflow.

### Option 2: manual

Add AA to the `plugin` array in your project `opencode.json`, and copy the
package assets in with `aa skill refresh --sync-agents`:

```json
{
  "plugin": ["./.opencode/plugins/aa.mjs"]
}
```

Restart OpenCode after editing `opencode.json`. The plugin registers all AA QA
workflow skills automatically.

---

## Usage

The main entry skill is `aa-workflow`:

```
skill load aa-workflow
```

Key CLI commands (run in terminal, never fabricated):

```bash
aa run --change <change-id>
aa report inspect --change <change-id>
```

## Updating

If skill updates do not appear after restart, re-sync from the package and
restart OpenCode:

```bash
aa skill refresh --sync-agents
```

`aa skill refresh` copies the packaged skills into `<project>/skills/`. With
`--sync-agents`, it also copies a runtime skill mirror plus the agents/tools/plugin
into `<project>/.opencode/`; the runtime mirror is what OMO discovers directly.
`--sync-opencode-user-skills` also updates only namespaced `aa-*` user skills as
defense in depth. It does not make a shared OpenCode/OMO server started outside
the project valid for benchmark execution: OMO discovers its project catalog
from the server working directory, so benchmark preflight requires that
directory to equal the SUT root and asks for a restart otherwise.
