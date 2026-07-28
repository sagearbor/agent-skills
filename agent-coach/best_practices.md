# Agent-coach best practices (rubric v1)

<!-- This file is BOTH the human reference AND the rubric fed to the scoring
model. Edit here to teach the coach new things. Snapshot before big edits:
`agent_coach.py rules-snapshot`. Old versions live in archive/ so a bad edit
is one `rules-revert <date>` away. Each rule: id | category | flag-when | suggest. -->

Coaching target: the **person driving an AI coding agent** (Claude Code, Codex,
Cursor…), not the agent. These are the habits that make a new user effective.

1. **model-selection** — a heavyweight model (Opus/Fable/GPT-5-high) was used for
   a trivial task (a one/few-line edit, rename, typo, simple question) with no
   real reasoning needed. → *Suggest Sonnet or Haiku — faster and cheaper for
   small work.*

2. **plan-first** — a large or multi-file change was made with no exploration or
   plan beforehand. → *Suggest Plan mode (shift+tab) before multi-file work so
   the approach is agreed before code moves.*

3. **use-skills** — work was done by hand that an available skill/command likely
   covers (converting docs, security review, etc.). → *Suggest checking for a
   skill first.*

4. **clear-prompt** — the request was vague or underspecified, causing the agent
   to guess, thrash, or ask many clarifying questions. → *Suggest stating the
   goal, constraints, and done-criteria up front.*

5. **delegate-search** — a broad multi-file search or many reads were done inline,
   bloating context, instead of delegating to a subagent. → *Suggest a subagent
   for wide searches so the main context stays lean.*

6. **verify-before-done** — success was claimed without running tests, a build, or
   otherwise exercising the change. → *Suggest verifying (run tests / the app)
   before declaring done.*

7. **avoid-thrash** — the same file was read/edited repeatedly or the same failed
   approach retried without stepping back. → *Suggest pausing to re-read the
   error or ask for a fresh diagnosis.*

8. **protect-secrets** — a `.env` or credential file was read/edited, or a secret
   was pasted into the chat. → *Never put secrets in prompts; reference the file,
   don't open it.*

9. **fresh-context** — one long session mixed several unrelated tasks, polluting
   context. → *Suggest /clear or a new session when switching topics.*

10. **small-batches** — a huge multi-part change was requested in one shot instead
    of iterating in reviewable steps. → *Suggest breaking large asks into smaller,
    verifiable increments.*
