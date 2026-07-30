#!/usr/bin/env python3
"""PreToolUse hook — block any `git commit` Claude Code runs that carries a secret.

This is the layer that needs no setup: installing the plugin installs it. It
covers the agent's commits on any machine, in any repo, with no git config and
no gitleaks binary required.

It also catches `--no-verify`, which by definition slips past the git-level
pre-commit hook — so the two layers are genuinely complementary rather than
redundant.

Fails OPEN. A hook that breaks someone's session on a parsing edge case would
get uninstalled within a day, and an uninstalled hook protects nothing.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "secret_guard.py"

# `git commit` possibly behind cd/&&/;, but not inside an obvious string.
_COMMIT = re.compile(r"(?:^|[;&|]|\s)git(?:\s+-[^\s]+)*\s+commit\b")
# -a/-am/--all stage tracked modifications at commit time, so --cached is blind.
_ALL = re.compile(r"\s-(?:[a-zA-Z]*a[a-zA-Z]*)\b|\s--all\b")
_AMEND = re.compile(r"\s--amend\b")


def deny(reason: str) -> None:
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    if payload.get("tool_name") != "Bash":
        return
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not _COMMIT.search(cmd):
        return

    cwd = payload.get("cwd") or "."
    mode = "all" if (_ALL.search(cmd) or _AMEND.search(cmd)) else "staged"
    try:
        p = subprocess.run(
            [sys.executable, str(GUARD), "scan", "--mode", mode,
             "--repo", cwd, "--json"],
            capture_output=True, text=True, timeout=25)
        findings = json.loads(p.stdout or "[]")
    except Exception:
        return  # fail open — never break the session

    if not findings:
        return

    lines = [f"  {f['rule']}  {f.get('file','?')}"
             f"{':' + str(f['line']) if f.get('line') else ''}"
             f"  {f.get('match','')}" for f in findings[:8]]
    more = f"\n  … and {len(findings) - 8} more" if len(findings) > 8 else ""
    bypass = ("\n\nThis also fires on --no-verify, which is why it exists."
              if "--no-verify" in cmd else "")
    deny(
        f"secret-guard blocked this commit: {len(findings)} possible credential"
        f"{'s' if len(findings) != 1 else ''} in the content being committed."
        f"\n\n" + "\n".join(lines) + more + bypass +
        "\n\nDo NOT just delete the line and re-commit — the value is still in "
        "the working tree, and if it was ever committed before it is already in "
        "history and needs rotating, not deleting."
        "\n\nFix: move the value into an untracked .env and read it via "
        "os.environ / process.env. For a test, skip when the variable is unset "
        "rather than hardcoding a real key."
        "\n\nIf it is a false positive, tell the user to run:"
        f"\n  python3 {GUARD} allow <fingerprint>")


if __name__ == "__main__":
    main()
