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
    return platform.node().split(".")[0] or "unknown"


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
        return rest in _PRECISION_SUFFIXES
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


# ------------------------------------------------------------- html report
# Colors: validated categorical palette (dataviz reference instance);
# slots 3-4 sit <3:1 on the surface, so every bar carries a value label.
_C = {"local": "#2a78d6", "cloud": "#008300", "ink": "#1c2733",
      "muted": "#5b6b7b", "grid": "#e3e8ee", "surface": "#fcfcfb"}


def _all_usage_events():
    """Every event any writer dropped in the ledger dir, in chart shape."""
    return [{"ts": e["ts"], "local": is_local_event(e),
             "model": e.get("model", "?"), "project": e.get("project", "?"),
             "tin": e.get("prompt_tokens") or 0,
             "tout": e.get("completion_tokens") or 0}
            for e in read_ledgers(scope="all")]


def _fmt_m(n):
    return f"{n/1e9:.2f}B" if n >= 1e9 else (f"{n/1e6:.1f}M" if n >= 1e6
                                             else f"{n/1e3:.0f}k" if n >= 1e3 else str(n))


def _bars_svg(rows, color_fn, w=680, bh=22, gap=2):
    """Horizontal bar chart with direct value labels (relief rule)."""
    if not rows:
        return "<p>no data</p>"
    mx = max(v for _, v in rows) or 1
    h = len(rows) * (bh + gap) + 4
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" font-family="sans-serif">']
    for i, (label, v) in enumerate(rows):
        y = i * (bh + gap)
        bw = max(3, (w - 260) * v / mx)
        parts.append(
            f'<text x="200" y="{y+bh-6}" text-anchor="end" font-size="12" '
            f'fill="{_C["ink"]}">{label[:30]}</text>'
            f'<rect x="208" y="{y}" width="{bw:.0f}" height="{bh-4}" rx="2" '
            f'fill="{color_fn(label)}"><title>{label}: {v:,} tokens</title></rect>'
            f'<text x="{212+bw:.0f}" y="{y+bh-6}" font-size="12" '
            f'fill="{_C["muted"]}">{_fmt_m(v)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _timeline_svg(buckets_local, buckets_cloud, keys, w=680, h=180):
    """Two thin lines (local vs cloud tokens per bucket) + hover dots."""
    if not keys:
        return "<p>no data</p>"
    mx = max([buckets_local.get(k, 0) + 0 for k in keys]
             + [buckets_cloud.get(k, 0) for k in keys]) or 1
    pad, iw, ih = 34, w - 44, h - 40

    def xy(i, v):
        x = pad + iw * (i / max(len(keys) - 1, 1))
        y = 8 + ih * (1 - v / mx)
        return x, y

    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" font-family="sans-serif">']
    for frac in (0, 0.5, 1):
        y = 8 + ih * (1 - frac)
        parts.append(f'<line x1="{pad}" y1="{y}" x2="{w-8}" y2="{y}" '
                     f'stroke="{_C["grid"]}" stroke-width="1"/>'
                     f'<text x="{pad-4}" y="{y+4}" text-anchor="end" font-size="10" '
                     f'fill="{_C["muted"]}">{_fmt_m(int(mx*frac))}</text>')
    for series, color, name in ((buckets_local, _C["local"], "local"),
                                (buckets_cloud, _C["cloud"], "cloud")):
        pts = [xy(i, series.get(k, 0)) for i, k in enumerate(keys)]
        path = " ".join(f"{x:.0f},{y:.0f}" for x, y in pts)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" '
                     f'stroke-width="2"/>')
        for i, (x, y) in enumerate(pts):
            v = series.get(keys[i], 0)
            if v:
                parts.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="4" fill="{color}">'
                             f'<title>{keys[i]} {name}: {v:,} tokens</title></circle>')
        ex, ey = pts[-1]
        parts.append(f'<text x="{ex+4:.0f}" y="{ey+4:.0f}" font-size="11" '
                     f'fill="{_C["ink"]}">{name}</text>')
    parts.append(f'<text x="{pad}" y="{h-6}" font-size="10" fill="{_C["muted"]}">'
                 f'{keys[0]}</text><text x="{w-8}" y="{h-6}" text-anchor="end" '
                 f'font-size="10" fill="{_C["muted"]}">{keys[-1]}</text></svg>')
    return "".join(parts)


def html_report(path, days=None):
    events = _all_usage_events()
    if days:
        cutoff = (datetime.datetime.now().astimezone()
                  - datetime.timedelta(days=days)).isoformat()
        events = [e for e in events if e["ts"] >= cutoff]
    if not events:
        raise SystemExit("no usage events found in either ledger")
    loc = [e for e in events if e["local"]]
    lin = sum(e["tin"] for e in loc)
    lout = sum(e["tout"] for e in loc)
    series = load_price_series()
    saved = 0.0
    priced_models = set()
    for e in loc:
        pin, pout, src = as_of_price(e["model"], e["ts"], series)
        saved += e["tin"] / 1e6 * pin + e["tout"] / 1e6 * pout
        if src == "model":
            priced_models.add(normalize_model_name(e["model"]))
    price_note = (f"per-model hosted rates for {len(priced_models)} model(s), "
                  f"reference benchmark for the rest"
                  if priced_models else
                  "reference benchmark only — run `prices update` for "
                  "per-model hosted rates")

    span_days = (max(e["ts"] for e in events)[:10] !=
                 min(e["ts"] for e in events)[:10])
    blen = 10 if span_days else 13  # daily vs hourly buckets
    bl, bc = defaultdict(int), defaultdict(int)
    for e in events:
        (bl if e["local"] else bc)[e["ts"][:blen]] += e["tin"] + e["tout"]
    keys = sorted(set(bl) | set(bc))[-60:]

    by_model = defaultdict(lambda: [0, False])
    for e in events:
        by_model[e["model"]][0] += e["tin"] + e["tout"]
        by_model[e["model"]][1] = e["local"]
    top = sorted(by_model.items(), key=lambda kv: -kv[1][0])[:10]
    model_rows = [(m, v[0]) for m, v in top]
    model_local = {m: v[1] for m, v in top}

    by_proj = defaultdict(int)
    for e in loc:
        by_proj[e["project"]] += e["tin"] + e["tout"]
    proj_rows = sorted(by_proj.items(), key=lambda kv: -kv[1])[:8]

    tiles = "".join(
        f'<div class="tile"><b>{v}</b><span>{k}</span></div>' for k, v in [
            ("local calls", f"{len(loc):,}"),
            ("local tokens in / out", f"{_fmt_m(lin)} / {_fmt_m(lout)}"),
            ("est. saved vs Azure*", f"${saved:,.2f}"),
            ("projects using local", f"{len(by_proj)}"),
            ("all-provider events", f"{len(events):,}"),
        ])
    legend = (f'<span class="chip" style="background:{_C["local"]}"></span> local '
              f'&nbsp; <span class="chip" style="background:{_C["cloud"]}"></span> cloud')
    table = "".join(f"<tr><td>{m}</td><td>{'local' if l else 'cloud'}</td>"
                    f"<td>{v:,}</td></tr>" for m, (v, l) in top)
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM usage ledger report</title><style>
body{{font-family:-apple-system,Segoe UI,sans-serif;background:{_C["surface"]};
 color:{_C["ink"]};max-width:760px;margin:0 auto;padding:20px;line-height:1.4}}
h1{{font-size:1.15rem}} h2{{font-size:.95rem;margin:20px 0 6px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:8px}}
.tile{{background:#fff;border:1px solid {_C["grid"]};border-radius:8px;
 padding:10px;text-align:center}} .tile b{{display:block;font-size:1.05rem}}
.tile span{{font-size:.7rem;color:{_C["muted"]}}}
.chip{{display:inline-block;width:10px;height:10px;border-radius:2px}}
.note{{font-size:.72rem;color:{_C["muted"]}}}
table{{border-collapse:collapse;font-size:.8rem}} td{{border:1px solid {_C["grid"]};
 padding:3px 8px}} summary{{cursor:pointer;font-size:.85rem;margin-top:14px}}
</style></head><body>
<h1>LLM usage ledger report</h1>
<div class="note">generated {datetime.datetime.now().astimezone().isoformat(timespec="minutes")}
 · sources: ~/.llm_token_ledger/ledger.jsonl + lmstudio-*.jsonl
 · {"last %g days" % days if days else "all time"}</div>
<div class="tiles">{tiles}</div>
<h2>Tokens over time ({"daily" if blen == 10 else "hourly"}) &nbsp; {legend}</h2>
{_timeline_svg(bl, bc, keys)}
<h2>Top models by tokens</h2>
{_bars_svg(model_rows, lambda m: _C["local"] if model_local.get(m) else _C["cloud"])}
<h2>Local tokens by project</h2>
{_bars_svg(proj_rows, lambda _: _C["local"])}
<details><summary>Data table (top models)</summary>
<table><tr><td><b>model</b></td><td><b>provider</b></td><td><b>tokens</b></td></tr>
{table}</table></details>
<p class="note">*savings = local tokens priced at what the same usage would
 have cost hosted, joined as-of each event's date against the append-only
 price series ({price_note}). The marginal cost actually paid was $0.</p>
</body></html>"""
    Path(path).write_text(html)
    print(f"wrote {path} ({len(events):,} events, {len(loc):,} local)")


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
    return 1


if __name__ == "__main__":
    sys.exit(main())
