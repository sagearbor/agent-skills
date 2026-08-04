---
description: Turn the agent-coach on/off, check status, or see your coaching dashboard
allowed-tools: ["Bash", "Read"]
---

# agent-coach control

The user typed `/coach $ARGUMENTS`. Run the matching command and report
the output plainly. Do NOT lecture them about coaching — just do it.

Resolve the script path once (works for both the marketplace install and a
direct clone; take the first that exists):

```
ls ~/.claude/plugins/cache/*/research-skills/*/agent-coach/agent_coach.py 2>/dev/null | tail -1
ls ~/.claude/skills/agent-coach/agent_coach.py 2>/dev/null
```

Then map `$ARGUMENTS` to a subcommand:

| Argument | Run | Then say |
|---|---|---|
| `off`, `stop`, `disable`, `quiet` | `<script> off` | confirmed off, and how to turn it back on |
| `on`, `start`, `enable` | `<script> on` | confirmed on; notes appear one turn later |
| `status`, (empty) | `<script> status` then `<script> doctor` | ON/OFF, thresholds, whether it is actually firing |
| `quieter` / `louder` | `<script> quieter` / `louder` | all thresholds nudged |
| `dashboard` | `<script> dashboard --open` (no path — writes to its own state dir) | where the file is |
| `ab` | `<script> ab` | side-by-side A/B tally: separate-call vs same-call coaching |
| `ab on` / `ab off` | `<script> ab on|off` | arm/disarm the comparison |
| `courses` + anything | `<script> courses <rest>` | pass through verbatim |
| `project` + a code | `<script> project <code>` — run it **from the user's current directory** | which repo got tagged |
| anything else | `<script> --help` | list the real subcommands |

Notes:
- If no script is found, tell them the plugin is not installed and stop.
- Never invent flags. If unsure, run `<script> --help` and show it.
