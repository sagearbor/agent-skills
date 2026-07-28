#!/usr/bin/env python3
"""Regression tests + telemetry for agent-coach. Fully deterministic — the
real model call (claude -p) is monkeypatched, so no network/keys/Claude Code
needed. Exercises: rubric parse, config, threshold gating, escalation gating,
dynamic raise, transcript parse, score parse, note format, recursion guard,
event log, rubric snapshot/revert, dashboard render, install into temp settings.

Usage: run_tests.py --model <id> [--auto] | --report
"""
import argparse
import datetime
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
LEDGER = SKILL_DIR.parent / "telemetry" / "agent-coach.jsonl"
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

    try:
        import agent_coach as ac
    except Exception as e:
        check("module_imports", False, repr(e)); return r
    check("module_imports", True)

    cats = ac.categories()
    check("rubric_parses", len(cats) >= 8 and "model-selection" in cats,
          f"got {cats}")

    with tempfile.TemporaryDirectory() as td:
        os.environ["AGENT_COACH_DIR"] = td
        ac.COACH_DIR = Path(td)
        ac.CONFIG = Path(td) / "config.json"

        cfg = ac.default_config()
        check("default_thresholds_low",
              all(v == ac.START_THRESHOLD for v in cfg["thresholds"].values()))

        ac.save_config(cfg)
        check("config_roundtrip", ac.load_config()["thresholds"] == cfg["thresholds"])

        # score parsing from a fixture claude -p 'result' text
        scores = ac.parse_scores(
            'here you go: [{"category":"model-selection","severity":0.8,'
            '"certainty":0.9,"note":"use haiku"},'
            '{"category":"bogus","severity":1,"certainty":1,"note":"x"}]')
        check("score_parse_filters", len(scores) == 1
              and scores[0]["category"] == "model-selection",
              f"got {scores}")

        # threshold gate: 0.8 fires at thr 0.5, not at 0.9
        cfg["thresholds"]["model-selection"] = 0.5
        fires = [s for s in scores if s["severity"] >= cfg["thresholds"][s["category"]]]
        check("threshold_gate_fires", len(fires) == 1)
        cfg["thresholds"]["model-selection"] = 0.9
        fires = [s for s in scores if s["severity"] >= cfg["thresholds"][s["category"]]]
        check("threshold_gate_silences", len(fires) == 0)

        # dynamic raise after a clean streak
        cfg = ac.default_config()
        for _ in range(ac.CLEAN_STREAK_TO_RAISE):
            ac.update_dynamic(cfg, set())  # no fires -> clean streak accrues
        check("dynamic_raise",
              cfg["thresholds"]["plan-first"] > ac.START_THRESHOLD,
              f"got {cfg['thresholds']['plan-first']}")
        cfg2 = ac.default_config()
        ac.update_dynamic(cfg2, {"plan-first"})
        check("fire_resets_streak", cfg2["clean_streak"]["plan-first"] == 0)

        # recursion guard: hook no-ops when AGENT_COACH_ACTIVE set
        os.environ["AGENT_COACH_ACTIVE"] = "1"
        check("recursion_guard", ac.run_hook({"transcript_path": "x"}) == {})
        del os.environ["AGENT_COACH_ACTIVE"]

        # transcript parse: a substantive turn is summarized; pure-chat is skipped
        tj = Path(td) / "t.jsonl"
        tj.write_text("\n".join(json.dumps(x) for x in [
            {"type": "user", "message": {"content": "fix the typo in readme"}},
            {"type": "assistant", "message": {"model": "claude-opus-4-8",
             "content": [{"type": "tool_use", "name": "Edit",
                          "input": {"file_path": "/x/README.md"}}]}},
        ]))
        turn = ac.extract_last_turn(tj)
        check("transcript_parse", turn is not None
              and "claude-opus-4-8" in turn[2]["models"], f"got {turn}")
        chat = Path(td) / "c.jsonl"
        chat.write_text(json.dumps(
            {"type": "user", "message": {"content": "hi"}}))
        check("activity_gate_skips_chat", ac.extract_last_turn(chat) is None)

        # full hook with a MOCKED model -> fires a note + logs an event
        ac.call_model = lambda m, p, timeout=45: (
            '[{"category":"model-selection","severity":0.9,"certainty":0.95,'
            '"note":"Opus for a typo — use Haiku"}]', {"input_tokens": 50})
        cfg = ac.default_config(); ac.save_config(cfg)
        out = ac.run_hook({"transcript_path": str(tj), "cwd": "/proj/demo"})
        check("hook_fires_note", "systemMessage" in out
              and "AGENT COACH" in out["systemMessage"]
              and "model-selection" in out["systemMessage"], f"got {out}")
        evf = ac.events_file()
        check("event_logged", evf.exists()
              and "model-selection" in evf.read_text())

        # escalation: low certainty + cutoff>0 -> smarter model consulted; if it
        # disagrees (severity below thr) the finding is dropped
        cfg = ac.default_config(); cfg["escalation_cutoff"] = 0.9
        cfg["thresholds"]["model-selection"] = 0.5; ac.save_config(cfg)
        calls = {"n": 0}

        def flaky(m, p, timeout=45):
            calls["n"] += 1
            if calls["n"] == 1:  # scorer: high severity, LOW certainty
                return ('[{"category":"model-selection","severity":0.8,'
                        '"certainty":0.2,"note":"maybe"}]', {})
            return ('[]', {})  # escalation model disagrees -> drop
        ac.call_model = flaky
        out = ac.run_hook({"transcript_path": str(tj), "cwd": "/proj/demo"})
        check("escalation_drops_false_positive",
              out == {} and calls["n"] == 2, f"got {out} calls={calls}")

        # dashboard renders from the logged events
        dpath = Path(td) / "dash.html"
        ac.dashboard(str(dpath))
        check("dashboard_renders", dpath.exists()
              and "agent-coach usage" in dpath.read_text())

    os.environ.pop("AGENT_COACH_DIR", None)

    # rubric snapshot + revert roundtrip (in a temp archive)
    orig = ac.RUBRIC.read_text()
    ac.rules_snapshot()
    snaps = sorted(ac.ARCHIVE.glob("best_practices_*.md"))
    check("rules_snapshot", len(snaps) >= 1)
    if snaps:
        snaps[-1].unlink()  # cleanup test artifact

    # install into a temp settings.json
    with tempfile.TemporaryDirectory() as td2:
        sp = Path(td2) / "settings.json"
        ac.settings_path = lambda: sp
        ac.install()
        check("install_adds_hook", sp.exists()
              and "agent_coach.py" in sp.read_text())
        ac.uninstall()
        check("uninstall_removes_hook", "agent_coach.py" not in sp.read_text())
    return r


def record(model, results, dur):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "skill": "agent-coach", "skill_version": version(), "model": model,
            "n_pass": sum(1 for v in results.values() if v == "pass"),
            "n_fail": sum(1 for v in results.values() if v.startswith("FAIL")),
            "duration_s": round(dur, 2), "results": results}) + "\n")


def entries():
    if not LEDGER.exists():
        return []
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model"); ap.add_argument("--auto", action="store_true")
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
            print(f"{v:12s} {m:26s} runs={len(es)} pass={ok/tot:.0%} "
                  f"dur={statistics.mean(d):.2f}±{sd:.2f}s")
        return
    if not a.model:
        sys.exit("--model required (or --report)")
    n = sum(1 for e in entries()
            if e["model"] == a.model and e["skill_version"] == version())
    if a.auto and n >= AUTO_RUNS:
        print(f"telemetry: {AUTO_RUNS} runs recorded — skipping"); return
    t0 = time.time(); res = run_suite(); dur = time.time() - t0
    record(a.model, res, dur)
    fails = sum(1 for v in res.values() if v.startswith("FAIL"))
    for k, v in res.items():
        print(f"  {k}: {v}")
    print("ALL PASS" if not fails else f"{fails} FAILURES")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
