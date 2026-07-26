#!/usr/bin/env python3
"""Regression tests + telemetry for meeting-canvas (convention: skills README).

Deterministic checks on the template (the skill's intelligence lives there);
the model-authored parts (row generation, placeholder substitution) are
covered by SKILL.md's own procedure and recorded via telemetry only.

Usage: run_tests.py --model <id> [--auto] | --report
"""
import argparse, datetime, json, re, statistics, sys, time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
LEDGER = SKILL_DIR.parent / "telemetry" / "meeting-canvas.jsonl"
TEMPLATE = SKILL_DIR / "assets" / "template.html"
AUTO_RUNS = 8

def version():
    try: return (SKILL_DIR / "VERSION").read_text().strip()
    except OSError: return "unversioned"

def run_suite():
    r = {}
    def check(name, cond, why=""):
        r[name] = "pass" if cond else f"FAIL: {why}"
    t = TEMPLATE.read_text()
    check("template_exists", TEMPLATE.is_file())
    for ph in ("{{TITLE}}", "{{DATE}}", "{{STORAGE_KEY}}"):
        check(f"placeholder_{ph.strip('{}')}", ph in t, f"{ph} missing")
    ext = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', t)
    check("self_contained", not ext, f"external refs: {ext[:3]}")
    check("localstorage_persistence", t.count("localStorage") >= 5, "persistence code missing")
    check("copy_plan_roundtrip_schema", "Copy plan" in t or "copyPlan" in t or "copy-plan" in t.lower(),
          "Copy-plan (bake-back) affordance missing")
    check("template_size_sane", 40_000 < len(t) < 400_000, f"len={len(t)}")
    return r

def record(model, results, dur):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "skill": "meeting-canvas", "skill_version": version(), "model": model,
            "n_pass": sum(1 for v in results.values() if v == "pass"),
            "n_fail": sum(1 for v in results.values() if v.startswith("FAIL")),
            "duration_s": round(dur, 2), "results": results}) + "\n")

def entries():
    if not LEDGER.exists(): return []
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model"); ap.add_argument("--auto", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        rows = {}
        for e in entries(): rows.setdefault((e["skill_version"], e["model"]), []).append(e)
        for (v, m), es in sorted(rows.items()):
            tot = sum(e["n_pass"] + e["n_fail"] for e in es); ok = sum(e["n_pass"] for e in es)
            d = [e["duration_s"] for e in es]
            sd = statistics.stdev(d) if len(d) > 1 else 0.0
            print(f"{v:16s} {m:26s} runs={len(es)} pass={ok/tot:.0%} dur={statistics.mean(d):.2f}±{sd:.2f}s")
        return
    if not a.model: sys.exit("--model required (or --report)")
    n = sum(1 for e in entries() if e["model"] == a.model and e["skill_version"] == version())
    if a.auto and n >= AUTO_RUNS:
        print(f"telemetry: {AUTO_RUNS} runs recorded for {a.model} @ {version()} — skipping"); return
    t0 = time.time(); res = run_suite(); dur = time.time() - t0
    record(a.model, res, dur)
    fails = sum(1 for v in res.values() if v.startswith("FAIL"))
    for k, v in res.items(): print(f"  {k}: {v}")
    print("ALL PASS" if not fails else f"{fails} FAILURES"); sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
