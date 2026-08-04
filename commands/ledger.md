---
description: LLM token spend — open the dashboard, report usage, ingest transcripts, manage prices
allowed-tools: ["Bash", "Read"]
---

# llm-usage-ledger control

The user typed `/ledger $ARGUMENTS`. Run the matching command and report the
output plainly. Do NOT lecture them about tracking — just do it.

Resolve the script path once (take the first that exists):

```
ls ~/.claude/plugins/cache/*/research-skills/*/llm-usage-ledger/llm_usage_ledger.py 2>/dev/null | tail -1
ls ~/.claude/skills/llm-usage-ledger/llm_usage_ledger.py 2>/dev/null
```

Map `$ARGUMENTS` to a subcommand:

| Argument | Run | Then say |
|---|---|---|
| `dashboard`, (empty) | `<script> report --html ~/llm-spend.html --days 60 --open` | the path, and that it opened in the browser |
| `dashboard <N>` | same with `--days <N>` | ditto |
| `report` | `<script> report --days 60` | the table |
| `by project` / `by user` / `by model` | `<script> report --days 60 --by project\|user\|model` | the table |
| `ingest` | `<script> ingest claude-code` | how many events were added (idempotent) |
| `prices` | `<script> prices list` | first ~10 rows and the total count |
| `prices update` | `<script> prices update` | how many rows appended |
| `where` | `echo ${LLM_TOKEN_LEDGER_DIR:-~/.llm_token_ledger}` then `ls -la` that dir | which dir is active, and whether it is shared or local |
| anything else | `<script> --help` | list the real subcommands |

Notes:
- Always report the ABSOLUTE path of any HTML written, so the user can click it.
- If no script is found, say the plugin is not installed and stop.
- Never invent flags. If unsure, run `<script> --help` and show it.
