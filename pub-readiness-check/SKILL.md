---
name: pub-readiness-check
description: Audit a repo for publication/grant readiness — confirms it has at least a stub of a future paper/manuscript AND that the data the code generates would be enough to populate the paper's figures and tables. Use when checking whether research code will produce publishable results, before/after major experiments, or when prepping a grant. If a paper stub or the figure-supporting data is missing, it drafts stubs in ./tmp/ and asks where to move them. Works in any repo.
---

# Publication Readiness Check

Audit the current repository on two axes and report a clear PASS/GAP verdict for each:

1. **Does a publication stub exist?** (a draft paper / manuscript / methods write-up)
2. **Would the data the code generates be sufficient to fill that paper's figures and tables?**

## Procedure

### Step 1 — Find the publication stub
Search the repo for an existing manuscript/paper artifact. Look (case-insensitive) for:
- Files: `PAPER*`, `MANUSCRIPT*`, `*.tex`, `paper/`, `manuscript/`, `docs/paper*`, `docs/publication*`, `docs/manuscript*`, files containing an `## Abstract` / `## Methods` / `## Results` structure.
- Also check `./tmp/` for a previously generated stub (e.g. `PAPER_STUB.md`).

Report whether a stub was found and where.

### Step 2 — Extract the planned figures/tables
If a stub exists, parse it for every **Figure** and **Table** placeholder and their captions. Build a list of planned exhibits. If captions include a "DATA REQUIRED" / "data needed" note, capture it. If the stub lists no figures, flag that as a gap (a methods/results paper needs exhibits).

### Step 3 — Inventory the data the code actually generates
Inspect the repo's experiment/output surface to see what numbers get produced:
- Training/eval scripts and what metrics they compute and log (e.g. accuracy, kappa, confusion matrices, ROC).
- Where results land: `outputs/`, `results/`, `*.csv`, `*.json`, logged metrics, W&B/MLflow/TensorBoard calls, saved plots/checkpoints.
- Config sweeps / multiple model configurations (needed for any "comparison" figure).

### Step 4 — Cross-check coverage
For each planned figure/table, decide whether the underlying data is (or will be) produced by the current code. Output a coverage table:

| Figure/Table | Data required | Produced by code? | Gap / action |
|---|---|---|---|

Mark each **COVERED**, **PARTIAL**, or **MISSING**. For comparison figures (e.g. "model A vs B vs C"), verify the code can run *all* the compared configurations and log them to a common metric.

### Step 5 — Fill gaps in ./tmp/
- **If no publication stub exists:** draft one at `./tmp/PAPER_STUB.md` using the standard structure for the repo's domain (Title, Abstract, Introduction, Methods, Results with explicit figure/table placeholders + full captions + a "DATA REQUIRED" line per exhibit, Discussion, References). Infer the domain and methods from the codebase (README, configs, model files).
- **If figure-supporting data is missing:** write `./tmp/PUB_DATA_GAPS.md` listing each missing exhibit, the exact metric/output the code must log to populate it, and the concrete code/experiment change needed.

### Step 6 — Report and ask
Summarize: stub PASS/GAP, data-coverage PASS/GAP, and the coverage table. If you created files in `./tmp/`, **ask the user whether to move them** to a sensible in-repo location (propose one based on the repo's layout, e.g. `docs/paper/`, `manuscript/`, or `docs/`). Do not move them without confirmation.

## Notes
- Keep generated stubs domain-appropriate — read the actual code, don't emit a generic template.
- This skill is read-only against the repo except for writing into `./tmp/`. Never modify source or move files without explicit user approval.
- If the repo treats `./tmp/` as gitignored scratch (common), say so when proposing the move.

## Regression note (2026-07-26)

Model-authored skill — no deterministic harness. Planned fixture protocol
(AUDIT 2026-07-26): two mini-repos, (a) stub+outputs → expect PASS verdicts,
(b) no stub → expect GAP; gate on verdict tokens, drafted stubs only in ./tmp/.
Record usage outcomes to ~/.claude/skills/telemetry/ if a harness is added.
