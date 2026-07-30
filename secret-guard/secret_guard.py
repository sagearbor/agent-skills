#!/usr/bin/env python3
"""secret-guard — stop credentials reaching git, on every machine, by default.

Two layers, because they cover different halves:

  agent layer  a PreToolUse hook blocks any `git commit` Claude Code runs.
               Installing the plugin IS the install — zero user action.
  machine layer a global core.hooksPath pre-commit hook covers EVERY commit
               (terminal, IDE, any repo, including clones made next year).

Detection prefers the gitleaks binary (~150 rules). When gitleaks is absent it
falls back to the bundled rules in patterns.json, so a fresh machine is never
unprotected — that is the whole point.

No model calls, no API key, no network. Pure pattern + entropy matching, so it
costs nothing per commit and works identically for subscription-only users.

Subcommands:
  scan            scan staged content (or --range / --stdin); exit 1 on findings
  install         install the machine-layer hook (default: --global)
  uninstall       remove it, restoring any previous core.hooksPath
  status          which layers are active, and what is protecting what
  history         scan full git history of one or many repos, with triage
  allow           baseline a finding you have accepted
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("SECRET_GUARD_HOME", Path.home() / ".secret-guard"))
HOOKS_DIR = HOME / "hooks"
BASELINE = HOME / "baseline.json"
SELF = Path(__file__).resolve()
PATTERNS = json.loads((SELF.parent / "patterns.json").read_text())

# Findings, baselines and scan output stay on this machine. They name real
# paths and real variable names; committing them re-creates the problem.
_RULES = PATTERNS["rules"]
_NAME_RE = re.compile(PATTERNS["name_context"])
_ASSIGN_RE = PATTERNS["assignment_prefix"]
_PLACEHOLDERS = tuple(PATTERNS["placeholder_markers"])
_SKIP_PATHS = tuple(PATTERNS["skip_path_markers"])
_MAX_LINE = 1000        # longer than this is minified/data, not code
_MAX_PER_LINE = 5       # more than this on one line means data blob


# ----------------------------------------------------------------- detection

def entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    n = len(s)
    return -sum((v / n) * math.log2(v / n) for v in counts.values())


def _is_placeholder(token: str) -> bool:
    low = token.lower()
    if any(m in low for m in _PLACEHOLDERS):
        return True
    # aaaa…, 0000…, repeated 2-char cycles
    return len(set(low)) <= 3


def _mixed(tok: str) -> bool:
    """Real keys mix classes. Prose words and hashes-of-one-class do not."""
    return sum(bool(re.search(p, tok)) for p in (r"[a-z]", r"[A-Z0-9]")) == 2


def scan_line(line: str, path: str = "") -> list[dict]:
    """Findings in one line.

    Two rule kinds, because they have different false-positive profiles:
      shape       — unmistakable formats (AKIA…, ghp_…, PEM). Fire anywhere.
      assignment  — the value must be ASSIGNED to a secret-named variable.
                    Requiring only that a secret-ish WORD appear somewhere on
                    the line matched every authentication doc in the corpus
                    (38k hits across 11 repos). Anchoring on `NAME = VALUE`
                    is what makes this usable.
    """
    if any(m in path for m in _SKIP_PATHS):
        return []
    # Minified bundles and embedded data blobs put hundreds of high-entropy
    # tokens on one line. A credential assignment is never 1000 chars wide, and
    # a 39-hit line is a data blob, not 39 credentials. gitleaks handles these
    # better; the builtin engine declines them rather than crying wolf.
    if len(line) > _MAX_LINE:
        return []
    out, seen = [], set()
    for rule in _RULES:
        if rule.get("kind") == "assignment":
            pattern = _ASSIGN_RE + "(" + rule["regex"] + ")"
            spans = [(m.group(1), m) for m in re.finditer(pattern, line)]
        else:
            spans = [(m.group(0), m) for m in re.finditer(rule["regex"], line)]
        for tok, _m in spans:
            if tok in seen:
                continue
            if rule.get("min_entropy") and entropy(tok) < rule["min_entropy"]:
                continue
            if rule.get("require_mixed") and not _mixed(tok):
                continue
            if rule["id"] != "private-key-pem" and _is_placeholder(tok):
                continue
            seen.add(tok)
            out.append({"rule": rule["id"], "desc": rule["desc"],
                        "match": tok, "fingerprint": fingerprint(tok)})
            if len(out) >= _MAX_PER_LINE:
                return out
    return out


def fingerprint(token: str) -> str:
    """Stable id for baselining — never store or print the secret itself."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def redact(token: str) -> str:
    return f"{token[:4]}…({len(token)} chars)"


def load_baseline() -> set[str]:
    if not BASELINE.exists():
        return set()
    try:
        return set(json.loads(BASELINE.read_text()).get("allowed", []))
    except (OSError, json.JSONDecodeError):
        return set()


# -------------------------------------------------------------- git plumbing

def git(*args: str, cwd: str | None = None) -> str:
    p = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    return p.stdout if p.returncode == 0 else ""


def has_gitleaks() -> str | None:
    return shutil.which("gitleaks")


def gitleaks_scan(cwd: str, staged: bool) -> list[dict] | None:
    """Use gitleaks when present. Returns None if it could not be used."""
    exe = has_gitleaks()
    if not exe:
        return None
    args = [exe, "protect" if staged else "detect", "--no-banner",
            "--redact", "--report-format", "json", "--report-path", "-"]
    if staged:
        args.append("--staged")
    p = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    if p.returncode not in (0, 1):
        return None
    try:
        rows = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return [{"rule": r.get("RuleID", "gitleaks"),
             "desc": r.get("Description", ""),
             "file": r.get("File", ""), "line": r.get("StartLine", 0),
             "match": r.get("Secret", "") or r.get("Match", ""),
             "fingerprint": fingerprint(r.get("Secret", "") or r.get("Match", "")),
             "engine": "gitleaks"} for r in rows]


def diff_for(cwd: str, mode: str, rng: str | None) -> str:
    """`git commit -a` stages at commit time, so --staged alone would miss it."""
    if mode == "range" and rng:
        return git("diff", "--unified=0", rng, cwd=cwd)
    if mode == "all":            # -a / -am: tracked modifications too
        return git("diff", "--unified=0", "HEAD", cwd=cwd)
    return git("diff", "--unified=0", "--cached", cwd=cwd)


def scan_diff(text: str) -> list[dict]:
    findings, path = [], ""
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for f in scan_line(line[1:], path):
            f["file"] = path
            findings.append(f)
    return findings


def scan(cwd: str, mode: str = "staged", rng: str | None = None) -> list[dict]:
    base = load_baseline()
    found = gitleaks_scan(cwd, staged=(mode != "range")) if mode != "range" else None
    if found is None:
        found = scan_diff(diff_for(cwd, mode, rng))
        for f in found:
            f["engine"] = "builtin"
    return [f for f in found if f["fingerprint"] not in base]


def report(findings: list[dict], stream=sys.stderr) -> None:
    n = len(findings)
    print(f"\n  secret-guard: {n} possible secret{'s' if n != 1 else ''} "
          f"in content about to be committed\n", file=stream)
    for f in findings:
        loc = f.get("file", "?")
        if f.get("line"):
            loc += f":{f['line']}"
        print(f"    {f['rule']:28s} {loc}", file=stream)
        print(f"    {'':28s} {redact(f.get('match',''))}  "
              f"fingerprint={f['fingerprint']}", file=stream)
    print(f"\n  Engine: {findings[0].get('engine','builtin')}"
          f"{'' if has_gitleaks() else '  (install gitleaks for ~150 more rules)'}",
          file=stream)
    print("\n  If this is a real credential: do NOT just delete the line — it is"
          "\n  already in your working tree and would enter history on commit."
          "\n  Move it to an untracked .env and read it via os.environ.\n"
          "\n  If it is a false positive:"
          f"\n    python3 {SELF} allow {findings[0]['fingerprint']}\n"
          "\n  To bypass once (recorded nowhere, use sparingly):"
          "\n    git commit --no-verify\n", file=stream)


# ------------------------------------------------------------------- install

HOOK_TEMPLATE = """#!/bin/sh
# Installed by secret-guard. Blocks commits containing likely credentials.
# Chains to the repository's own hook afterwards so nothing is silently lost.
[ -n "$SECRET_GUARD_SKIP" ] && exit 0
"{python}" "{script}" scan --mode staged --quiet-ok || exit 1

# core.hooksPath makes git ignore .git/hooks entirely, which would silently
# disable pre-commit/husky in repos that use them. Re-run the repo's own hook.
_gd=$(git rev-parse --git-dir 2>/dev/null) || exit 0
_own="$_gd/hooks/pre-commit"
if [ -x "$_own" ] && [ "$_own" != "$0" ]; then
  SECRET_GUARD_SKIP=1 "$_own" "$@" || exit 1
fi
exit 0
"""


def install(global_: bool = True) -> dict:
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    hook = HOOKS_DIR / "pre-commit"
    hook.write_text(HOOK_TEMPLATE.format(python=sys.executable, script=SELF))
    hook.chmod(0o755)

    prev = git("config", "--global", "core.hooksPath").strip()
    if prev and Path(prev).resolve() != HOOKS_DIR.resolve():
        # Somebody already uses a global hooks dir — do not clobber it.
        (HOME / "previous_hooks_path").write_text(prev)
    if not global_:
        return {"hook": str(hook), "scope": "written-only",
                "note": "run with --global to activate for every repo"}
    subprocess.run(["git", "config", "--global", "core.hooksPath", str(HOOKS_DIR)],
                   check=True)
    return {"hook": str(hook), "scope": "global",
            "hooksPath": str(HOOKS_DIR), "previous": prev or None,
            "gitleaks": has_gitleaks() or "not installed (builtin rules active)"}


def uninstall() -> dict:
    prevf = HOME / "previous_hooks_path"
    if prevf.exists():
        subprocess.run(["git", "config", "--global", "core.hooksPath",
                        prevf.read_text().strip()], check=False)
        prevf.unlink()
        return {"restored": "previous core.hooksPath"}
    subprocess.run(["git", "config", "--global", "--unset", "core.hooksPath"],
                   check=False)
    return {"removed": "global core.hooksPath"}


def status() -> dict:
    hp = git("config", "--global", "core.hooksPath").strip()
    active = bool(hp) and Path(hp).resolve() == HOOKS_DIR.resolve()
    return {
        "machine_layer": "ACTIVE" if active else "not installed",
        "hooksPath": hp or None,
        "agent_layer": "active whenever the plugin is installed (PreToolUse hook)",
        "engine": "gitleaks" if has_gitleaks() else "builtin patterns.json",
        "gitleaks_path": has_gitleaks(),
        "rules": len(_RULES) if not has_gitleaks() else "~150 (gitleaks)",
        "baselined": len(load_baseline()),
        "note": None if active else
        "Run: python3 secret_guard.py install --global",
    }


# ------------------------------------------------------------------- history

def history(repos: list[str]) -> dict:
    out = {}
    for r in repos:
        if not (Path(r) / ".git").exists():
            continue
        exe = has_gitleaks()
        if exe:
            p = subprocess.run([exe, "detect", "--no-banner", "--redact",
                                "--report-format", "json", "--report-path", "-"],
                               capture_output=True, text=True, cwd=r)
            try:
                rows = json.loads(p.stdout or "[]")
            except json.JSONDecodeError:
                rows = []
            found = [{"rule": x.get("RuleID"), "file": x.get("File"),
                      "line": x.get("StartLine"), "commit": (x.get("Commit") or "")[:8],
                      "fingerprint": fingerprint(x.get("Secret", ""))} for x in rows]
        else:
            # No gitleaks: scan tracked files at HEAD (history needs gitleaks).
            found = []
            for f in git("ls-files", cwd=r).splitlines():
                fp = Path(r) / f
                try:
                    text = fp.read_text(errors="ignore")
                except (OSError, UnicodeDecodeError):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    for hit in scan_line(line, f):
                        found.append({**hit, "file": f, "line": i,
                                      "commit": "HEAD-only"})
        base = load_baseline()
        found = [f for f in found if f.get("fingerprint") not in base]
        out[Path(r).name] = found
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--mode", choices=["staged", "all", "range"], default="staged")
    s.add_argument("--range")
    s.add_argument("--repo", default=".")
    s.add_argument("--quiet-ok", action="store_true",
                   help="print nothing when clean (hook mode)")
    s.add_argument("--json", action="store_true")
    i = sub.add_parser("install")
    i.add_argument("--global", dest="global_", action="store_true", default=True)
    i.add_argument("--no-global", dest="global_", action="store_false")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    h = sub.add_parser("history")
    h.add_argument("repos", nargs="+")
    h.add_argument("--json", action="store_true")
    a = sub.add_parser("allow")
    a.add_argument("fingerprint")
    a.add_argument("--reason", default="")
    args = ap.parse_args()

    if args.cmd == "scan":
        f = scan(args.repo, args.mode, args.range)
        if args.json:
            print(json.dumps([{k: (redact(v) if k == "match" else v)
                               for k, v in x.items()} for x in f], indent=2))
        elif f:
            report(f)
        elif not args.quiet_ok:
            print("secret-guard: clean")
        return 1 if f else 0

    if args.cmd == "install":
        print(json.dumps(install(args.global_), indent=2))
    elif args.cmd == "uninstall":
        print(json.dumps(uninstall(), indent=2))
    elif args.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif args.cmd == "history":
        res = history(args.repos)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            total = sum(len(v) for v in res.values())
            for repo, fs in sorted(res.items(), key=lambda kv: -len(kv[1])):
                mark = "clean" if not fs else f"{len(fs)} finding(s)"
                print(f"  {repo:38s} {mark}")
                for x in fs[:12]:
                    print(f"      {x.get('rule','?'):26s} {x.get('file')}:{x.get('line')}"
                          f"  [{x.get('commit')}]")
                if len(fs) > 12:
                    print(f"      … {len(fs)-12} more")
            print(f"\n  total: {total}")
            if not has_gitleaks():
                print("  NOTE: gitleaks absent — scanned HEAD only, NOT history.")
        return 1 if any(res.values()) else 0
    elif args.cmd == "allow":
        HOME.mkdir(parents=True, exist_ok=True)
        data = {"allowed": []}
        if BASELINE.exists():
            data = json.loads(BASELINE.read_text())
        entry = args.fingerprint
        if entry not in data["allowed"]:
            data["allowed"].append(entry)
        data.setdefault("reasons", {})[entry] = args.reason
        BASELINE.write_text(json.dumps(data, indent=2))
        print(f"baselined {entry} ({len(data['allowed'])} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
