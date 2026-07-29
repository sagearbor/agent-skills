---
name: llm-usage-ledger
description: Track EVERY LLM usage stream — cloud API calls, Claude Code/Codex subscription transcripts (ingest), and local models — in one append-only cross-repo token ledger with dated as-of price series, burst analysis, and interactive $-vs-tokens HTML dashboards. No local-model software required. Use whenever asked about LLM usage, token ledgers, spend/cost reporting, subscription-usage accounting, or price series in ANY repo.
---

# llm-usage-ledger

**Rule 0 (telemetry):** once per session, before first use, run
`python3 ~/.claude/skills/llm-usage-ledger/tests/run_tests.py --auto --model <your-model-id>`
(no-ops after 8 recorded runs per model+version).

**Rule 0.5 (auto-capture):** the first time this skill is used in a session,
also run `python3 ~/.claude/skills/llm-usage-ledger/llm_usage_ledger.py ingest
claude-code` (idempotent, seconds). Zero-thought automation for a whole org:
ship this as a plugin with a SessionStart hook, or add to settings.json:

```json
"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command":
  "python3 ~/.claude/skills/llm-usage-ledger/llm_usage_ledger.py ingest claude-code >/dev/null 2>&1 &"}]}]}
```

Capture boundary (by design): usage is recorded where our tooling runs —
instrumented pipelines, skill calls, assistant-session transcripts. Someone
chatting directly in a vendor's own app is invisible to the ledger, and
that's accepted: no attribution we could trust exists there anyway.

## What this skill provides

`llm_usage_ledger.py` — stdlib-only Python (no pip installs), usable as a
module or CLI by any repo, any AI tool, or a human. Most org users have NO
local models: this skill stands alone — it needs no LM Studio or any other
local-model software. (Local-model server control lives in the separate
`lmstudio-panel` skill, which delegates all its accounting here.)

| Command | Purpose |
|---|---|
| `report [--by project\|model\|user\|project-model] [--windows] [--days N]` | cross-repo usage rollup + burst analysis |
| `report --html [PATH]` | self-contained interactive $-vs-tokens dashboard (works from any dir/repo) |
| `prices update` / `prices list` | append dated hosted rates (LiteLLM table) for models seen in the ledger; reports join **as-of** each event's date — entries are never edited, so old reports stay reproducible |
| `ingest claude-code` / `ingest codex` | derive subscription-plan usage (flat-fee tools) from local transcripts into the ledger — idempotent, merged by message UUID |

## Rules

1. **Never make an LLM call without logging.** Call `log_usage()` right after
   any completion (see the 3-line recipe below). The ledger is the
   cost-justification evidence; silent calls destroy it.
2. **Ledger location:** `$LLM_TOKEN_LEDGER_DIR` (default `~/.llm_token_ledger/`).
   One file per (user, machine): `lmstudio-<user>-<machine>.jsonl` (prefix
   kept from this code's original home for back-compat of existing data) —
   safe to point at a shared location, no write collisions. Orgs: set the
   env var to the shared path; `report` aggregates every file it sees.
3. **reasoning_tokens is `null`** when a model doesn't report it — never
   fabricate zeros.
4. Events are raw + timestamped; derive new views (windows, bursts, per-dev)
   at report time — do not pre-aggregate into the ledger.
5. **Subscription events stay separate:** ingest records carry
   `subscription: true` and are priced at API rates as "what the plan
   absorbed" — never mixed into local savings or cloud spend.

## Instrumenting any repo's own LLM client (3 lines)

A repo does NOT need to route calls through this skill to be counted — hook
its client's success path once:

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.home() / ".claude/skills/llm-usage-ledger"))
from llm_usage_ledger import log_usage
# right after each successful completion:
log_usage(model, response_usage_dict, duration_s, task_tag="my-pipeline",
          provider="azure")   # provider defaults to "lmstudio" (local)
```

Repos with their own writer (like llm-as-judge's `token_ledger.py`) are also
fine AS-IS: `report`/`report --html` read **every `*.jsonl` in the ledger
dir** and normalize both schemas (`prompt_tokens/completion_tokens` and
`tokens_in/tokens_out`). One drop-dir, any writer, one merged report — set
`LLM_TOKEN_LEDGER_DIR` to a shared location for org-wide rollups.
