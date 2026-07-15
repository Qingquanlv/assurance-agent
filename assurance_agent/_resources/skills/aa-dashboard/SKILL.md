---
name: aa-dashboard
description: >
  Launch the Case Center browser dashboard to visualize qa/cases/ and qa/changes/ YAML files.
  Use when the user wants to view, browse, or inspect test cases visually.
  Trigger phrases: "查看 case", "Case Center", "展示用例", "open dashboard", "show cases"
---

## Context Contract

Do not rely on prior conversation context.

**Before doing any work:**

1. Read `qa/changes/<change-id>/workflow-state.yaml` if a change-id is provided.
2. Identify the project root containing `qa/cases/` and `qa/changes/`.
3. Use files as the sole source of truth for case data.

**This skill is read-only.** It does not write to `workflow-state.yaml` or modify any case files.

---

# QA Dashboard — Case Center

Launch a browser-based Case Center that reads `qa/cases/` and `qa/changes/` YAML files
from the project and displays them in a filterable three-panel layout.

## When to Use

- User wants to browse or inspect test cases visually
- User wants to compare delta cases in a change (`qa/changes/<id>/`)
- During QA review to visualize coverage

## How to Start

```bash
skills/aa-dashboard/scripts/start-server.sh --project-dir <project-root>
```

`<project-root>` must be the directory containing the `qa/` folder.

The script outputs JSON with the URL. Tell the user to open it:

```
{"type":"server-started","url":"http://localhost:PORT/cases",...}
```

## Requirements

- Python 3 must be available
- `--project-dir` must point to a directory containing `qa/cases/` or `qa/changes/`
- If neither directory exists, the page shows a friendly error — it won't crash

## Stopping

The `server-started` JSON output includes a `session_dir` field. Pass it to `stop-server.sh`:

```bash
skills/aa-dashboard/scripts/stop-server.sh <session_dir>
```

Example: if the server started with `"session_dir":"/path/to/project/.superpowers/aa-dashboard/12345-1234567890"`, run:
```bash
skills/aa-dashboard/scripts/stop-server.sh /path/to/project/.superpowers/aa-dashboard/12345-1234567890
```
