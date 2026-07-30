#!/usr/bin/env python3
"""Regression tests + telemetry for secret-guard (convention: skills README).

Offline, deterministic, hermetic: every git test runs in a temp dir with
GIT_CONFIG_GLOBAL and SECRET_GUARD_HOME redirected, so the suite can never
touch the developer's real gitconfig or baseline.

Usage: run_tests.py --model <id> [--auto] | --report
"""
import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
LEDGER = SKILL_DIR.parent / "telemetry" / "secret-guard.jsonl"
GUARD = SKILL_DIR / "secret_guard.py"
HOOK = SKILL_DIR / "hooks" / "pretooluse_secret_guard.py"
AUTO_RUNS = 8
sys.path.insert(0, str(SKILL_DIR))

REAL_SHAPE = "7fca3f3d944046a5bc477b19e5c96ffe"        # 32 hex, fake but real-shaped
LONG_SHAPE = "Ab3" + "xY7qL2mN8pQ4rS6tU9vW1zA5bC0dE" * 2 + "fG3hJ"


def version():
    try:
        return (SKILL_DIR / "VERSION").read_text().strip()
    except OSError:
        return "unversioned"


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, capture_output=True, check=False)


def _repo(root):
    d = Path(root) / "r"
    d.mkdir()
    _git(d, "init", "-q", ".")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    return d


def _hook(payload, env):
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    return p.stdout.strip()


def run_suite():
    r = {}

    def check(name, cond, why=""):
        r[name] = "pass" if cond else f"FAIL: {why}"

    # ---- SKILL.md + packaging ---------------------------------------------
    sm = (SKILL_DIR / "SKILL.md").read_text()
    fm = re.match(r"^---\n(.*?)\n---\n", sm, re.S)
    check("frontmatter", bool(fm) and "name: secret-guard" in fm.group(1))
    check("description_len", bool(fm) and len(
        re.search(r"description:\s*(.+)", fm.group(1)).group(1)) > 120,
        "description too short to trigger reliably")
    check("documents_no_api_key", "no model call" in sm.lower()
          or "no api key" in sm.lower(), "must state it needs no credentials")
    check("documents_limits", "speed bump" in sm.lower(),
          "must state it is bypassable, not a hard control")
    check("documents_never_commit", "Never commit scan output" in sm)
    hooks_json = SKILL_DIR.parent / "hooks" / "hooks.json"
    check("hooks_json_exists", hooks_json.is_file(), "plugin hook not wired")
    if hooks_json.is_file():
        hj = json.loads(hooks_json.read_text())
        pre = hj.get("hooks", {}).get("PreToolUse", [])
        check("hook_matcher_bash", any(e.get("matcher") == "Bash" for e in pre))
        check("hook_uses_plugin_root",
              "${CLAUDE_PLUGIN_ROOT}" in json.dumps(hj),
              "must use CLAUDE_PLUGIN_ROOT, not an absolute path")

    # ---- detection ---------------------------------------------------------
    import secret_guard as sg
    check("entropy_monotonic", sg.entropy("aaaaaaaa") < sg.entropy("a1B2c3D4"))
    check("fingerprint_stable", sg.fingerprint("x") == sg.fingerprint("x"))
    check("fingerprint_distinct", sg.fingerprint("x") != sg.fingerprint("y"))
    check("redact_hides_value", REAL_SHAPE not in sg.redact(REAL_SHAPE),
          "redaction must never echo the secret")

    hits = sg.scan_line(f'AZURE_OPENAI_API_KEY = "{REAL_SHAPE}"', "app.py")
    check("detects_named_hex32", len(hits) == 1 and hits[0]["rule"] == "azure-key-hex32")
    check("no_name_no_hit", sg.scan_line(f'checksum = "{REAL_SHAPE}"', "app.py") == [],
          "bare hex without secret-ish name must not fire")
    check("placeholder_ignored",
          sg.scan_line('API_KEY = "your_key_here"', "app.py") == [])
    check("env_usage_ignored",
          sg.scan_line('API_KEY = os.environ["API_KEY"]', "app.py") == [])
    check("example_path_skipped",
          sg.scan_line(f'API_KEY = "{REAL_SHAPE}"', ".env.example") == [],
          "template files must be skipped by path")
    check("lockfile_skipped",
          sg.scan_line(f'API_KEY = "{REAL_SHAPE}"', "package-lock.json") == [])
    check("pem_needs_no_name",
          len(sg.scan_line("-----BEGIN RSA PRIVATE KEY-----", "k.pem")) == 1)

    # Regression: assignment_prefix once had a CAPTURING group for the variable
    # name, so group(1) returned "api_key" instead of the key. Every long-key
    # assignment silently stopped matching while precision looked excellent.
    hits_long = sg.scan_line(f'    api_key = "{LONG_SHAPE}"', "t.py")
    check("detects_long_key_assignment", len(hits_long) >= 1,
          "long key assigned to api_key must fire")
    check("assignment_returns_value_not_name",
          all(h["match"] != "api_key" and len(h["match"]) > 20 for h in hits_long),
          "match must be the secret, not the variable name")
    check("detects_shell_env_assignment",
          len(sg.scan_line(f'AZURE_OPENAI_API_KEY={LONG_SHAPE}', ".env.bak")) >= 1,
          "unquoted shell assignment is how .env.bak leaked")
    # Known limitation of the builtin engine, asserted so it stays deliberate.
    check("bare_string_is_known_gap",
          sg.scan_line(f'assert "{LONG_SHAPE}" not in out', "t.py") == [],
          "documented gap: no assignment anchor; gitleaks catches this by shape")
    check("aws_key_detected",
          len(sg.scan_line("AKIA2E0A8F3B5C7D9G1H", "a.py")) >= 1)
    # AWS's own docs use AKIAIOSFODNN7EXAMPLE everywhere; flagging it would
    # train people to ignore the hook.
    check("aws_doc_example_suppressed",
          sg.scan_line("AKIAIOSFODNN7EXAMPLE", "a.py") == [],
          "the canonical AWS documentation key must not fire")
    check("conn_string_password",
          len(sg.scan_line("DB=postgres://user:s3cretpw99@host/db", "c.py")) >= 1)

    # ---- hook behaviour (hermetic) -----------------------------------------
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": str(Path(td) / "gc"),
               "SECRET_GUARD_HOME": str(Path(td) / "sg")}
        d = _repo(td)
        (d / "bad.py").write_text(f'AZURE_OPENAI_API_KEY = "{REAL_SHAPE}"\n')
        _git(d, "add", "-A")
        base = {"tool_name": "Bash", "cwd": str(d)}

        out = _hook({**base, "tool_input": {"command": "git commit -m x"}}, env)
        check("hook_denies_commit",
              bool(out) and json.loads(out)["hookSpecificOutput"]
              ["permissionDecision"] == "deny")
        check("hook_reason_redacts", REAL_SHAPE not in out,
              "the deny reason must not contain the secret")
        out_nv = _hook({**base, "tool_input":
                        {"command": "git commit --no-verify -m x"}}, env)
        check("hook_catches_no_verify",
              bool(out_nv) and "deny" in out_nv,
              "--no-verify is the case the git hook cannot catch")
        check("hook_ignores_non_commit",
              _hook({**base, "tool_input": {"command": "git status"}}, env) == "")
        check("hook_ignores_other_tools",
              _hook({"tool_name": "Read", "cwd": str(d),
                     "tool_input": {"file_path": "x"}}, env) == "")
        check("hook_fails_open_on_garbage",
              subprocess.run([sys.executable, str(HOOK)], input="not json",
                             capture_output=True, text=True,
                             env=env).returncode == 0,
              "must never break the session on bad input")

    # ---- commit -a blind spot ---------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": str(Path(td) / "gc"),
               "SECRET_GUARD_HOME": str(Path(td) / "sg")}
        d = _repo(td)
        (d / "f.py").write_text("X = 1\n")
        _git(d, "add", "-A")
        _git(d, "commit", "-q", "-m", "i")
        (d / "f.py").write_text(f'AZURE_OPENAI_API_KEY = "{REAL_SHAPE}"\n')  # UNSTAGED
        base = {"tool_name": "Bash", "cwd": str(d)}
        check("commit_dash_a_not_blind",
              "deny" in _hook({**base, "tool_input":
                               {"command": "git commit -am x"}}, env),
              "-a stages at commit time; scanning --cached alone misses it")
        check("plain_commit_nothing_staged_allowed",
              _hook({**base, "tool_input": {"command": "git commit -m x"}}, env) == "",
              "nothing staged means nothing to block")

    # ---- machine layer: install, chaining, uninstall ------------------------
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": str(Path(td) / "gc"),
               "SECRET_GUARD_HOME": str(Path(td) / "sg")}
        subprocess.run([sys.executable, str(GUARD), "install", "--global"],
                       capture_output=True, env=env, check=False)
        st = json.loads(subprocess.run(
            [sys.executable, str(GUARD), "status"], capture_output=True,
            text=True, env=env).stdout)
        check("install_activates", st["machine_layer"] == "ACTIVE")

        d = _repo(td)
        own = d / ".git" / "hooks" / "pre-commit"
        own.parent.mkdir(parents=True, exist_ok=True)
        marker = Path(td) / "own.log"
        own.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexit 0\n')
        own.chmod(0o755)

        (d / "ok.py").write_text("x = 1\n")
        _git(d, "add", "-A")
        subprocess.run(["git", "commit", "-q", "-m", "clean"], cwd=d,
                       capture_output=True, env=env)
        check("clean_commit_succeeds",
              subprocess.run(["git", "log", "--oneline"], cwd=d,
                             capture_output=True, text=True,
                             env=env).stdout.strip() != "")
        check("chains_to_repo_own_hook", marker.exists(),
              "core.hooksPath silently disables .git/hooks unless we chain")

        (d / "bad.py").write_text(f'AZURE_OPENAI_API_KEY = "{REAL_SHAPE}"\n')
        _git(d, "add", "-A")
        p = subprocess.run(["git", "commit", "-m", "leak"], cwd=d,
                           capture_output=True, text=True, env=env)
        check("secret_commit_blocked", p.returncode != 0,
              "commit containing a credential must fail")
        n = subprocess.run(["git", "log", "--oneline"], cwd=d, capture_output=True,
                           text=True, env=env).stdout.strip().splitlines()
        check("blocked_commit_not_created", len(n) == 1)

        subprocess.run([sys.executable, str(GUARD), "uninstall"],
                       capture_output=True, env=env, check=False)
        st2 = json.loads(subprocess.run(
            [sys.executable, str(GUARD), "status"], capture_output=True,
            text=True, env=env).stdout)
        check("uninstall_deactivates", st2["machine_layer"] != "ACTIVE")

    # ---- baseline ----------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "SECRET_GUARD_HOME": str(Path(td) / "sg")}
        fp = None
        d = _repo(td)
        (d / "b.py").write_text(f'API_KEY = "{REAL_SHAPE}"\n')
        _git(d, "add", "-A")
        out = subprocess.run([sys.executable, str(GUARD), "scan", "--repo", str(d),
                              "--json"], capture_output=True, text=True, env=env)
        rows = json.loads(out.stdout or "[]")
        check("scan_exits_1_on_find", out.returncode == 1)
        if rows:
            fp = rows[0]["fingerprint"]
            subprocess.run([sys.executable, str(GUARD), "allow", fp],
                           capture_output=True, env=env, check=False)
            out2 = subprocess.run([sys.executable, str(GUARD), "scan", "--repo",
                                   str(d), "--json"], capture_output=True,
                                  text=True, env=env)
            check("baseline_suppresses", out2.returncode == 0,
                  "allowed fingerprint must stop firing")
            check("baseline_is_outside_repo",
                  not (Path(d) / "baseline.json").exists(),
                  "baseline must never land inside a repo")
    return r


def record(model, results, dur):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "skill": "secret-guard", "skill_version": version(), "model": model,
            "n_pass": sum(1 for v in results.values() if v == "pass"),
            "n_fail": sum(1 for v in results.values() if v.startswith("FAIL")),
            "duration_s": round(dur, 2), "results": results}) + "\n")


def entries():
    if not LEDGER.exists():
        return []
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        rows = {}
        for e in entries():
            rows.setdefault((e["skill_version"], e["model"]), []).append(e)
        for (v, m), es in sorted(rows.items()):
            tot = sum(e["n_pass"] + e["n_fail"] for e in es)
            ok = sum(e["n_pass"] for e in es)
            d = [e["duration_s"] for e in es]
            sd = statistics.stdev(d) if len(d) > 1 else 0.0
            print(f"{v:16s} {m:26s} runs={len(es)} pass={ok/tot:.0%} "
                  f"dur={statistics.mean(d):.2f}±{sd:.2f}s")
        return
    if not a.model:
        sys.exit("--model required (or --report)")
    n = sum(1 for e in entries()
            if e["model"] == a.model and e["skill_version"] == version())
    if a.auto and n >= AUTO_RUNS:
        print(f"telemetry: {AUTO_RUNS} runs recorded for {a.model} @ {version()} — skipping")
        return
    t0 = time.time()
    res = run_suite()
    dur = time.time() - t0
    record(a.model, res, dur)
    fails = sum(1 for v in res.values() if v.startswith("FAIL"))
    for k, v in res.items():
        print(f"  {k}: {v}")
    print("ALL PASS" if not fails else f"{fails} FAILURES")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
