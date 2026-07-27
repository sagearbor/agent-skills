#!/usr/bin/env python3
"""Regression tests + telemetry for the lmstudio-panel global skill.

Fully deterministic — no LM Studio server required. Exercises the ledger
write/read/aggregate path (the part other repos and the org depend on) with
a temp ledger dir, plus CLI integrity.

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
LEDGER = SKILL_DIR.parent.parent / "telemetry" / "lmstudio-panel.jsonl"
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

    # 1. module imports clean
    try:
        import lmstudio_panel as lp
        check("module_imports", True)
    except Exception as e:
        check("module_imports", False, repr(e))
        return r

    with tempfile.TemporaryDirectory() as td:
        os.environ["LLM_TOKEN_LEDGER_DIR"] = td

        # 2. ledger write/read roundtrip with full schema
        usage = {"prompt_tokens": 100, "completion_tokens": 20,
                 "completion_tokens_details": {"reasoning_tokens": 7}}
        ev = lp.log_usage("test-model", usage, 1.5, task_tag="unit")
        back = lp.read_ledgers()
        check("ledger_roundtrip", len(back) == 1
              and back[0]["prompt_tokens"] == 100
              and back[0]["reasoning_tokens"] == 7,
              f"got {back!r}")
        required = {"ts", "duration_s", "provider", "machine", "user",
                    "project", "model", "prompt_tokens", "completion_tokens",
                    "reasoning_tokens", "task_tag"}
        check("schema_complete", required.issubset(ev.keys()),
              f"missing {required - set(ev.keys())}")

        # 3. reasoning tokens absent -> null, not 0
        ev2 = lp.log_usage("test-model", {"prompt_tokens": 5,
                                          "completion_tokens": 2}, 0.1)
        check("reasoning_null_not_zero", ev2["reasoning_tokens"] is None)

        # 4. per-user filename (shared-dir collision safety)
        files = list(Path(td).glob("lmstudio-*.jsonl"))
        check("per_user_filename", len(files) == 1
              and lp.whoami() in files[0].name
              and lp.machine_name() in files[0].name)

        # 5. aggregation math on a known fixture
        lp.log_usage("model-b", {"prompt_tokens": 50, "completion_tokens": 10},
                     2.0, project="proj-x")
        agg = lp.aggregate(lp.read_ledgers(), by="model")
        check("aggregate_math",
              agg.get("test-model", {}).get("prompt") == 105
              and agg.get("model-b", {}).get("calls") == 1,
              f"got {agg!r}")

        # 6. hourly windows + burst ratio derivable from raw events
        w, mean, peak, burst = lp.hourly_windows(lp.read_ledgers())
        check("windows_derivable", len(w) >= 1 and mean > 0 and peak,
              f"got {w!r}")

        # 7. corrupt ledger line skipped, not fatal
        files[0].write_text(files[0].read_text() + "NOT JSON\n")
        check("corrupt_line_tolerated", len(lp.read_ledgers()) == 3)

    os.environ.pop("LLM_TOKEN_LEDGER_DIR", None)

    # 8. project auto-detect returns a non-empty string
    check("project_autodetect", isinstance(lp.detect_project(), str)
          and len(lp.detect_project()) > 0)

    # 9. CLI help exits 0
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
