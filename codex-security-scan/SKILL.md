---
name: codex-security-scan
description: Run a real security scan on one repo or many, using OpenAI's open-source Codex Security methodology (threat model → discovery → validation → attack-path → SARIF) driven entirely by Claude, with NO OpenAI account or API key. Findings accumulate in one cross-repo SQLite workbench and render as a self-contained HTML dashboard. Use when asked to security scan / audit / pentest-review a repository, find vulnerabilities, check for OWASP or CWE issues, produce SARIF for CI, track security findings across repos, or build a security posture dashboard.
---

# Codex Security Scan (Claude-driven)

Upstream `@openai/codex-security` is Apache-2.0 and ships its whole methodology
as prose: 13 skills, reference docs, JSON schemas, a SQLite workbench, a SARIF
finalizer. Only its *runner* is OpenAI-bound. This skill swaps the runner for
**you** — Claude does the security reasoning, the upstream workbench does the
bookkeeping, and **no code and no credentials leave the machine**.

Upstream is **not vendored**. `csec.py` installs it from npm into a cache, so
their improvements arrive on their own. `tests/contract_test.py` fails loudly
if a release breaks an assumption this skill relies on.

## Rules

0. **Regression telemetry (once per session).** Before the first scan, run
   `python3 <skill>/tests/run_tests.py --auto --model <your-model-id>`
   (e.g. `--model claude-opus-5`). `--auto` records to the ledger only until 8
   runs exist for the current (model, skill version) pair, then exits
   instantly. If any test FAILS, stop and tell the user. `--report` prints the
   model × version matrix.
1. **Never hand-write `sealedAt` or `artifacts` into `scan-manifest.json`.**
   Author an *unsealed* draft; `csec.py finish` lets the workbench seal it. A
   pre-sealed manifest is rejected with a confusing timestamp error.
2. **Report only what the code shows.** No assumed deployment, no assumed
   public route. Keep the source, the broken control, and the sink. A safe
   neighbouring path does not prove this path is safe.
3. **Every finding needs a real location** — file plus line range you actually
   read. No location, no finding.

## Setup (once per machine)

```bash
python3 <skill>/csec.py doctor
```

Installs upstream into `~/.cache/codex-security-skill`, creates the shared
workbench at `~/.codex-security-state`, and prints `pluginRoot`. Needs `npm`
and Python 3.10+. Override paths with `CODEX_SECURITY_SKILL_CACHE` and
`CODEX_SECURITY_STATE_DIR`.

## Scanning one repository

**Step 1 — open a scan.**

```bash
python3 <skill>/csec.py start --target /path/to/repo --scope .
```

Returns `scanId`, `scanDir`, `filesTotal`, and writes `recipe.json` into the
scan dir. Use `--scope src/` to scope to a subtree.

**Step 2 — do the actual security review.** Read the upstream methodology and
follow it. It lives at `<pluginRoot>/skills/`:

| Upstream skill | Read it for |
|---|---|
| `threat-model/SKILL.md` | assets, trust boundaries, attacker capabilities |
| `security-scan/SKILL.md` | the standard repo pass and its file-inventory discipline |
| `finding-discovery/SKILL.md` | how to turn suspicion into a candidate with evidence |
| `validation/SKILL.md` | deciding reportable vs rejected vs deferred |
| `attack-path-analysis/SKILL.md` | reachability and severity |
| `security-diff-scan/SKILL.md` | PR / branch / working-tree diffs instead of a whole repo |
| `triage-finding/`, `fix-finding/` | triaging and remediating an existing finding |

Also read `<pluginRoot>/references/security-guidance.md` and
`shared-hard-rules.md` — they carry the reporting bar.

Work the repo file by file. Inventory what is in scope, review every file, and
keep a candidate list. Then validate each candidate and drop the ones you
cannot substantiate. **Prefer few, real, well-evidenced findings over volume.**

**Step 3 — write `analysis.json`** (anywhere; pass its path in step 4):

```json
{
  "threatModel": {"summary": "What this service is and who can reach it."},
  "findings": [{
    "ruleId": "path-traversal.archive-extraction",
    "anchor": "archive-entry-write-without-containment",
    "title": "Zip extraction writes entries without containment checks",
    "summary": "Entry names are joined to the destination and written without verifying the resolved path stays inside it, so a crafted archive overwrites files outside the extraction root.",
    "severity": "high",
    "confidence": "high",
    "rationale": "Entry name flows from namelist() into join() and open() with no normalization.",
    "category": "path-traversal",
    "cwe": ["CWE-22"],
    "locations": [{"path": "src/extract.py", "startLine": 7, "endLine": 9, "role": "sink"}],
    "remediation": "Resolve with realpath and reject entries outside dest_dir."
  }],
  "surfaces": [{
    "id": "surface_archive_extraction", "label": "Archive extraction",
    "disposition": "reported", "riskArea": "path-traversal"
  }],
  "completeness": "complete"
}
```

Enums are enforced — anything else is rejected:
`severity` = critical | high | medium | low | info ·
`disposition` = reported | no_issue_found | rejected | not_applicable | needs_follow_up ·
`completeness` = complete | partial | unknown ·
`role` = sink | source | supporting.

Surfaces are your coverage claim: one row per area you reviewed, including the
ones that came back clean (`no_issue_found`). A repo with zero findings still
needs surfaces, otherwise the scan claims no coverage.

**Step 4 — seal and register.**

```bash
python3 <skill>/csec.py finish --scan-dir "<scanDir>" --analysis analysis.json
```

Validates against upstream schemas, seals the manifest, generates `report.md`
and `exports/results.sarif`, and registers the findings in the shared
workbench. Report the severity counts and the `report.md` path to the user.

## Scanning many repositories

Run steps 1–4 per repo against one shared `CODEX_SECURITY_STATE_DIR`. Findings
accumulate across repos and across sessions — that is the point.

For more than ~3 repos, dispatch one subagent per repo (they are fully
independent) and have each do steps 1–4 for its own repo. Tell each subagent
the skill path and the shared state dir. Then render the dashboard once:

```bash
python3 <skill>/csec.py dashboard --output security-dashboard.html \
  --title "Security posture"
```

Self-contained HTML — no network, opens anywhere, light and dark. Use
`csec.py list` for the same data as JSON.

## Translating upstream's Codex-isms

The upstream prose is written for OpenAI's Codex CLI. Read it through this map:

| Upstream says | Here it means |
|---|---|
| `$validation`, `$threat-model` | read that skill's `SKILL.md` under `<pluginRoot>/skills/` and follow it yourself |
| MCP tools (`open_codex_security_workspace`, `complete_codex_security_scan`, `await_codex_security_scan_start`) | unavailable — take the **prompt-only path** the SKILL.md documents; `csec.py` covers it |
| "In the Codex desktop app…" | not applicable; skip to the prompt-only branch |
| `<python_command> <plugin_dir>/scripts/…` | `python3 <pluginRoot>/scripts/…` |
| `$CODEX_SECURITY_PLUGIN_ROOT` | the `pluginRoot` from `csec.py doctor` |
| subagent fan-out / deep-scan queues | optional; use Claude subagents, or do a single pass |

Ignore `deep-security-scan` unless explicitly asked — it assumes worker queues
and MCP orchestration that the prompt-only path does not provide.

## Attribution

Wraps [openai/codex-security](https://github.com/openai/codex-security)
(Apache-2.0). Upstream is fetched from npm at runtime and is not redistributed
here; see `NOTICE`. Not affiliated with or endorsed by OpenAI.
