#!/usr/bin/env python3
"""
lmstudio-panel: control a local LM Studio server (server/model control only).

Global skill, usable from ANY repo (and by non-Claude tools — plain stdlib
Python, no pip dependencies). Two jobs:

  1. Server/model control: ensure the LM Studio server is up, list models,
     load/unload (the model-major batching primitive: load one model, run
     everything through it, unload, next).
  2. chat(): one OpenAI-compatible completion call that ALWAYS logs a usage
     event — input/output/reasoning tokens, wall-time, project, user,
     machine — via the sibling llm-usage-ledger skill.

ALL usage accounting (ledger, reports, prices, ingest, HTML dashboards)
lives in the llm-usage-ledger skill; this module only delegates to it.

CLI:
  lmstudio_panel.py serve                      # ensure server is running
  lmstudio_panel.py models                     # list downloaded models
  lmstudio_panel.py load <model> | unload      # model-major primitives
  lmstudio_panel.py chat --model M --prompt P [--task-tag T] [--max-tokens N]
  lmstudio_panel.py smoke --model M            # judge-shaped health check
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "llm-usage-ledger"))
try:
    from llm_usage_ledger import log_usage, detect_project
except ImportError as _e:
    raise ImportError(
        "lmstudio-panel now delegates all usage accounting to the "
        "llm-usage-ledger skill, which was not found next to it. Install "
        "the llm-usage-ledger skill (expected at "
        f"{Path(__file__).resolve().parent.parent / 'llm-usage-ledger'}) "
        "from the same skills repo/marketplace, then retry."
    ) from _e

# Back-compat re-exports: old callers did `from lmstudio_panel import ...`
# for accounting functions that now live in llm-usage-ledger. Keep them
# importable from here so existing instrumented repos keep working.
try:
    from llm_usage_ledger import (  # noqa: F401
        aggregate, as_of_price, hourly_windows, html_report, is_local_event,
        ledger_dir, load_price_series, machine_name, match_price,
        normalize_model_name, print_report, prices_update, read_ledgers,
        whoami,
    )
except ImportError:
    pass  # older llm-usage-ledger without some names: core imports above stand

BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
LMS = os.path.expanduser("~/.lmstudio/bin/lms")


# ----------------------------------------------------------------- server
def _http_json(url: str, payload: dict = None, timeout: float = 600):
    req = urllib.request.Request(
        url, headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def server_up() -> bool:
    try:
        _http_json(f"{BASE_URL}/models", timeout=5)
        return True
    except Exception:
        return False


def ensure_server() -> bool:
    if server_up():
        return True
    subprocess.run([LMS, "server", "start"], capture_output=True, timeout=60)
    for _ in range(10):
        if server_up():
            return True
        time.sleep(1)
    return False


def list_models():
    return [m["id"] for m in _http_json(f"{BASE_URL}/models", timeout=10)["data"]]


def load_model(model: str) -> bool:
    r = subprocess.run([LMS, "load", model, "--yes"],
                       capture_output=True, text=True, timeout=600)
    return r.returncode == 0


def unload_all() -> bool:
    r = subprocess.run([LMS, "unload", "--all"],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0


# ------------------------------------------------------------------- chat
def chat(model: str, messages, max_tokens: int = 2048,
         temperature: float = 0.0, task_tag: str = None,
         project: str = None, timeout: float = 600) -> dict:
    """One completion call, always ledger-logged.

    Returns {"content", "usage", "duration_s", "raw"}."""
    t0 = time.time()
    raw = _http_json(f"{BASE_URL}/chat/completions", {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }, timeout=timeout)
    duration = time.time() - t0
    log_usage(model, raw.get("usage"), duration,
              task_tag=task_tag, project=project)
    return {"content": raw["choices"][0]["message"]["content"],
            "usage": raw.get("usage"), "duration_s": round(duration, 2),
            "raw": raw}


SMOKE_PROMPT = (
    "A clinical protocol must state the number of participants to be "
    "enrolled. The following excerpt contains no mention of enrollment "
    "numbers anywhere. Reply with exactly one word: VIOLATION or COMPLIANT."
)


def smoke(model: str) -> bool:
    """Judge-shaped health check: expects VIOLATION in the reply."""
    r = chat(model, [{"role": "user", "content": SMOKE_PROMPT}],
             max_tokens=4096, task_tag="smoke-test")
    ok = "VIOLATION" in r["content"].upper()
    print(f"{model}: {'PASS' if ok else 'FAIL'} "
          f"({r['duration_s']}s, {json.dumps(r['usage'])})")
    if not ok:
        print(f"  reply was: {r['content'][:200]!r}")
    return ok


# -------------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("models")
    p = sub.add_parser("load"); p.add_argument("model")
    sub.add_parser("unload")
    p = sub.add_parser("chat")
    p.add_argument("--model", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--task-tag")
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--temperature", type=float, default=0.0)
    p = sub.add_parser("smoke"); p.add_argument("--model", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        ok = ensure_server()
        print("server up" if ok else "FAILED to start server")
        return 0 if ok else 1
    if a.cmd == "models":
        print("\n".join(list_models()))
        return 0
    if a.cmd == "load":
        return 0 if load_model(a.model) else 1
    if a.cmd == "unload":
        return 0 if unload_all() else 1
    if a.cmd == "chat":
        r = chat(a.model, [{"role": "user", "content": a.prompt}],
                 max_tokens=a.max_tokens, temperature=a.temperature,
                 task_tag=a.task_tag)
        print(r["content"])
        return 0
    if a.cmd == "smoke":
        return 0 if smoke(a.model) else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
