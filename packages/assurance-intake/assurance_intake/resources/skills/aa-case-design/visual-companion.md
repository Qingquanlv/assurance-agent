# Visual Companion Guide — AA Case Design

Browser-based visual companion for showing coverage diagrams, E2E path flows, coverage matrices, and UI mockups during `aa-case-design` brainstorming sessions.

## When to Use

Decide **per question, not per session**. The test: **would the user understand this better by seeing it than reading it?**

**Use the browser** when the content itself is visual:

- **UI flow comparison** — side-by-side comparison of two E2E user paths
- **E2E path diagram** — step-by-step visualization of the user journey under test
- **Test coverage matrix** — module × test type grid showing what is already covered vs. what this change adds
- **Data flow diagram** — how data moves between API, database, and UI layers
- **Page interaction mockup** — which UI elements are touched by each test case
- **API/E2E/Unit/Visual coverage map** — overall coverage landscape for a module

**Use the terminal** when the content is text or a decision:

- **Module confirmation** — "Does this belong in qa/cases/warehouse/inbound/?"
- **Change type confirmation** — ADDED / MODIFIED / REMOVED
- **Data needs clarification** — what entities must exist and in what state
- **Success assertion text** — what the observable outcome should be
- **Exception scope decision** — should we include or exclude this edge case?
- **Automation target yes/no** — should we generate tests/api or tests/e2e?
- **Coverage approach selection (A/B/C)** — trade-off comparison in words

A question *about* a test topic is not automatically a visual question. "Should we test the empty state?" is a scope decision — use the terminal. "Which E2E path through this wizard covers the happy path?" is a visual question — use the browser.

---

## How It Works

The server watches a directory for HTML files and serves the newest one to the browser. You write HTML content to `screen_dir`, the user sees it in their browser and can click to select options. Selections are recorded to `state_dir/events` that you read on your next turn.

**Content fragments vs full documents:** If your HTML file starts with `<!DOCTYPE` or `<html`, the server serves it as-is (just injects the helper script). Otherwise, the server automatically wraps your content in the frame template — adding the header, CSS theme, selection indicator, and all interactive infrastructure. **Write content fragments by default.** Only write full documents when you need complete control over the page.

---

## Starting a Session

```bash
# Start server with persistence (brainstorm files saved to project)
scripts/start-server.sh --project-dir /path/to/project

# Returns: {"type":"server-started","port":52341,"url":"http://localhost:52341",
#           "screen_dir":"/path/to/project/.superpowers/brainstorm/12345-1706000000/content",
#           "state_dir":"/path/to/project/.superpowers/brainstorm/12345-1706000000/state"}
```

Save `screen_dir` and `state_dir` from the response. Tell user to open the URL.

**Finding connection info:** The server writes its startup JSON to `$STATE_DIR/server-info`. If you launched the server in the background and didn't capture stdout, read that file to get the URL and port. When using `--project-dir`, check `<project>/.superpowers/brainstorm/` for the session directory.

**Note:** Pass the project root as `--project-dir` so brainstorm visual files persist in `.superpowers/brainstorm/` and survive server restarts. Without it, files go to `/tmp` and get cleaned up. Remind the user to add `.superpowers/` to `.gitignore` if it's not already there.

**Launching the server by platform:**

**Claude Code (macOS / Linux):**
```bash
scripts/start-server.sh --project-dir /path/to/project
```

**Claude Code (Windows):**
```bash
# Windows auto-detects and uses foreground mode, which blocks the tool call.
# Use run_in_background: true on the Bash tool call so the server survives
# across conversation turns.
scripts/start-server.sh --project-dir /path/to/project
```
When calling this via the Bash tool, set `run_in_background: true`. Then read `$STATE_DIR/server-info` on the next turn to get the URL and port.

**Codex:**
```bash
# Codex reaps background processes. The script auto-detects CODEX_CI and
# switches to foreground mode. Run it normally — no extra flags needed.
scripts/start-server.sh --project-dir /path/to/project
```

**Gemini CLI:**
```bash
# Use --foreground and set is_background: true on your shell tool call
# so the process survives across turns
scripts/start-server.sh --project-dir /path/to/project --foreground
```

**Other environments:** The server must keep running in the background across conversation turns. If your environment reaps detached processes, use `--foreground` and launch the command with your platform's background execution mechanism.

If the URL is unreachable from your browser (common in remote/containerized setups), bind a non-loopback host:

```bash
scripts/start-server.sh \
  --project-dir /path/to/project \
  --host 0.0.0.0 \
  --url-host localhost
```

---

## The Loop

1. **Check server is alive**, then **write HTML** to a new file in `screen_dir`:
   - Before each write, check that `$STATE_DIR/server-info` exists. If it doesn't (or `$STATE_DIR/server-stopped` exists), the server has shut down — restart it with `start-server.sh` before continuing. The server auto-exits after 30 minutes of inactivity.
   - Use semantic filenames: `e2e-path.html`, `coverage-matrix.html`, `ui-flow.html`
   - **Never reuse filenames** — each screen gets a fresh file
   - Use Write tool — **never use cat/heredoc** (dumps noise into terminal)
   - Server automatically serves the newest file

2. **Tell user what to expect and end your turn:**
   - Remind them of the URL (every step, not just first)
   - Give a brief text summary of what's on screen (e.g., "Showing the E2E path for the order receive flow — 3 steps")
   - Ask them to respond in the terminal: "Take a look and let me know what you think. Click to select an option if you'd like."

3. **On your next turn** — after the user responds in the terminal:
   - Read `$STATE_DIR/events` if it exists — this contains the user's browser interactions (clicks, selections) as JSON lines
   - Merge with the user's terminal text to get the full picture
   - The terminal message is the primary feedback; `state_dir/events` provides structured interaction data

4. **Iterate or advance** — if feedback changes current screen, write a new file (e.g., `coverage-matrix-v2.html`). Only move to the next question when the current step is validated.

5. **Unload when returning to terminal** — when the next step doesn't need the browser (e.g., a data needs question, a scope decision), push a waiting screen to clear the stale visual:

   ```html
   <!-- filename: waiting.html (or waiting-2.html, etc.) -->
   <div style="display:flex;align-items:center;justify-content:center;min-height:60vh">
     <p class="subtitle">Continuing in terminal...</p>
   </div>
   ```

   This prevents the user from staring at a resolved diagram while the conversation has moved on. When the next visual question comes up, push a new content file as usual.

6. Repeat until done.

---

## Writing Content Fragments

Write just the content that goes inside the page. The server wraps it in the frame template automatically (header, theme CSS, selection indicator, and all interactive infrastructure).

**Coverage matrix example:**

```html
<h2>Test Coverage Matrix: Warehouse Inbound</h2>
<p class="subtitle">Green = existing coverage · Blue = this change adds · Gray = out of scope</p>

<div class="options" data-multiselect>
  <div class="option" data-choice="api-happy" onclick="toggleSelect(this)">
    <div class="letter">A</div>
    <div class="content">
      <h3>API — Happy Path</h3>
      <p>POST /api/inbound/receive — verify state transition and response</p>
    </div>
  </div>
  <div class="option" data-choice="e2e-happy" onclick="toggleSelect(this)">
    <div class="letter">B</div>
    <div class="content">
      <h3>E2E — Happy Path</h3>
      <p>User receives goods via the Inbound screen, verifies status updates</p>
    </div>
  </div>
  <div class="option" data-choice="api-exception" onclick="toggleSelect(this)">
    <div class="letter">C</div>
    <div class="content">
      <h3>API — Exception: duplicate receive</h3>
      <p>POST /api/inbound/receive with already-received order — verify 409 response</p>
    </div>
  </div>
</div>
```

**E2E path diagram example:**

```html
<h2>E2E Path: Order Receive Flow</h2>
<p class="subtitle">Steps the user takes — natural language, no locators</p>

<div class="mockup">
  <div class="mockup-header">User Journey</div>
  <div class="mockup-body">
    <ol>
      <li>Navigate to Warehouse &gt; Inbound</li>
      <li>Search for order PO-12345</li>
      <li>Click "Receive All"</li>
      <li>Confirm the receive dialog</li>
      <li>Verify order status changes to "Received"</li>
      <li>Verify item quantities are updated in stock</li>
    </ol>
  </div>
</div>
```

---

## CSS Classes Available

The frame template provides these CSS classes for your content:

### Options (A/B/C choices)

```html
<div class="options">
  <div class="option" data-choice="a" onclick="toggleSelect(this)">
    <div class="letter">A</div>
    <div class="content">
      <h3>Title</h3>
      <p>Description</p>
    </div>
  </div>
</div>
```

**Multi-select:** Add `data-multiselect` to the container to let users select multiple options (e.g., selecting multiple test types to include).

```html
<div class="options" data-multiselect>
  <!-- same option markup — users can select/deselect multiple -->
</div>
```

### Cards (visual designs)

```html
<div class="cards">
  <div class="card" data-choice="coverage-a" onclick="toggleSelect(this)">
    <div class="card-image"><!-- diagram content --></div>
    <div class="card-body">
      <h3>Coverage Plan A</h3>
      <p>API + E2E, 4 cases</p>
    </div>
  </div>
</div>
```

### Mockup container

```html
<div class="mockup">
  <div class="mockup-header">E2E Path: Inbound Receive</div>
  <div class="mockup-body"><!-- your path or flow content --></div>
</div>
```

### Split view (side-by-side comparison)

```html
<div class="split">
  <div class="mockup"><!-- coverage plan A --></div>
  <div class="mockup"><!-- coverage plan B --></div>
</div>
```

### Pros/Cons

```html
<div class="pros-cons">
  <div class="pros"><h4>Pros</h4><ul><li>Covers backend state</li></ul></div>
  <div class="cons"><h4>Cons</h4><ul><li>More data setup required</li></ul></div>
</div>
```

### Mock elements (wireframe building blocks for UI flow mockups)

```html
<div class="mock-nav">Warehouse | Inbound | Receive</div>
<div style="display: flex;">
  <div class="mock-sidebar">Order List</div>
  <div class="mock-content">Order Detail Panel</div>
</div>
<button class="mock-button">Receive All</button>
<div class="placeholder">Status: Pending → Received</div>
```

### Typography and sections

- `h2` — page title
- `h3` — section heading
- `.subtitle` — secondary text below title
- `.section` — content block with bottom margin
- `.label` — small uppercase label text

---

## Browser Events Format

When the user clicks options in the browser, their interactions are recorded to `$STATE_DIR/events` (one JSON object per line). The file is cleared automatically when you push a new screen.

```jsonl
{"type":"click","choice":"api-happy","text":"API — Happy Path","timestamp":1706000101}
{"type":"click","choice":"e2e-happy","text":"E2E — Happy Path","timestamp":1706000108}
{"type":"click","choice":"api-exception","text":"API — Exception: duplicate receive","timestamp":1706000115}
```

The full event stream shows the user's exploration path — they may click multiple options before settling. The last `choice` event is typically the final selection, but the pattern of clicks can reveal hesitation or preferences worth asking about.

If `$STATE_DIR/events` doesn't exist, the user didn't interact with the browser — use only their terminal text.

---

## Design Tips for QA Visuals

- **Scale fidelity to the question** — path diagrams for flow, matrix for coverage scope questions
- **Explain the question on each page** — "Which coverage approach fits this change?" not just "Pick one"
- **Keep natural steps human-readable** — no CSS selectors, XPath, or data-testid attributes in any visual
- **Iterate before advancing** — if feedback changes current screen, write a new version
- **2–4 options max** per screen
- **Coverage matrix** — use color coding: green = existing, blue = this change adds, gray = out of scope
- **E2E path diagram** — use numbered steps in plain English; the goal is to align on what the user does, not how the automation implements it

---

## File Naming

- Use semantic names: `e2e-path.html`, `coverage-matrix.html`, `ui-flow.html`, `data-flow.html`
- Never reuse filenames — each screen must be a new file
- For iterations: append version suffix like `coverage-matrix-v2.html`
- Server serves newest file by modification time

---

## Cleaning Up

```bash
scripts/stop-server.sh $SESSION_DIR
```

If the session used `--project-dir`, visual files persist in `.superpowers/brainstorm/` for later reference. Only `/tmp` sessions get deleted on stop.

---

## Reference

- Frame template (CSS reference): `scripts/frame-template.html`
- Helper script (client-side): `scripts/helper.js`
