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

## Enable — nothing to install
The plugin ships the Stop hook itself (`hooks/hooks.json`, resolved via
`${CLAUDE_PLUGIN_ROOT}`), so installing the plugin IS the wiring. No setup
command, and nothing to redo when a new version ships.

It is **off until you opt in**, because scoring costs money per turn and this
skill rides in a multi-skill bundle — somebody installing it for `md-convert`
must not silently start paying for coaching. On your first turn after install
you get one notice, then silence:

```
 ● agent-coach is installed but OFF
   Cost: roughly a tenth of a cent per scored turn (one small Haiku call).
   Turn it on:        agent_coach.py on
   Never ask again:   agent_coach.py off
```

Until you run `on`, the hook does one file read and exits — no model call, no
cost. `off` records the decline and never asks again.

**Direct-clone users** (no marketplace) still need `agent_coach.py install`,
which writes a **version-independent launcher** to `~/.agent-coach/coach_hook.py`
and points `settings.json` at that — never at a versioned plugin path, which
would silently die at the next release. `doctor` accepts either route and tells
you which one is active.

## Tune it (natural language works — the model runs these for you)
| Command | Effect |
|---|---|
| `agent_coach.py status` | per-category threshold table |
| `agent_coach.py set <category> <0-1>` | one category (lower = more coaching, 1.0 = silent) |
| `agent_coach.py quieter` / `louder` | nudge ALL thresholds ±0.1 |
| `agent_coach.py off` / `on` | silence / restore |
| `agent_coach.py escalate <0-1>` | send low-certainty calls to a smarter model (Sonnet); 0 = never |
| `agent_coach.py budget <daily-$\|off>` | OPTIONAL daily spend cap (off by default; pauses scoring once hit) |
| `agent_coach.py frequency <n>` | score every Nth turn (1 = every turn); overrides the auto-ramp |
| `agent_coach.py dashboard [out.html]` | usage rollup (aggregates `$AGENT_COACH_SHARED_DIR` too) |
| `agent_coach.py doctor` | health check — is the hook actually firing? |
| `agent_coach.py precision date\|full` | `full` adds wall-clock time to the **local** log only |
| `agent_coach.py project <D1-code\|poc\|skip\|show>` | tag this repo (asked once, at 25 turns) |
| `agent_coach.py ask-after <n>` | change that 25-turn threshold |

## Training pointers (`courses`)
When the **same** habit is missed repeatedly across **distinct sessions**, the
coach points at one specific course instead of only the micro-fix.

| Command | Effect |
|---|---|
| `courses status` | gates, hits per category, per-course state, catalog age |
| `courses preview <category>` | render the note exactly as a user sees it — **changes no state** |
| `courses on` / `off` | course pointers only (micro-fixes unaffected) |
| `courses done <id\|category>` | "already took this" → permanent suppress |
| `courses dismiss <id>` | "not interested" → permanent suppress |
| `courses snooze <days>` | pause all course pointers |
| `courses min-hits <n>` / `cooldown <days>` | tune the two gates |
| `courses watch on` | opt in to "new modules published" news (max 1×/30d) |
| `courses share on` | opt in to sending course events to the org dir (**off** by default) |
| `courses refresh` | re-verify every URL by real HTTP fetch |

**Gates** (on top of the normal severity threshold): ≥3 hits across **distinct
sessions**, one pointer per **7 days** globally, **2 suggestions max** per course
ever, hard stop on dismiss/done.

**Links can never be hallucinated.** `course_map.json` is never sent to the
scoring model — rule text is URL-stripped on the way in (`URL_RE`). The model
decides *whether* a habit was missed; Python decides *what* link to render; and a
course is only suggested if its last real HTTP fetch returned **200**. `courses
refresh` re-checks and stamps `verified_on`; `doctor` warns past 180 days.

**Deliberately unmapped:** `protect-secrets` (the inline warning is the right
response), `verify-before-done`, `avoid-thrash`, `small-batches` (no honest free
course teaches these — a wrong pointer costs more credibility than a missing one).

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

## Org rollup — exactly what is and isn't shared
Every coaching decision is logged to a per-user JSONL (`~/.agent-coach/`). Set
`AGENT_COACH_SHARED_DIR=/path/on/shared/drive` and each user writes their own
file there (per-user filenames → no write collisions) — then
`agent_coach.py dashboard` renders an instant usage chart across everyone.

**Shared** (usernames in plain text — deliberate, this is an onboarding
programme, not anonymous research):

| Field | Why |
|---|---|
| `date`, `dow` | weekend/weekday patterns |
| `gap_s` | seconds since your previous scored turn — **burst analysis without a wall clock** |
| `user`, `machine` | who to help |
| `project`, `d1`, `tier`, `repo` | per-project token variance (the "same budget, 20× the spend" question) |
| `fired`, `scored`, `thrash` | the actual coaching product |
| `usage` | per-person / per-project cost |

**Never shared:**
- `time` (wall-clock). Enabled locally by `precision full`; hard-stripped from the
  shared payload. Analyse your own night-work locally and present the *finding* —
  the raw rows never have to leave your laptop.
- **Course-pointer events**, unless you explicitly run `courses share on`. Training
  completion is tied to raises and assignments, so a shared list of who keeps being
  told to take a course is a de facto competency ranking. Off by default.

**Tokens are activity, not productivity.** The dashboard reports "effort turns"
with `avoid-thrash`-flagged turns *subtracted*, because thrashing produces high
token counts and no output. Pair with merged PRs or days-to-done before drawing
any conclusion about a person.

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
- **Frequency ramp**: scores every turn for the first 3 turns (a quick user who
  only does a few turns gets full coverage), then auto-switches to every 5th
  turn to save cost — announced once, revert with `frequency 1`.
- **Budget cap** (`budget <daily-$>`) is OPTIONAL and OFF by default — turn it
  on only if you want a hard daily ceiling; a coach you find useful keeps running.
- Only substantive turns are scored (pure Q&A skipped). Recursion-guarded.
- The direct-API path is verified by construction (mocked tests); a keyed user
  confirms it live.
