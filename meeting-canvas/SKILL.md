---
name: meeting-canvas
description: Generate a fresh interactive meeting worksheet as a self-contained HTML canvas — a draggable/resizable Gantt roadmap, paintable owner-color key, per-bar aim dates and comments, floating sticky notes, an editable decisions/parking-lot box, and full localStorage persistence with a "Copy plan" hand-back to Claude. Use when the user types /meeting-canvas, or asks for a "meeting canvas", "interactive meeting worksheet", "live meeting html", "meeting worksheet", or a live planning/roadmap page to drive a working session.
---

# Meeting Canvas

Produce a single self-contained HTML file from `assets/template.html` that the user can open, drag around live during a meeting, and hand back to you as JSON to bake changes into source. Then publish it with the Artifact tool.

## What the generated canvas contains

- **Masthead** with the meeting title and date.
- **Decisions / parking-lot box** — an editable, auto-saving free-text area.
- **Gantt roadmap** over a rolling 18-month window (starts at the current month, computed at page load):
  - Drag a bar to move it; drag its left/right edge to resize. Snaps to quarter-month.
  - A `◆` **aim-date** diamond in each bar, draggable independently.
  - **Row reordering** — drag the `⋮⋮` grip at the start of a row's label to move it up/down; the new order persists (`__order`). (Uses window-level pointer listeners so a row can travel past many neighbors in one gesture.)
  - **"+ row" button** — appends a persistent, nameable empty row (stored under an `extra<N>` key); added rows survive refresh and appear in Copy plan.
  - **Multi-dimensional visual tags** independent of owner color: each bar carries a **phase** (`st.trk`: `early` → pill edge / `late` → squiggly edge via the shared SVG `#gWobble` filter) and a **priority** (`st.prio`: `top` = 3px ink border / `mid` = 1.5px / `low` = none / `back` = dimmed + 45° hatch).
  - A **key strip** with three armable groups: the **owner-color key** plus **phase** and **priority** chips (miniature bars showing the real shape/border). Click any swatch or chip to arm it, then click bars to paint that value; re-click a bar to toggle the value off; arming a tag disarms owner paint and vice versa; **Esc** disarms everything. A header note shows what's being painted. **Double-click an owner swatch** to cycle that category through `--gc1..--gc8` (every bar already painted with it recolors immediately). "+ color" adds a category (max 8); slots are decoupled from identity and persisted (`__slots`). Owner labels are editable.
  - **Right-click** a bar or its label to open a **tag popover**: segmented phase/priority buttons, an assignee text field, and a `💬` shortcut to the comment editor (closes on outside-click / Esc).
  - **Color mode toggle** ("Color: key" ↔ "Color: assignee"): in *key* mode bars use their painted owner color; in *assignee* mode bars are colored by distinct assignee (each distinct assignee maps to `--gc1..--gc8` in first-appearance order, cycling; unassigned bars stay neutral) and the key area shows a read-only assignee→color map. The mode is persisted.
  - **Filter button** — opens a checkbox popover (per phase value / priority value / owner category); unchecked values hide matching rows and the button reads "Filter (N hidden)", "show all" resets. Filters are **transient — never persisted**, so a reload always shows everything (screenshot use-case: hide sensitive rows, screenshot, then show all).
  - **Double-click** a bar or its label to open a **multi-line comment editor** (modal with a scrollable/resizable textarea and "• bullet" / "1. numbered" insert helpers). Multi-line notes render with line breaks in the tooltip and pass through the Jira CSV inside a quoted field.
  - Hover tooltip shows id, name, date range, aim date, phase, priority, assignee, owner, description, comment.
- **Sticky notes** — floating "+ sticky" button, click-anywhere placement, drag by header, `◐` cycles color, `×` deletes.
- **Jira CSV** button — copies a CSV suitable for Jira bulk issue import (columns `Summary, Description, Assignee, Labels, Start date, Due date, Target month`; dates ISO, computed from the timeline base month; `Labels` includes the phase value and `prio-<value>` alongside the kebab-cased owner; rows with empty labels skipped) to the clipboard.
- **Copy plan** button — puts the full state (bars, labels, owners, assignees, phase, priority, aims, comments, legend, color mode, row order, stickies, decisions) as JSON on the clipboard so the user can paste it back to you.
- **Reset** button — clears browser-saved edits and restores the baked-in plan.
- Full **localStorage persistence** and **light/dark theme** support.

## Procedure

### Step 1 — Parse the invocation

The argument is the **meeting title**. Optionally the user lists workstream / row names (comma- or newline-separated, or described in prose — extract them).

- **Title**: everything that reads as the meeting name. If none given, use `Meeting Worksheet`.
- **Rows**: the workstreams to seed the Gantt with.
  - If the user named workstreams, make one row per named workstream, plus 2-3 `empty` placeholder rows at the end for ideas added live.
  - If the user gave no rows, seed **6 generic placeholder rows** (`Workstream 1..6`) **plus 3 empty rows**.

### Step 2 — Build the ROWS array

Each row is a JS object literal on its own line. Fields:

- `id` — short tag shown before the label (e.g. `"W1"`, `"A"`). Optional; omit or `""` for no tag.
- `label` — the row name (string).
- `start` — month offset where the bar begins: `0` = the current month, up to `18`.
- `dur` — length in months (>= 0.5).
- `desc` — one-line description shown in the hover tooltip (string; escape `"` as needed).
- `track` — set to `"empty"` for an unnamed dashed placeholder row; omit otherwise.

Stagger the `start`/`dur` values so bars don't all stack in the same columns (spread them across the 18 months). Example row literals:

```js
    {id:"W1", label:"Discovery & scoping", start:0, dur:3, desc:"Interviews, requirements, success criteria."},
    {id:"W2", label:"Build phase 1",       start:2, dur:5, desc:"First deliverable slice."},
    {id:"",   label:"", start:4, dur:3, desc:"Empty slot — click the label to name a new idea.", track:"empty"},
```

For each **empty** row use exactly:
`{id:"", label:"", start:<n>, dur:3, desc:"Empty slot — click the label to name a new idea, drag the bar to schedule it.", track:"empty"}`
with staggered `start` values.

### Step 3 — Substitute and write the file

Read `assets/template.html` (in this skill directory) and replace:

- `{{TITLE}}` → the meeting title (appears 3×: `<title>`, masthead `<h1>`, footer). Replace **all** occurrences.
- `{{DATE}}` → today's date as `YYYY-MM-DD` (appears 2×). Replace all.
- `{{STORAGE_KEY}}` → a **unique slug** for this canvas: `mc-<date>-<title-slug>` (e.g. `mc-20260707-q3-planning`). Appears in several `localStorage` keys (`-gantt`, `-stickies`, `-decisions`). Replace **all** occurrences. **This must be unique per generated canvas** so two canvases never clobber each other's saved state.
- `/* ROWS_JSON */` → the row object literals from Step 2 (comma-separated, one per line).

Write the result to `./tmp/meetings/<date>-<title-slug>.html` (create the directory). `tmp/` is commonly gitignored — if the repo has no `tmp/` and you cannot create one, write to the session scratchpad directory instead. Use an absolute path when writing.

### Step 4 — Publish and explain the loop

Publish the written file with the **Artifact** tool: pass its path, `favicon "🗒️"`, and a one-sentence `description`. Keep the same file path on later redeploys so it updates the **same** artifact URL.

Then tell the user the edit loop, briefly:

1. Open the artifact and edit live — drag bars, paint owners, add stickies, type decisions. **Everything auto-saves in the browser.**
2. When they want the state made permanent, click **Copy plan** and paste the JSON back to you.
3. You bake that JSON into the ROWS array + defaults in the source file and **republish to the same artifact URL**.
4. After baking, they click **Reset** in the browser so the (now up-to-date) baked-in plan replaces the stale browser copy.

## Baking a "Copy plan" JSON back in

When the user pastes the JSON: for each row key, update the matching ROWS object's `start`, `dur`, `label`, `assignee`, and (if `owner` is set) leave a comment noting the owner; carry `note` into `desc` or a comment as appropriate (notes may be multi-line). The pasted `phase`/`priority` labels map back to `st.trk` (`Early`→`early`, `Late`→`late`) and `st.prio` (`Top/Mid/Low/Backlog`→`top/mid/low/back`) — set them as saved defaults per row only if you want the baked-in plan to keep those tags. Reorder the ROWS array to match the top-level `__order` array (list of row keys in display order), and turn any `extra<N>` keys in it into real named ROWS entries. Update `LEGEND_DEFAULT` from the `legend` array; if the `slots` array differs from `[1,2,3,…]`, set the template's `legendSlots` default to match so recolored swatches persist. Set the initial `colorMode` from the top-level `mode` field if you want the baked-in default to match. Recreate stickies/decisions only if you want them baked in (usually the user just wants the roadmap persisted). Rewrite the file and republish to the same URL.

## Creating Jira issues directly (optional, if Atlassian MCP is connected)

The "Jira CSV" button is the always-available path (Jira → System → External System Import → CSV). If the user has the **Atlassian MCP** tools connected, you can instead create the issues directly from the pasted Copy-plan JSON: map each non-empty row to an issue (Summary = `label`, Description = `note`/desc, Assignee = `assignee`, Labels = kebab-cased `owner`, Start/Due dates computed from `start`/`dur` against the timeline base month). Confirm the target project/board with the user before creating issues.

## Notes

- The file is fully self-contained (no external assets) so it works in the Artifact viewer's strict CSP.
- A disabled **live-transcript (Web Speech API)** block is left commented in the template for a future iteration; it is off because mic/CSP behavior in the artifact viewer is unverified. See the comment in the template to enable and test in Chrome.

## Regression telemetry (new-machine convention, 2026-07-26)

Once per session before first use, run:
`python3 ~/.claude/skills/meeting-canvas/tests/run_tests.py --auto --model <your-model-id>`
(no-ops after 8 recorded runs per model+version; `--report` prints the matrix).
If any test FAILS, tell the user before generating a canvas.
