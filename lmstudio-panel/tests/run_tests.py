#!/usr/bin/env python3
"""Regression tests + telemetry for the lmstudio-panel global skill.

Fully deterministic — no LM Studio server required. lmstudio-panel is now
server-control only; all accounting tests live in the sibling
llm-usage-ledger skill. This suite checks the module imports (including the
delegation to llm-usage-ledger), that ledger writes still flow through the
delegated log_usage, and CLI integrity.

Usage: run_tests.py --model <id> [--auto] | --report
"""
import argparse
import datetime
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
LEDGER = SKILL_DIR.parent / "telemetry" / "lmstudio-panel.jsonl"
AUTO_RUNS = 8

sys.path.insert(0, str(SKILL_DIR))


def version():
    try:
        return (SKILL_DIR / "VERSION").read_text().strip()
    except OSError:
        return "unversioned"


def run_suite():
    r = {}

    def check(name, cond, why=""):
        r[name] = "pass" if cond else f"FAIL: {why}"

    # 1. module imports clean (includes the llm-usage-ledger delegation)
    try:
        import lmstudio_panel as lp
        check("module_imports", True)
    except Exception as e:
        check("module_imports", False, repr(e))
        return r

    # 2. accounting is delegated: log_usage comes from llm-usage-ledger and
    # writes to the shared ledger dir when called through this module
    with tempfile.TemporaryDirectory() as td:
        os.environ["LLM_TOKEN_LEDGER_DIR"] = td
        try:
            import llm_usage_ledger as ul
            ev = lp.log_usage("test-model", {"prompt_tokens": 3,
                                             "completion_tokens": 1}, 0.1,
                              task_tag="delegation-test")
            files = list(Path(td).glob("lmstudio-*.jsonl"))
            check("delegates_to_ledger",
                  lp.log_usage is ul.log_usage
                  and ev["prompt_tokens"] == 3
                  and len(files) == 1
                  and json.loads(files[0].read_text())["task_tag"]
                  == "delegation-test",
                  f"files={files!r} ev={ev!r}")
        except Exception as e:
            check("delegates_to_ledger", False, repr(e))
        finally:
            os.environ.pop("LLM_TOKEN_LEDGER_DIR", None)

    # 3. back-compat re-exports still importable from this module
    reexports = ["read_ledgers", "aggregate", "hourly_windows", "as_of_price",
                 "load_price_series", "html_report", "print_report",
                 "prices_update", "normalize_model_name", "match_price",
                 "is_local_event", "ledger_dir", "machine_name", "whoami",
                 "detect_project"]
    missing = [n for n in reexports if not callable(getattr(lp, n, None))]
    check("backcompat_reexports", not missing, f"missing {missing}")

    # 4. project auto-detect returns a non-empty string
    check("project_autodetect", isinstance(lp.detect_project(), str)
          and len(lp.detect_project()) > 0)

    # 5. CLI help exits 0
    p = subprocess.run([sys.executable, str(SKILL_DIR / "lmstudio_panel.py"),
                        "--help"], capture_output=True, timeout=20)
    check("cli_help", p.returncode == 0)
    return r


def record(model, results, dur):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "skill": "lmstudio-panel", "skill_version": version(),
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
