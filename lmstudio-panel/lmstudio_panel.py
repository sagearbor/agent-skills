#!/usr/bin/env python3
"""
lmstudio-panel: control a local LM Studio server + cross-repo usage ledger.

Global skill, usable from ANY repo (and by non-Claude tools — plain stdlib
Python, no pip dependencies). Three jobs:

  1. Server/model control: ensure the LM Studio server is up, list models,
     load/unload (the model-major batching primitive: load one model, run
     everything through it, unload, next).
  2. chat(): one OpenAI-compatible completion call that ALWAYS logs a usage
     event — input/output/reasoning tokens, wall-time, project, user,
     machine — to the ledger.
  3. Ledger + report: append-only JSONL, one file per (user, machine) so
     shared-location writes never collide; `report` aggregates every ledger
     file it can see (project x model x user, plus per-hour burst analysis).

Ledger location: $LLM_TOKEN_LEDGER_DIR, default ~/.llm_token_ledger/.
Point the env var at a shared location (mounted drive, synced folder) for
org-wide rollups; per-user filenames make that safe. Events are raw and
timestamped — every windowed/burst view is derived at report time, so new
analyses need no re-instrumentation.

CLI:
  lmstudio_panel.py serve                      # ensure server is running
  lmstudio_panel.py models                     # list downloaded models
  lmstudio_panel.py load <model> | unload      # model-major primitives
  lmstudio_panel.py chat --model M --prompt P [--task-tag T] [--max-tokens N]
  lmstudio_panel.py smoke --model M            # judge-shaped health check
  lmstudio_panel.py report [--by project|model|user] [--windows] [--days N]
"""

import argparse
import datetime
import getpass
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
LMS = os.path.expanduser("~/.lmstudio/bin/lms")
SCHEMA_VERSION = 1
_write_lock = threading.Lock()

# Reference cloud rates for savings estimates ($/1M tokens). Versioned +
# dated so historical reports stay reproducible: add entries, never edit.
CLOUD_REFERENCE_RATES = {
    "2026-07": {"input_per_m": 2.50, "output_per_m": 10.00},
}
CURRENT_RATE_KEY = "2026-07"


# ---------------------------------------------------------------- identity
def ledger_dir() -> Path:
    return Path(os.environ.get("LLM_TOKEN_LEDGER_DIR")
                or Path.home() / ".llm_token_ledger")


def whoami() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def machine_name() -> str:
    # Override for environments that share a hostname (e.g. WSL reports the
    # Windows hostname): set LLM_LEDGER_MACHINE=pc-wsl etc. so per-machine
    # ledger filenames never collide in a shared ledger dir.
    return (os.environ.get("LLM_LEDGER_MACHINE")
            or platform.node().split(".")[0] or "unknown")


def detect_project(cwd: str = ".") -> str:
    """Git repo name of cwd, else the directory basename."""
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip()).name
    except Exception:
        pass
    return Path(cwd).resolve().name


# ----------------------------------------------------------------- ledger
def ledger_file() -> Path:
    return ledger_dir() / f"lmstudio-{whoami()}-{machine_name()}.jsonl"


def log_usage(model: str, usage: dict, duration_s: float,
              task_tag: str = None, project: str = None) -> dict:
    """Append one usage event (schema-versioned, thread-safe).
    Returns the event dict. Never raises. TOKEN_LEDGER_DISABLED=1 disables."""
    details = (usage or {}).get("completion_tokens_details") or {}
    event = {
        "schema": SCHEMA_VERSION,
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_s": round(duration_s, 2),
        "provider": "lmstudio",
        "machine": machine_name(),
        "user": whoami(),
        "project": project or detect_project(),
        "model": model,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "completion_tokens": (usage or {}).get("completion_tokens"),
        # None (not 0) when the server doesn't report reasoning tokens.
        "reasoning_tokens": details.get("reasoning_tokens"),
        "task_tag": task_tag,
    }
    if os.getenv("TOKEN_LEDGER_DISABLED", "").lower() in ("1", "true", "yes"):
        return event
    try:
        with _write_lock:
            ledger_dir().mkdir(parents=True, exist_ok=True)
            with open(ledger_file(), "a") as f:
                f.write(json.dumps(event) + "\n")
    except Exception as e:
        print(f"lmstudio-panel: ledger write failed ({e!r})", file=sys.stderr)
    return event


def _normalize(e: dict) -> dict:
    """Map any known ledger schema to the skill's event shape. Currently:
    the skill's own schema (passthrough) and the tokens_in/tokens_out shape
    written by in-repo LLM-client hooks (e.g. token_ledger.py)."""
    if "tokens_in" in e or "tokens_out" in e:
        return {"schema": e.get("schema"), "ts": e.get("ts", ""),
                "duration_s": e.get("duration_s"),
                "provider": e.get("provider", "?"),
                "machine": e.get("machine", machine_name()),
                "user": e.get("user", whoami()),
                "project": e.get("project", "?"), "model": e.get("model", "?"),
                "prompt_tokens": e.get("tokens_in") or 0,
                "completion_tokens": e.get("tokens_out") or 0,
                "reasoning_tokens": e.get("reasoning_tokens"),
                "task_tag": e.get("task_tag")}
    return e


def is_local_event(e: dict) -> bool:
    return e.get("provider") in ("lmstudio", "local", "ollama", "mlx")


def read_ledgers(days: float = None, scope: str = "skill"):
    """Events from the ledger dir. scope='skill' reads only this skill's
    lmstudio-*.jsonl files; scope='all' reads EVERY *.jsonl (any writer,
    any schema _normalize knows), so reports cover a whole org drop-dir."""
    events = []
    if not ledger_dir().is_dir():
        return events
    cutoff = None
    if days:
        cutoff = (datetime.datetime.now().astimezone()
                  - datetime.timedelta(days=days))
    pattern = "*.jsonl" if scope == "all" else "lmstudio-*.jsonl"
    for path in sorted(ledger_dir().glob(pattern)):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                e = _normalize(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
            if not e.get("ts"):
                continue
            if cutoff:
                try:
                    if datetime.datetime.fromisoformat(e["ts"]) < cutoff:
                        continue
                except (KeyError, ValueError):
                    pass
            events.append(e)
    return events


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


# ----------------------------------------------------------------- report
def _fmt_tokens(n):
    return f"{n/1e6:.1f}M" if n >= 1e6 else (f"{n/1e3:.1f}k" if n >= 1e3 else str(n))


def aggregate(events, by="project"):
    """Group events -> {key: {calls, prompt, completion, reasoning, wall_s}}."""
    groups = defaultdict(lambda: {"calls": 0, "prompt": 0, "completion": 0,
                                  "reasoning": 0, "wall_s": 0.0})
    for e in events:
        if by == "project-model":
            key = f"{e.get('project','?')} / {e.get('model','?')}"
        else:
            key = e.get(by if by != "project" else "project", "?") or "?"
        g = groups[key]
        g["calls"] += 1
        g["prompt"] += e.get("prompt_tokens") or 0
        g["completion"] += e.get("completion_tokens") or 0
        g["reasoning"] += e.get("reasoning_tokens") or 0
        g["wall_s"] += e.get("duration_s") or 0
    return dict(groups)


def hourly_windows(events):
    """Tokens per clock hour -> (windows dict, mean, peak_key, burst_ratio)."""
    windows = defaultdict(int)
    for e in events:
        try:
            hour = e["ts"][:13]  # YYYY-MM-DDTHH
        except (KeyError, TypeError):
            continue
        windows[hour] += (e.get("prompt_tokens") or 0) + \
                         (e.get("completion_tokens") or 0)
    if not windows:
        return {}, 0, None, 0.0
    mean = sum(windows.values()) / len(windows)
    peak = max(windows, key=windows.get)
    burst = windows[peak] / mean if mean else 0.0
    return dict(windows), mean, peak, burst


def print_report(by="project-model", windows=False, days=None):
    events = read_ledgers(days=days, scope="all")
    if not events:
        print(f"no ledger events found in {ledger_dir()}")
        return
    span = f"last {days:g} days" if days else "all time"
    print(f"lmstudio-panel usage report ({span}, {len(events)} calls, "
          f"{len(set(e.get('user') for e in events))} user(s))")
    print(f"{'group':44s} {'calls':>6s} {'prompt':>8s} {'compl':>8s} "
          f"{'reason':>8s} {'wall':>8s}")
    for key, g in sorted(aggregate(events, by).items(),
                         key=lambda kv: -kv[1]["prompt"]):
        print(f"{key[:44]:44s} {g['calls']:6d} {_fmt_tokens(g['prompt']):>8s} "
              f"{_fmt_tokens(g['completion']):>8s} "
              f"{_fmt_tokens(g['reasoning']):>8s} {g['wall_s']:7.0f}s")
    if windows:
        w, mean, peak, burst = hourly_windows(events)
        print(f"\nhourly windows: {len(w)} active hours, "
              f"mean {_fmt_tokens(int(mean))} tok/hr, "
              f"peak {peak} ({_fmt_tokens(w[peak])}), "
              f"burst ratio {burst:.1f}x"
              + ("  <- bursty" if burst >= 3 else "  <- steady"))


# ------------------------------------------------------------------ prices
# Append-only, dated per-model price series (Sage's paradigm, 2026-07-27):
# `prices update` fetches the LiteLLM community price table (the same data
# ccusage uses) and APPENDS today's entries — old entries are never edited,
# and reports join as-of (latest entry dated <= the usage event). Open-weight
# models are priced at their cheapest listed HOSTED rate: the honest
# counterfactual "you could have rented this exact model for $X". Models
# with no hosted listing fall back to the dated reference benchmark.
LITELLM_PRICES_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/"
                      "main/model_prices_and_context_window.json")


def prices_file() -> Path:
    return ledger_dir() / "prices.jsonl"


def normalize_model_name(name: str) -> str:
    """'lmstudio-community/Qwen3-32B-MLX-4bit' -> 'qwen3-32b' etc."""
    n = name.lower().split(":")[0]
    if "/" in n:
        n = n.split("/")[-1]
    for suf in ("-mlx-4bit", "-mlx-8bit", "-gguf", "-mxfp4", "-4bit",
                "-8bit", "-mlx"):
        n = n.removesuffix(suf)
    return n


_PRECISION_SUFFIXES = ("-fp8", "-fp16", "-bf16", "-awq", "-int4", "-int8",
                       "-quantized")


def _same_model(target: str, key_norm: str) -> bool:
    """True only for the SAME model: exact normalized match, or the key is
    target + a precision/quant suffix. A bare prefix match is NOT enough —
    'gpt-5' must not match 'gpt-5-nano' (a different, cheaper model)."""
    if key_norm == target:
        return True
    if key_norm.startswith(target):
        rest = key_norm[len(target):]
        if rest in _PRECISION_SUFFIXES:
            return True
        # dated snapshot of the same model, e.g. claude-sonnet-5-20250929
        return (len(rest) == 9 and rest.startswith("-20")
                and rest[1:].isdigit())
    return False


def match_price(model: str, table: dict):
    """Cheapest hosted chat-mode rate for exactly `model` in a LiteLLM-shaped
    table. Returns (input_per_m, output_per_m, matched_key) or None."""
    target = normalize_model_name(model)
    if not target:
        return None
    best = None
    for key, info in table.items():
        if not isinstance(info, dict) or info.get("mode") not in (None, "chat"):
            continue
        if not _same_model(target, normalize_model_name(key)):
            continue
        ci, co = info.get("input_cost_per_token"), info.get("output_cost_per_token")
        if not ci or not co:
            continue
        cand = (ci * 1e6, co * 1e6, key)
        if best is None or (cand[0] + cand[1]) < (best[0] + best[1]):
            best = cand
    return best


def prices_update(models=None):
    """Fetch current hosted rates and append dated entries (never edits)."""
    if models is None:
        models = sorted({e.get("model", "") for e in read_ledgers(scope="all")})
    with urllib.request.urlopen(LITELLM_PRICES_URL, timeout=30) as r:
        table = json.loads(r.read())
    today = datetime.date.today().isoformat()
    rates = CLOUD_REFERENCE_RATES[CURRENT_RATE_KEY]
    entries = [{"ts": today, "model": "reference",
                "input_per_m": rates["input_per_m"],
                "output_per_m": rates["output_per_m"],
                "source": f"builtin:{CURRENT_RATE_KEY}"}]
    for m in models:
        hit = match_price(m, table)
        if hit:
            entries.append({"ts": today, "model": normalize_model_name(m),
                            "input_per_m": round(hit[0], 4),
                            "output_per_m": round(hit[1], 4),
                            "source": f"litellm:{hit[2]}"})
    with _write_lock:
        ledger_dir().mkdir(parents=True, exist_ok=True)
        with open(prices_file(), "a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    for e in entries:
        print(f"{e['ts']} {e['model']:32s} in=${e['input_per_m']:<8g} "
              f"out=${e['output_per_m']:<8g} ({e['source']})")
    print(f"appended {len(entries)} dated entries -> {prices_file()}")


def load_price_series() -> dict:
    """{normalized_model: [(ts, in_per_m, out_per_m), ...] sorted by ts}."""
    series = defaultdict(list)
    if prices_file().exists():
        for line in prices_file().read_text().splitlines():
            try:
                e = json.loads(line)
                series[e["model"]].append(
                    (e["ts"], e["input_per_m"], e["output_per_m"]))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    for v in series.values():
        v.sort()
    return dict(series)


def as_of_price(model: str, ts: str, series: dict):
    """Latest price entry dated <= ts for model (else reference, else the
    builtin table). Returns (in_per_m, out_per_m, source)."""
    day = (ts or "")[:10]
    for key, src in ((normalize_model_name(model), "model"),
                     ("reference", "reference")):
        rows = [r for r in series.get(key, []) if r[0] <= day] or None
        if rows:
            return rows[-1][1], rows[-1][2], src
    rates = CLOUD_REFERENCE_RATES[CURRENT_RATE_KEY]
    return rates["input_per_m"], rates["output_per_m"], "builtin"


# ------------------------------------------------------------------ ingest
# Subscription-usage importers (2026-07-27, Sage's defend-the-plan case):
# flat-fee tools (Claude Code Max, Codex) don't hit the ledger, but their
# local transcripts record per-message tokens. `ingest claude-code` derives
# ledger events from ~/.claude/projects transcripts into ONE derived file,
# merged by message UUID — re-running never duplicates, and history already
# ingested survives the tool's own transcript pruning. Records carry
# subscription=true: reports price them at API rates as "what the plan
# absorbed", never mixed into local savings or cloud spend.
INGEST_CONTACT = "sage.arbor@duke.edu"


def _ingest_derived_file(tool: str) -> Path:
    return ledger_dir() / f"{tool}-{whoami()}-{machine_name()}.jsonl"


def _ingest_error(tool: str, n_errors: int, sample: str):
    log = ledger_dir() / f"ingest-errors-{tool}.log"
    with open(log, "a") as f:
        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} "
                f"{n_errors} unparsed lines; sample: {sample[:400]}\n")
    print(f"ingest {tool}: {n_errors} lines not understood — logged to {log}."
          f"\nPlease email that file to {INGEST_CONTACT} so the importer "
          f"can be fixed.", file=sys.stderr)


def ingest_claude_code():
    """Derive subscription-usage events from Claude Code transcripts."""
    src = Path.home() / ".claude" / "projects"
    if not src.is_dir():
        print("ingest claude-code: no ~/.claude/projects found — nothing to do")
        return
    out = _ingest_derived_file("claude-code")
    existing = {}
    if out.exists():
        for line in out.read_text().splitlines():
            try:
                e = json.loads(line)
                existing[e.get("src_id")] = e
            except json.JSONDecodeError:
                continue
    n_new = n_err = 0
    sample_err = ""
    for f in src.glob("*/*.jsonl"):
        for line in f.read_text(errors="replace").splitlines():
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
                msg = d.get("message") or {}
                u = msg.get("usage") or {}
                tin = (u.get("input_tokens") or 0) + \
                      (u.get("cache_creation_input_tokens") or 0)
                tout = u.get("output_tokens") or 0
                cr = u.get("cache_read_input_tokens") or 0
                if not (tin or tout or cr):
                    continue
                sid = d.get("uuid") or f"{f.name}:{d.get('timestamp')}"
                if sid in existing:
                    continue
                cwd = d.get("cwd") or ""
                existing[sid] = {
                    "schema": SCHEMA_VERSION, "src_id": sid,
                    "ts": (d.get("timestamp") or "").replace("Z", "+00:00"),
                    "provider": "claude-code", "subscription": True,
                    "machine": machine_name(), "user": whoami(),
                    "project": Path(cwd).name if cwd else f.parent.name,
                    "model": msg.get("model") or "claude-unknown",
                    "prompt_tokens": tin, "completion_tokens": tout,
                    "cache_read_tokens": cr, "task_tag": "claude-code"}
                n_new += 1
            except Exception as e:
                n_err += 1
                sample_err = sample_err or f"{e!r}: {line[:200]}"
    with _write_lock:
        ledger_dir().mkdir(parents=True, exist_ok=True)
        with open(out, "w") as fh:
            for e in existing.values():
                fh.write(json.dumps(e) + "\n")
    print(f"ingest claude-code: {n_new} new events, {len(existing)} total "
          f"-> {out}")
    if n_err:
        _ingest_error("claude-code", n_err, sample_err)


def ingest_codex():
    """Codex CLI importer — stub until a real session format is in hand."""
    src = Path.home() / ".codex"
    if not src.is_dir():
        print("ingest codex: no ~/.codex found on this machine — nothing to do")
        return
    files = list(src.rglob("*.jsonl"))
    print(f"ingest codex: found {len(files)} session file(s) but this "
          f"importer doesn't know the Codex format yet.\nPlease email one "
          f"redacted sample file to {INGEST_CONTACT} so it can be added.")


# ------------------------------------------------------------- html report
# Colors: validated categorical palette (dataviz reference instance);
# slots 3-4 sit <3:1 on the surface, so every bar carries a value label.
_C = {"local": "#2a78d6", "cloud": "#008300", "ink": "#1c2733",
      "muted": "#5b6b7b", "grid": "#e3e8ee", "surface": "#fcfcfb"}


def _all_usage_events():
    """Every event any writer dropped in the ledger dir, in chart shape."""
    return [{"ts": e["ts"], "local": is_local_event(e),
             "sub": bool(e.get("subscription")),
             "model": e.get("model", "?"), "project": e.get("project", "?"),
             "tin": e.get("prompt_tokens") or 0,
             "tout": e.get("completion_tokens") or 0,
             "cr": e.get("cache_read_tokens") or 0}
            for e in read_ledgers(scope="all")]


def _fmt_m(n):
    return f"{n/1e9:.2f}B" if n >= 1e9 else (f"{n/1e6:.1f}M" if n >= 1e6
                                             else f"{n/1e3:.0f}k" if n >= 1e3 else str(n))


def _agg_rows(events, series):
    """Pre-aggregate events for the interactive report: one row per
    (time bucket, model, project, locality) with token sums, call count,
    and as-of-priced cost. Small enough to embed; rich enough to filter."""
    span_days = (max(e["ts"] for e in events)[:10]
                 != min(e["ts"] for e in events)[:10])
    blen = 10 if span_days else 13  # daily vs hourly buckets
    agg = {}
    for e in events:
        key = (e["ts"][:blen], e["model"], e["project"], bool(e["local"]),
               bool(e.get("sub")))
        r = agg.setdefault(key, {"i": 0, "o": 0, "c": 0, "s": 0.0})
        r["i"] += e["tin"]
        r["o"] += e["tout"]
        r["c"] += 1
        pin, pout, _src = as_of_price(e["model"], e["ts"], series)
        # Cache reads (subscription transcripts) priced at 10% of input rate.
        r["s"] += (e["tin"] / 1e6 * pin + e["tout"] / 1e6 * pout
                   + e.get("cr", 0) / 1e6 * pin * 0.1)
    rows = [{"b": b, "m": m, "p": p, "l": l, "u": u, "i": r["i"],
             "o": r["o"], "c": r["c"], "s": round(r["s"], 4)}
            for (b, m, p, l, u), r in agg.items()]
    rows.sort(key=lambda r: r["b"])
    return rows, ("daily" if blen == 10 else "hourly")


# Plain string template (NOT an f-string: CSS/JS braces stay literal).
# Placeholders: @@GENERATED@@ @@SPAN@@ @@DATA@@ @@TABLE@@ @@PRICENOTE@@
_HTML_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM usage ledger report</title><style>
body{font-family:-apple-system,Segoe UI,sans-serif;background:#fcfcfb;
 color:#1c2733;max-width:780px;margin:0 auto;padding:20px;line-height:1.4}
h1{font-size:1.15rem} h2{font-size:.95rem;margin:18px 0 6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:8px}
.tile{background:#fff;border:1px solid #e3e8ee;border-radius:8px;
 padding:10px;text-align:center}.tile b{display:block;font-size:1.05rem}
.tile span{font-size:.7rem;color:#5b6b7b}
.chip{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px}
.note{font-size:.72rem;color:#5b6b7b}
.filters{background:#fff;border:1px solid #e3e8ee;border-radius:8px;
 padding:8px 12px;margin:10px 0;font-size:.82rem}
.scope button{border:1px solid #e3e8ee;background:#fff;border-radius:14px;
 padding:3px 12px;margin-right:6px;cursor:pointer;font-size:.8rem}
.scope button.on{background:#1c2733;color:#fff;border-color:#1c2733}
#mlist{display:flex;flex-wrap:wrap;gap:2px 14px;margin-top:6px;max-height:130px;
 overflow-y:auto}
#mlist label{white-space:nowrap;cursor:pointer}
#mlist input{vertical-align:-2px}
.mini{border:none;background:none;color:#2a78d6;cursor:pointer;font-size:.78rem;
 text-decoration:underline;padding:0 4px}
table{border-collapse:collapse;font-size:.8rem}
td{border:1px solid #e3e8ee;padding:3px 8px}
summary{cursor:pointer;font-size:.85rem;margin-top:14px}
.empty{color:#5b6b7b;font-size:.85rem;padding:18px 0}
</style></head><body>
<h1>LLM usage ledger report</h1>
<div class="note">generated @@GENERATED@@ · ledger dir: ~/.llm_token_ledger
 (all *.jsonl writers merged) · @@SPAN@@</div>
<div class="filters">
 <span class="scope" id="clsChips"><button data-c="local">Local</button><button data-c="cloud">Cloud</button><button data-c="sub">Subscription</button></span>
 &nbsp;·&nbsp;
 <span class="scope" id="metricChips"><button data-m="usd" class="on">$</button><button data-m="tok">tokens</button></span>
 <details><summary>Models (<span id="mCount"></span> shown)
  <button class="mini" id="mAll">all</button><button class="mini" id="mNone">none</button></summary>
  <div id="mlist"></div></details>
</div>
<div class="tiles" id="tiles"></div>
<h2><span id="metricWord">$</span> over time (<span id="gran"></span>)
 <span id="legend"></span></h2>
<div id="timeline"></div>
<h2>Models by tokens <span class="note">(within current filters)</span></h2>
<div id="modelbars"></div>
<h2>Tokens by project <span class="note">(within current filters)</span></h2>
<div id="projbars"></div>
<noscript><p class="note">Charts need JavaScript — the data table below is
 complete and static.</p></noscript>
<details><summary>Data table (all models, unfiltered)</summary>
<table><tr><td><b>model</b></td><td><b>where</b></td><td><b>calls</b></td>
<td><b>tokens in</b></td><td><b>tokens out</b></td></tr>@@TABLE@@</table></details>
<p class="note">*money = tokens priced as-of each event's date against the
 append-only price series (@@PRICENOTE@@). For local rows that is COST AVOIDED
 (marginal cost paid: $0); for cloud rows it is estimated spend at surrogate
 public rates, not an Azure invoice.</p>
<script id="d" type="application/json">@@DATA@@</script>
<script>
(function(){
"use strict";
var D; try{ D = JSON.parse(document.getElementById("d").textContent); }
catch(e){ document.getElementById("timeline").textContent =
  "report data failed to load: " + e; return; }
var C = {local:"#2a78d6", cloud:"#008300", sub:"#e87ba4", ink:"#1c2733",
         muted:"#5b6b7b", grid:"#e3e8ee"};
var visible = {local:true, cloud:true, sub:true};
var metric = "usd", sel = null;   // sel === null -> all models
var NS = "http://www.w3.org/2000/svg";
function cls(r){ return r.u ? "sub" : (r.l ? "local" : "cloud"); }
function val(r){ return metric === "usd" ? r.s : r.i + r.o; }

function fmt(n){ n = Math.round(n);
  return n >= 1e9 ? (n/1e9).toFixed(2)+"B" : n >= 1e6 ? (n/1e6).toFixed(1)+"M"
       : n >= 1e3 ? (n/1e3).toFixed(0)+"k" : String(n); }
function fmtV(n){
  if (metric !== "usd") return fmt(n);
  return n >= 1000 ? "$" + (n/1000).toFixed(1) + "k"
       : n >= 100 ? "$" + n.toFixed(0)
       : n >= 1 ? "$" + n.toFixed(2) : "$" + n.toFixed(3); }
function el(tag, attrs, parent, text){
  var e = document.createElementNS(NS, tag);
  for (var k in attrs) e.setAttribute(k, attrs[k]);
  if (text !== undefined) e.textContent = text;
  parent.appendChild(e); return e; }
function div(id){ var d = document.getElementById(id);
  while (d.firstChild) d.removeChild(d.firstChild); return d; }
function rows(){
  return D.rows.filter(function(r){
    if (!visible[cls(r)]) return false;
    return !sel || sel.has(r.m); }); }

function modelTotals(rs){
  var t = {};
  rs.forEach(function(r){
    if (!t[r.m]) t[r.m] = {v:0, k:cls(r)};
    t[r.m].v += val(r); });
  return Object.keys(t).map(function(m){ return {m:m, v:t[m].v, k:t[m].k}; })
    .sort(function(a,b){ return b.v - a.v; }); }

function tiles(rs){
  var calls=0, tin=0, tout=0, saved=0, spend=0, subv=0;
  rs.forEach(function(r){ calls += r.c; tin += r.i; tout += r.o;
    if (r.u) subv += r.s; else if (r.l) saved += r.s; else spend += r.s; });
  var t = div("tiles");
  [["calls", calls.toLocaleString()],
   ["tokens in / out", fmt(tin) + " / " + fmt(tout)],
   ["local cost avoided*", "$" + saved.toFixed(2)],
   ["cloud est. spend*", "$" + spend.toFixed(2)],
   ["subscription @ API rates*", "$" + subv.toFixed(2)],
   ["models shown", String(modelTotals(rs).length)]
  ].forEach(function(kv){
    var d = document.createElement("div"); d.className = "tile";
    var b = document.createElement("b"); b.textContent = kv[1];
    var s = document.createElement("span"); s.textContent = kv[0];
    d.appendChild(b); d.appendChild(s); t.appendChild(d); }); }

function timeline(rs){
  var host = div("timeline");
  var buckets = []; var seen = {};
  rs.forEach(function(r){ if(!seen[r.b]){ seen[r.b]=1; buckets.push(r.b);} });
  buckets.sort(); buckets = buckets.slice(-60);
  if (!buckets.length){ host.textContent = "no events match the current filters";
    host.className = "empty"; return; }
  host.className = "";
  var byb = {local:{}, cloud:{}, sub:{}};
  rs.forEach(function(r){
    var k = cls(r);
    byb[k][r.b] = (byb[k][r.b] || 0) + val(r); });
  var names = ["local","cloud","sub"].filter(function(n){
    return visible[n]; });
  var mx = metric === "usd" ? 0.01 : 1;
  buckets.forEach(function(b){ names.forEach(function(n){
    mx = Math.max(mx, byb[n][b] || 0); }); });
  var pad = 10 + fmtV(mx).length * 7.5;
  var w = 720, h = 190, iw = w - pad - 40, ih = h - 42;
  var svg = el("svg", {viewBox: "0 0 " + w + " " + h, role: "img",
                       "font-family": "sans-serif"}, host);
  [0, 0.5, 1].forEach(function(f){
    var y = 8 + ih * (1 - f);
    el("line", {x1:pad, y1:y, x2:w-8, y2:y, stroke:C.grid,
                "stroke-width":1}, svg);
    el("text", {x:pad-5, y:y+4, "text-anchor":"end", "font-size":10,
                fill:C.muted}, svg, fmtV(mx*f)); });
  names.forEach(function(n){
    var pts = buckets.map(function(b, i){
      return [pad + iw * (buckets.length < 2 ? 0.5 : i/(buckets.length-1)),
              8 + ih * (1 - (byb[n][b] || 0)/mx)]; });
    el("polyline", {points: pts.map(function(p){
        return p[0].toFixed(0) + "," + p[1].toFixed(0); }).join(" "),
      fill:"none", stroke:C[n], "stroke-width":2}, svg);
    pts.forEach(function(p, i){
      var v = byb[n][buckets[i]] || 0; if (!v) return;
      var c = el("circle", {cx:p[0].toFixed(0), cy:p[1].toFixed(0), r:4,
                            fill:C[n]}, svg);
      el("title", {}, c, buckets[i] + " " + n + ": " + fmtV(v)); }); });
  el("text", {x:pad, y:h-6, "font-size":10, fill:C.muted}, svg, buckets[0]);
  el("text", {x:w-8, y:h-6, "text-anchor":"end", "font-size":10,
              fill:C.muted}, svg, buckets[buckets.length-1]);
  var lg = document.getElementById("legend");
  while (lg.firstChild) lg.removeChild(lg.firstChild);
  names.forEach(function(n){
    var chip = document.createElement("span");
    chip.className = "chip"; chip.style.background = C[n];
    lg.appendChild(chip);
    lg.appendChild(document.createTextNode(" " + n + "  ")); }); }

function bars(hostId, pairs, colorOf){
  var host = div(hostId);
  if (!pairs.length){ host.textContent = "no events match the current filters";
    host.className = "empty"; return; }
  host.className = "";
  var shown = pairs.slice(0, 15);
  var mx = shown[0].v || 1;
  var bh = 22, gap = 2, w = 720;
  var svg = el("svg", {viewBox: "0 0 " + w + " " + (shown.length*(bh+gap)+4),
                       role:"img", "font-family":"sans-serif"}, host);
  shown.forEach(function(p, i){
    var y = i * (bh + gap);
    var bw = Math.max(3, (w - 300) * p.v / mx);
    el("text", {x:215, y:y+bh-6, "text-anchor":"end", "font-size":12,
                fill:C.ink}, svg, p.m.length > 32 ? p.m.slice(0,31)+"…" : p.m);
    var r = el("rect", {x:222, y:y, width:bw.toFixed(0), height:bh-4, rx:2,
                        fill:colorOf(p)}, svg);
    el("title", {}, r, p.m + ": " + fmtV(p.v));
    el("text", {x:(226+bw).toFixed(0), y:y+bh-6, "font-size":12,
                fill:C.muted}, svg, fmtV(p.v)); });
  if (pairs.length > shown.length){
    var n = document.createElement("div"); n.className = "note";
    n.textContent = "(+" + (pairs.length - shown.length) + " more in the table)";
    host.appendChild(n); } }

function refreshChips(){
  document.querySelectorAll("#clsChips button").forEach(function(b){
    var c = b.getAttribute("data-c"), on = visible[c];
    b.className = on ? "on" : "";
    b.style.background = on ? C[c] : "";
    b.style.borderColor = on ? C[c] : ""; });
  document.querySelectorAll("#metricChips button").forEach(function(b){
    b.className = b.getAttribute("data-m") ===
      (metric === "usd" ? "usd" : "tok") ? "on" : ""; }); }

function render(){
  refreshChips();
  document.getElementById("metricWord").textContent =
    metric === "usd" ? "$" : "Tokens";
  var rs = rows();
  tiles(rs);
  timeline(rs);
  var mt = modelTotals(rs);
  document.getElementById("mCount").textContent = String(mt.length);
  bars("modelbars", mt, function(p){ return C[p.k] || C.cloud; });
  var pt = {};
  rs.forEach(function(r){ pt[r.p] = (pt[r.p] || 0) + val(r); });
  bars("projbars", Object.keys(pt).map(function(p){
      return {m:p, v:pt[p]}; }).sort(function(a,b){ return b.v-a.v; }),
    function(){ return C.ink; }); }

document.getElementById("gran").textContent = D.granularity;
var allModels = modelTotals(D.rows);
var ml = document.getElementById("mlist");
allModels.forEach(function(p){
  var lab = document.createElement("label");
  var cb = document.createElement("input");
  cb.type = "checkbox"; cb.checked = true; cb.value = p.m;
  cb.addEventListener("change", function(){
    var boxes = ml.querySelectorAll("input");
    var on = []; boxes.forEach(function(b){ if (b.checked) on.push(b.value); });
    sel = on.length === boxes.length ? null : new Set(on);
    render(); });
  lab.appendChild(cb);
  var chip = document.createElement("span"); chip.className = "chip";
  chip.style.background = C[p.k] || C.cloud;
  lab.appendChild(chip);
  lab.appendChild(document.createTextNode(" " + p.m + " (" + fmtV(p.v) + ")"));
  ml.appendChild(lab); });
function setAll(state){
  ml.querySelectorAll("input").forEach(function(b){ b.checked = state; });
  sel = state ? null : new Set(); render(); }
document.getElementById("mAll").addEventListener("click",
  function(e){ e.preventDefault(); setAll(true); });
document.getElementById("mNone").addEventListener("click",
  function(e){ e.preventDefault(); setAll(false); });
document.querySelectorAll("#clsChips button").forEach(function(b){
  b.addEventListener("click", function(){
    var c = b.getAttribute("data-c");
    visible[c] = !visible[c];
    if (!visible.local && !visible.cloud && !visible.sub)
      visible[c] = true;   // never allow an all-off dead end
    render(); }); });
document.querySelectorAll("#metricChips button").forEach(function(b){
  b.addEventListener("click", function(){
    metric = b.getAttribute("data-m") === "usd" ? "usd" : "tok";
    render(); }); });
render();
})();
</script>
</body></html>"""


def html_report(path, days=None):
    events = _all_usage_events()
    if days:
        cutoff = (datetime.datetime.now().astimezone()
                  - datetime.timedelta(days=days)).isoformat()
        events = [e for e in events if e["ts"] >= cutoff]
    if not events:
        raise SystemExit("no usage events found in either ledger")
    series = load_price_series()
    rows, granularity = _agg_rows(events, series)

    priced = {normalize_model_name(e["model"]) for e in events if e["local"]
              and as_of_price(e["model"], e["ts"], series)[2] == "model"}
    price_note = (f"per-model hosted rates for {len(priced)} local model(s), "
                  "reference benchmark for the rest" if priced else
                  "reference benchmark only — run `prices update` "
                  "for per-model hosted rates")

    # Static, JS-independent fallback table over ALL models.
    by_model = {}
    for e in events:
        r = by_model.setdefault(e["model"], {"c": 0, "i": 0, "o": 0,
                                             "l": e["local"]})
        r["c"] += 1
        r["i"] += e["tin"]
        r["o"] += e["tout"]
    table = "".join(
        f"<tr><td>{escape_html(m)}</td><td>{'local' if r['l'] else 'cloud'}</td>"
        f"<td>{r['c']:,}</td><td>{r['i']:,}</td><td>{r['o']:,}</td></tr>"
        for m, r in sorted(by_model.items(), key=lambda kv: -(kv[1]["i"]
                                                              + kv[1]["o"])))

    data_json = json.dumps({"rows": rows, "granularity": granularity}
                           ).replace("</", "<\\/")
    html = (_HTML_TEMPLATE
            .replace("@@GENERATED@@", datetime.datetime.now().astimezone()
                     .isoformat(timespec="minutes"))
            .replace("@@SPAN@@", ("last %g days" % days) if days else "all time")
            .replace("@@TABLE@@", table)
            .replace("@@PRICENOTE@@", escape_html(price_note))
            .replace("@@DATA@@", data_json))
    Path(path).write_text(html)
    n_local = sum(1 for e in events if e["local"])
    print(f"wrote {path} ({len(events):,} events, {n_local:,} local, "
          f"{len(rows):,} chart rows)")


def escape_html(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


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
    p = sub.add_parser("report")
    p.add_argument("--by", default="project-model",
                   choices=["project", "model", "user", "project-model"])
    p.add_argument("--windows", action="store_true")
    p.add_argument("--days", type=float)
    p.add_argument("--html", nargs="?", const="llm_usage_report.html",
                   metavar="PATH", help="write a self-contained graphical "
                   "HTML report (charts over BOTH ledgers) to PATH")
    p = sub.add_parser("prices", help="append-only dated price series")
    p.add_argument("action", choices=["update", "list"],
                   help="update: fetch hosted rates for ledger models and "
                        "append dated entries; list: show the series")
    p = sub.add_parser("ingest", help="import subscription-tool usage "
                       "(flat-fee plans) into the ledger as derived events")
    p.add_argument("tool", choices=["claude-code", "codex"])
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
    if a.cmd == "report":
        if a.html:
            html_report(a.html, days=a.days)
        else:
            print_report(by=a.by, windows=a.windows, days=a.days)
        return 0
    if a.cmd == "prices":
        if a.action == "update":
            prices_update()
        else:
            for model, rows in sorted(load_price_series().items()):
                for ts, pin, pout in rows:
                    print(f"{ts} {model:32s} in=${pin:<8g} out=${pout:g}")
        return 0
    if a.cmd == "ingest":
        (ingest_claude_code if a.tool == "claude-code" else ingest_codex)()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
