---
name: secret-guard
description: Stop API keys, tokens, and passwords from ever reaching git. Ships a PreToolUse hook that blocks any commit Claude Code makes containing a credential (no setup — installing the plugin is the install), plus a one-command global git hook covering every commit from every tool in every repo. Also audits what already leaked and walks the rotation. No API key, no model call, no per-commit cost. Use when onboarding people to Claude Code, setting up a new machine or repo, checking whether secrets are committed, after finding a leaked key, or when asked about gitleaks, pre-commit hooks, secret scanning, or credential rotation.
---

# secret-guard

Credentials reach git through three doors, and `.gitignore` only closes one:

1. **Backup filenames dodge the pattern.** `.gitignore` with `.env` and `*.env`
   does not match `.env.bak` or `envBU01p2y`. Both get committed.
2. **Source files must be tracked.** A test or debug script needs a working key
   to run, someone pastes a real one in. No filename rule can ever help.
3. **A document about the secret copies the secret.** A plan or issue quotes the
   offending line verbatim. This one catches almost everybody.

Only content scanning closes all three. That is what this does.

## Two layers, deliberately

| Layer | Covers | Setup |
|---|---|---|
| **Agent** — `PreToolUse` hook | every `git commit` **Claude Code** runs, including `--no-verify` | **none** — installing the plugin is the install |
| **Machine** — global `core.hooksPath` | every commit from **any** tool, in **every** repo, including clones made next year | one command |

They are complementary, not redundant. The agent layer catches `--no-verify`,
which by definition slips past a git hook. The machine layer catches commits
made from a terminal or IDE, which the agent layer never sees.

`.git/hooks/` is never committed and never cloned, which is why the machine
layer uses **global** `core.hooksPath` rather than per-repo installs. A
`.pre-commit-config.yaml` sitting in a repo that nobody ran protects nothing —
that is the single most common failure.

## Cost and dependencies: none

`gitleaks` is a Go binary doing regex and entropy matching. No model call, no
network, no `ANTHROPIC_API_KEY`, no subscription draw — identical protection for
subscription-only users. When gitleaks is absent the bundled rules in
`patterns.json` run instead, so a fresh machine is never unprotected. Install
gitleaks later for ~150 rules instead of ~12; nothing else changes.

## Commands

```bash
python3 <skill>/secret_guard.py status              # which layers are live
python3 <skill>/secret_guard.py install --global    # machine layer
python3 <skill>/secret_guard.py scan                # staged content, here
python3 <skill>/secret_guard.py history ~/code/*/   # what already leaked
python3 <skill>/secret_guard.py allow <fingerprint> # baseline a false positive
python3 <skill>/secret_guard.py uninstall           # restores previous hooksPath
```

`scan` exits 1 on findings. Findings print **redacted** (`7fca…(32 chars)`) with a
stable fingerprint — the value itself is never echoed, because printing it is
failure mode 3.

## When someone hits a block

Say this, in this order — the order matters:

1. **Do not just delete the line and re-commit.** If the value was ever committed
   before, it is already in history; deleting changes nothing and creates false
   confidence.
2. **Was it ever committed?** `git log -S'<fragment>' --oneline --all`. If yes,
   the credential must be **rotated**. History rewriting is optional; rotation is
   not.
3. **Who else uses it?** Check whether the key belongs to a shared resource
   before revoking — rotating a shared sandbox key breaks other people's work.
   Most providers issue two keys so you can roll one at a time.
4. **Then fix the code.** Move it to an untracked `.env` read via
   `os.environ` / `process.env`. For tests, skip when unset rather than
   hardcoding:
   `@pytest.mark.skipif(not os.getenv("X"), reason="no creds")`.
5. **False positive?** `secret_guard.py allow <fingerprint>` — baselines that one
   value only, in `~/.secret-guard/baseline.json`, without weakening the rule.

## Triage: real key vs fixture

Content scanners flag shapes, not meaning. Judge with:

- **Does it have name context?** `AZURE_OPENAI_API_KEY = "..."` is a credential;
  a bare 32-hex string in a checksum table is not.
- **Is it a placeholder?** `your_key_here`, `changeme`, `xxxx`, all-one-character
  — already filtered, but new variants appear.
- **Is the file a template?** `.env.example`, `*.sample`, `/fixtures/` — already
  skipped by path.
- **Does it still work?** The decisive test. An expired key is cleanup, not an
  incident. Never test it by pasting into a shell that logs history.

Report the **file and line**, never the value.

## Never do

- **Never commit scan output, baselines, or findings.** They name real paths and
  real variable names. Everything lives in `~/.secret-guard/`, outside any repo.
- **Never quote a secret** in a commit message, issue, plan document, or chat.
  Reference `file:line` and a fingerprint.
- **Never treat a block as noise to route around.** `--no-verify` exists, and the
  agent layer deliberately still catches it.

## Limits — say these plainly

- **Not a control, a speed bump.** A determined user bypasses both layers. The
  only unbypassable layer is server-side push protection (free on public GitHub
  repos; paid Advanced Security on private ones). Ask whether your org licenses
  it before designing around its absence.
- **History needs gitleaks.** Without it, `history` scans tracked files at HEAD
  only and says so. A secret deleted in an old commit will be missed.
- **Entropy heuristics miss low-entropy secrets** — a password like
  `hunter2hunter2` has no shape to match.
- **The builtin engine trades recall for precision.** It anchors on
  `NAME = VALUE`, so a key in a bare string (`assert "sk-…" not in out`) or
  assigned to an unusual name (`valid_key = …`) is missed. Loosening this is not
  free: matching on "a secret-ish word appears somewhere on the line" produced
  **38,537 hits across 11 repos**, which is worse than nothing because people
  stop reading. Install gitleaks to get shape-based recall back.
- **Lines over 1000 characters are skipped** — minified bundles and embedded
  data blobs put hundreds of high-entropy tokens on one line. gitleaks handles
  those; the builtin engine declines rather than crying wolf.
