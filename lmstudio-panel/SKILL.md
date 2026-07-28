---
name: lmstudio-panel
description: Control a local LM Studio server — serve, list/load/unload models, model-major batching, and chat/smoke calls that are ALWAYS ledger-logged via the llm-usage-ledger skill. Use whenever a task calls local models via LM Studio in ANY repo. For usage reporting, token ledgers, prices, or dashboards use the llm-usage-ledger skill (this skill delegates all accounting there).
---

# lmstudio-panel

**Rule 0 (telemetry):** once per session, before first use, run
`python3 ~/.claude/skills/lmstudio-panel/tests/run_tests.py --auto --model <your-model-id>`
(no-ops after 8 recorded runs per model+version).

**Dependency:** this skill does server control ONLY. ALL usage accounting
(ledger writes, reports, price series, subscription ingest, HTML dashboards)
lives in the sibling **llm-usage-ledger** skill, which must be installed from
the same skills repo/marketplace — `chat()` imports `log_usage` from it.
Org users with NO local models don't need this skill at all: go straight to
**llm-usage-ledger** for all usage tracking and reporting.

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

Reporting moved: `report`, `report --html`, `prices`, and `ingest` are now
`python3 ~/.claude/skills/llm-usage-ledger/llm_usage_ledger.py ...` — see
that skill's SKILL.md.

## Rules

1. **Never call the local server without logging.** Use `chat()` (auto-logs
   via llm-usage-ledger) or call `log_usage()` yourself after any raw HTTP
   call. The ledger is the hardware-cost-justification evidence; silent
   calls destroy it.
2. **Model-major batching:** for multi-model sweeps, iterate model → all work
   → `unload` → next model. Never leave two 60GB-class models loaded.
3. Ledger location, schemas, prices, and report rules: see the
   llm-usage-ledger SKILL.md.

## Python use from a repo

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.home() / ".claude/skills/lmstudio-panel"))
from lmstudio_panel import chat, load_model, unload_all, log_usage

r = chat("qwen/qwen3-32b", [{"role": "user", "content": "..."}],
         task_tag="expansion-validation")   # ledger entry written automatically
```

`log_usage` (and the other accounting names: `read_ledgers`, `aggregate`,
`hourly_windows`, `as_of_price`, `html_report`, ...) are still importable
from `lmstudio_panel` as thin back-compat re-exports, but new code should
import them from `llm_usage_ledger` directly.
