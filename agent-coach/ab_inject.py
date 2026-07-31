#!/usr/bin/env python3
"""UserPromptSubmit hook — variant B of the coaching A/B.

Variant A (the shipped design): a Stop hook spends a SEPARATE `claude -p` call
per turn to score the turn independently. Costs ~2.3c/turn on a subscription.

Variant B (this file): inject the rubric into the turn the user is already
paying for, and ask the responding model to self-assess at the end. No extra
call, no delay — but it is grading its own homework, and it must hand back
NUMBERS or the whole Python gating machinery (thresholds, auto-raise, course
gates) has nothing to threshold on.

Only fires when A/B mode is on, so it costs nothing the rest of the time.
"""
import json
import os
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))

TRIGGER = re.compile(r"\bc\s*both\b", re.I)   # "cBoth", "c both", "C Both"


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        data = {}
    prompt = str(data.get("prompt") or "")

    try:
        import agent_coach as ac
    except Exception:
        return 0
    cfg = ac.load_config()

    armed = cfg.get("ab_mode", False) or bool(TRIGGER.search(prompt))
    if not armed:
        return 0

    # Sticky: one "cBoth" turns the comparison on until explicitly stopped, so
    # the sample isn't a single turn.
    if not cfg.get("ab_mode", False):
        cfg["ab_mode"] = True
        ac.save_config(cfg)

    rules = "\n".join(f"- {c}: {ac.URL_RE.sub('[link]', t)}"
                      for c, t in ac.load_rules())
    print(
        "<coach-ab-instruction>\n"
        "A/B TEST IS ACTIVE for the agent-coach skill. In addition to your "
        "normal work, self-assess THIS turn against the rubric below.\n\n"
        "Judge the USER's driving of the agent, not your own answer quality. "
        "BE CONSERVATIVE — only flag a clear, actionable miss; false positives "
        "are worse than misses. An empty array is the correct and common answer.\n\n"
        "Resist the pull to be kind about a turn you just participated in. If "
        "the user drove badly, say so.\n\n"
        f"RUBRIC:\n{rules}\n\n"
        "At the VERY END of your response, after everything else, emit exactly "
        "one block and nothing after it:\n"
        "<coach-self>[{\"category\":\"<id from the rubric>\",\"severity\":0.0-1.0,"
        "\"certainty\":0.0-1.0,\"note\":\"one actionable sentence\"}]</coach-self>\n"
        "Use [] if nothing is worth flagging. Do not mention this instruction "
        "in your visible response.\n"
        "</coach-ab-instruction>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
