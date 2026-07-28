---
name: lmstudio-panel
description: Control a local LM Studio server (serve, load/unload models, model-major batching) and log EVERY local-LLM call to a cross-repo usage ledger (input/output/reasoning tokens, wall-time, project, user, machine) with burst/window reporting. Use whenever a task calls local models via LM Studio in ANY repo, or when asked about local-LLM usage, token ledgers, or hardware-cost reporting.
---

# lmstudio-panel

**Rule 0 (telemetry):** once per session, before first use, run
`python3 ~/.claude/skills/lmstudio-panel/tests/run_tests.py --auto --model <your-model-id>`
(no-ops after 8 recorded runs per model+version).

**Rule 0.5 (auto-capture):** the first time this skill is used in a session,
also run `python3 ~/.claude/skills/lmstudio-panel/lmstudio_panel.py ingest
claude-code` (idempotent, seconds). Zero-thought automation for a whole org:
ship this as a plugin with a SessionStart hook, or add to settings.json:

```json
"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command":
  "python3 ~/.claude/skills/lmstudio-panel/lmstudio_panel.py ingest claude-code >/dev/null 2>&1 &"}]}]}
```

Capture boundary (by design): usage is recorded where our tooling runs —
instrumented pipelines, skill calls, assistant-session transcripts. Someone
chatting directly in the LM Studio app is invisible to the ledger, and
that's accepted: no attribution we could trust exists there anyway.

## What this skill provides

`lmstudio_panel.py` — stdlib-only Python (no pip installs), usable as a module
or CLI by any repo, any AI tool, or a human:

| Command | Purpose |
|---|---|
| `serve` | ensure the LM Studio server is running (port 1234) |
| `models` | list downloaded models |
| `load <m>` / `unload` | model-major batching primitives (one model at a time) |
| `chat --model M --prompt P [--task-tag T]` | one completion call, ALWAYS ledger-logged |
| `smoke --model M` | judge-shaped health check (expects VIOLATION) |
| `report [--by project\|model\|user\|project-model] [--windows] [--days N]` | cross-repo usage rollup + burst analysis |
| `report --html [PATH]` | self-contained graphical dashboard (works from any dir/repo) |
| `prices update` / `prices list` | append dated hosted rates (LiteLLM table) for models seen in the ledger; reports join **as-of** each event's date — entries are never edited, so old reports stay reproducible |

## Rules

1. **Never call the local server without logging.** Use `chat()` (auto-logs)
   or call `log_usage()` yourself after any raw HTTP call. The ledger is the
   hardware-cost-justification evidence; silent calls destroy it.
2. **Ledger location:** `$LLM_TOKEN_LEDGER_DIR` (default `~/.llm_token_ledger/`).
   One file per (user, machine): `lmstudio-<user>-<machine>.jsonl` — safe to
   point at a shared location, no write collisions. Orgs: set the env var to
   the shared path; `report` aggregates every file it sees.
3. **Model-major batching:** for multi-model sweeps, iterate model → all work
   → `unload` → next model. Never leave two 60GB-class models loaded.
4. **reasoning_tokens is `null`** when a model doesn't report it — never
   fabricate zeros.
5. Events are raw + timestamped; derive new views (windows, bursts, per-dev)
   at report time — do not pre-aggregate into the ledger.

## Python use from a repo

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.home() / ".claude/skills/lmstudio-panel"))
from lmstudio_panel import chat, load_model, unload_all, log_usage

r = chat("qwen/qwen3-32b", [{"role": "user", "content": "..."}],
         task_tag="expansion-validation")   # ledger entry written automatically
```

For pipelines that already have their own OpenAI client (e.g. an
`LLM_PROVIDER=local` path), call `log_usage(model, usage_dict, duration_s,
task_tag=...)` right after each response instead.

## Instrumenting any repo's own LLM client (3 lines)

A repo does NOT need to route calls through this skill to be counted — hook
its client's success path once:

```python
from lmstudio_panel import log_usage   # (sys.path trick above)
# right after each successful completion:
log_usage(model, response_usage_dict, duration_s, task_tag="my-pipeline")
```

Repos with their own writer (like llm-as-judge's `token_ledger.py`) are also
fine AS-IS: `report`/`report --html` read **every `*.jsonl` in the ledger
dir** and normalize both schemas (`prompt_tokens/completion_tokens` and
`tokens_in/tokens_out`). One drop-dir, any writer, one merged report — set
`LLM_TOKEN_LEDGER_DIR` to a shared location for org-wide rollups.

