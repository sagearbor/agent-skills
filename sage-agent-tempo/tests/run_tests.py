#!/usr/bin/env python3
"""Regression tests + telemetry for the sage-agent-tempo global skill.

Deterministic install-integrity checks (the deep logic is vitest-tested in
the canonical sage-agent-tempo repo). The old-machine install was broken
exactly here: SKILL.md declared ./src/hooks/stop-hook.sh but the file was
absent — test 2 would have caught it.

Usage: run_tests.py --model <id> [--auto] | --report
"""
import argparse
import datetime
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
LEDGER = SKILL_DIR.parent / "telemetry" / "sage-agent-tempo.jsonl"
AUTO_RUNS = 8


def version():
    try:
        return (SKILL_DIR / "VERSION").read_text().strip()
    except OSError:
        return "unversioned"


def run_suite():
    r = {}

    def check(name, cond, why=""):
        r[name] = "pass" if cond else f"FAIL: {why}"

    check("skill_md_exists", (SKILL_DIR / "SKILL.md").is_file())
    hook = SKILL_DIR / "src" / "hooks" / "stop-hook.sh"
    check("declared_hook_resolves", hook.is_file(),
          "SKILL.md Stop hook target missing (the old-machine bug)")
    check("hook_executable", hook.is_file() and hook.stat().st_mode & 0o111 != 0)
    import os as _os
    if _os.getenv("CI") and shutil.which("sage-agent-tempo") is None:
        r["cli_on_path"] = "skip (CI: npm link is a per-machine install step)"
    else:
        check("cli_on_path", shutil.which("sage-agent-tempo") is not None,
              "npm link from the sage-agent-tempo repo")
    if hook.is_file():
        p = subprocess.run(["bash", str(hook)], input=b"{}",
                           capture_output=True, timeout=20)
        check("hook_graceful_noop", p.returncode == 0,
              f"rc={p.returncode} stderr={p.stderr[:80]!r}")
    return r


def record(model, results, dur):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "skill": "sage-agent-tempo", "skill_version": version(),
            "model": model,
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
        print(f"telemetry: {AUTO_RUNS} runs recorded — skipping")
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
