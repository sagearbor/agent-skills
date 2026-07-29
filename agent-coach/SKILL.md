---
name: agent-coach
description: Pair-programming coach for people learning an AI coding agent (Claude Code, Codex, Cursor). A Stop hook scores each finished turn against a best-practices rubric with a cheap model and shows a short note when the user missed something worth improving — model choice, plan-first, using skills, verifying, avoiding thrash. Use when onboarding new agent users, or when asked to enable/tune/report coaching. Runs on the user's existing Claude Code auth (no API keys).
---

# agent-coach

Turns each finished turn into gentle, in-the-moment coaching. Cheap by design:
one small `claude -p --model haiku` call per substantive turn (rides the user's
existing auth — **no API keys, works for everyone**), gated by per-category
thresholds that start low (heavy coaching for a beginner) and **auto-raise as
each habit improves**, so it fades where you've learned and persists where you
haven't.

## Enable (one time)
```
python3 ~/.claude/skills/agent-coach/agent_coach.py install
# restart Claude Code
```
Uninstall: `agent_coach.py uninstall`.

## Tune it (natural language works — the model runs these for you)
| Command | Effect |
|---|---|
| `agent_coach.py status` | per-category threshold table |
| `agent_coach.py set <category> <0-1>` | one category (lower = more coaching, 1.0 = silent) |
| `agent_coach.py quieter` / `louder` | nudge ALL thresholds ±0.1 |
| `agent_coach.py off` / `on` | silence / restore |
| `agent_coach.py escalate <0-1>` | send low-certainty calls to a smarter model (Sonnet); 0 = never |
| `agent_coach.py budget <daily-$\|off>` | pause scoring once daily spend hits the cap (safety for metered users) |
| `agent_coach.py dashboard [out.html]` | usage rollup (aggregates `$AGENT_COACH_SHARED_DIR` too) |

## The rubric
`best_practices.md` — the teachable list, also the scoring rubric. Edit it to
teach new habits. Snapshot before big edits (`rules-snapshot`); old versions live
in `archive/`, so a bad edit is one `rules-revert <date>` away.

## How scoring works
Haiku returns per-rule `severity` (size of the miss) and `certainty`. A note
fires when `severity ≥ threshold[category]`. If `escalation_cutoff > 0`, only
**borderline low-certainty** interventions are re-checked by Sonnet — so a
false positive gets killed before it reaches the user, and you can dial
quality-vs-cost independently of annoyance.

## Org rollup (optional, zero manual effort)
Every coaching decision is logged to a per-user JSONL (`~/.agent-coach/`). Set
`AGENT_COACH_SHARED_DIR=/path/on/shared/drive` and each user writes their own
file there (per-user filenames → no write collisions) — then
`agent_coach.py dashboard` renders an instant usage chart across everyone.

## Rule 0 (telemetry)
Once per session run
`python3 ~/.claude/skills/agent-coach/tests/run_tests.py --auto --model <your-model-id>`.

## Cost & latency (v0.2)
- **Metered users** (an `ANTHROPIC_API_KEY` is set — the common org case): the
  coach makes a **direct minimal Haiku API call (~$0.001, ~1s)** — 30× cheaper
  than routing through `claude -p`.
- **Subscription users** (no key): falls back to `claude -p` (key-free, uses
  existing Claude Code auth, ~$0.03/turn from CC's system-prompt overhead).
- **Zero added latency**: scoring runs in a **detached background process** in
  parallel with the answer; the note appears at the *next* turn. Never blocks.
- **Budget cap** (`budget <daily-$>`) pauses scoring once the day's spend hits
  the cap — a safety valve for metered/capped users.
- Only substantive turns are scored (pure Q&A skipped). Recursion-guarded.
- The direct-API path is verified by construction (mocked tests); a keyed user
  confirms it live.
