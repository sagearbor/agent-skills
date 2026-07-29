#!/usr/bin/env python3
"""agent-coach — a pair-programming coach for people driving AI coding agents.

A Stop hook scores each finished turn against best_practices.md using a cheap
model (Haiku) via `claude -p` (so it rides the user's EXISTING Claude Code
auth — no API keys, works for everyone). Per-category thresholds gate whether
a note is shown; thresholds start low (heavy coaching for a beginner) and rise
per-category as the user stops tripping that habit. Optional escalation sends
only low-certainty borderline calls to a smarter model (Sonnet).

Zero cloud keys. Cheap (one small Haiku call per substantive turn). Every
coaching decision is logged for a rollup dashboard and the quarterly scrub.

CLI:
  agent_coach.py hook              # (called by the Stop hook; reads stdin JSON)
  agent_coach.py install|uninstall # wire/unwire the Stop hook in settings.json
  agent_coach.py status            # per-category threshold table
  agent_coach.py set <cat> <0-1>   # set one category's threshold
  agent_coach.py quieter|louder    # nudge ALL thresholds +/-0.1
  agent_coach.py off|on            # silence (all -> 1.0) / restore
  agent_coach.py escalate <0-1>    # escalation certainty cutoff (0 = never)
  agent_coach.py rules-snapshot|rules-list|rules-revert <date>
  agent_coach.py dashboard [out.html]
"""
import argparse
import datetime
import getpass
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
RUBRIC = SKILL_DIR / "best_practices.md"
ARCHIVE = SKILL_DIR / "archive"
COACH_DIR = Path(os.environ.get("AGENT_COACH_DIR") or Path.home() / ".agent-coach")
CONFIG = COACH_DIR / "config.json"
BANNER = "~~~~~~~~~~~~~~~  AGENT COACH START  ~~~~~~~~~~~~~~~"
BANNER_END = "~~~~~~~~~~~~~~~~  AGENT COACH END  ~~~~~~~~~~~~~~~~"
SCORER_DEFAULT = "haiku"
ESCALATION_DEFAULT_MODEL = "sonnet"
CLEAN_STREAK_TO_RAISE = 8   # clean turns before a category's threshold rises
RAISE_STEP = 0.05
THRESHOLD_CAP = 0.9
START_THRESHOLD = 0.3       # low => beginner gets coached a lot, then it fades


# ------------------------------------------------------------------ identity
def whoami():
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def machine():
    return (os.environ.get("LLM_LEDGER_MACHINE")
            or platform.node().split(".")[0] or "unknown")


def events_file(shared=False):
    if shared:
        d = os.environ.get("AGENT_COACH_SHARED_DIR")
        if not d:
            return None
        base = Path(d)
    else:
        base = COACH_DIR
    return base / f"coach-{whoami()}-{machine()}.jsonl"


# --------------------------------------------------------------------- rubric
def load_rules():
    """[(num, category, text)] parsed from best_practices.md."""
    rules = []
    if not RUBRIC.exists():
        return rules
    for m in re.finditer(r"^\s*\d+\.\s+\*\*([a-z-]+)\*\*\s*(.*?)(?=^\s*\d+\.\s+\*\*|\Z)",
                         RUBRIC.read_text(), re.S | re.M):
        rules.append((m.group(1), " ".join(m.group(2).split())))
    return rules


def categories():
    return [c for c, _ in load_rules()]


# --------------------------------------------------------------------- config
def default_config():
    return {"enabled": True, "escalation_cutoff": 0.0,
            "scorer_model": SCORER_DEFAULT,
            "escalation_model": ESCALATION_DEFAULT_MODEL,
            "budget_daily_usd": None,   # None = unlimited
            "spend_date": "", "spend_today": 0.0,
            "turn": 0,
            "thresholds": {c: START_THRESHOLD for c in categories()},
            "clean_streak": {c: 0 for c in categories()}}


def load_config():
    cfg = default_config()
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    # keep thresholds in sync with any newly-added rules
    for c in categories():
        cfg["thresholds"].setdefault(c, START_THRESHOLD)
        cfg["clean_streak"].setdefault(c, 0)
    return cfg


def save_config(cfg):
    COACH_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1))


# ------------------------------------------------------------ transcript read
def extract_last_turn(transcript_path):
    """Compact summary of the most recent user->assistant turn. Returns
    (user_text, summary_lines, meta) or None if nothing substantive."""
    try:
        lines = [json.loads(l) for l in Path(transcript_path).read_text(
            errors="replace").splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return None
    # find the last genuine human message (type user, text content, not a
    # tool_result-only turn)
    start = None
    for i in range(len(lines) - 1, -1, -1):
        e = lines[i]
        if e.get("type") != "user":
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            start = i
            break
        if isinstance(content, list) and any(
                b.get("type") == "text" for b in content if isinstance(b, dict)):
            start = i
            break
    if start is None:
        return None

    turn = lines[start:]
    user_text = ""
    c0 = (turn[0].get("message") or {}).get("content")
    if isinstance(c0, str):
        user_text = c0
    elif isinstance(c0, list):
        user_text = " ".join(b.get("text", "") for b in c0
                             if isinstance(b, dict) and b.get("type") == "text")

    models, edits, writes, bashes, reads, tasks, tools = set(), [], [], [], [], 0, 0
    for e in turn:
        msg = e.get("message") or {}
        if msg.get("model"):
            models.add(msg["model"])
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            tools += 1
            name, inp = b.get("name", "?"), b.get("input", {}) or {}
            if name in ("Edit", "MultiEdit"):
                edits.append(inp.get("file_path", "?"))
            elif name == "Write":
                writes.append(inp.get("file_path", "?"))
            elif name == "Bash":
                bashes.append(str(inp.get("command", ""))[:80])
            elif name == "Read":
                reads.append(inp.get("file_path", "?"))
            elif name in ("Task", "Agent"):
                tasks += 1

    if tools == 0 and len(user_text) < 400:
        return None  # pure Q&A / trivial — nothing to coach (activity gate only)

    reread = {p: reads.count(p) for p in set(reads) if reads.count(p) > 2}
    summary = [
        f"model(s): {', '.join(sorted(models)) or 'unknown'}",
        f"tools used: {tools} total | edits={len(edits)} writes={len(writes)} "
        f"bash={len(bashes)} reads={len(reads)} subagents={tasks}",
    ]
    if edits or writes:
        summary.append("files changed: " + ", ".join(
            Path(p).name for p in (edits + writes))[:300])
    if reread:
        summary.append("re-read same file: " + ", ".join(
            f"{Path(p).name}x{n}" for p, n in reread.items()))
    if bashes:
        summary.append("bash: " + " ; ".join(bashes[:3]))
    touched = edits + writes + reads
    if any(".env" in p or "credential" in p.lower() for p in touched):
        summary.append("NOTE: a .env / credential-looking file was touched")
    return user_text, summary, {"models": sorted(models), "n_tools": tools}


# ------------------------------------------------------------------- scoring
def build_prompt(user_text, summary):
    rules = "\n".join(f"- {c}: {t}" for c, t in load_rules())
    return (
        "You are a terse pair-programming coach. Given what a user just did in "
        "one turn driving an AI coding agent, decide if they missed any of these "
        "best practices. BE CONSERVATIVE — only flag a clear, actionable miss; "
        "false positives are worse than misses.\n\n"
        f"BEST PRACTICES:\n{rules}\n\n"
        f"USER REQUEST (truncated):\n{user_text[:600]}\n\n"
        f"WHAT HAPPENED THIS TURN:\n" + "\n".join(summary) + "\n\n"
        "Respond with ONLY a JSON array (possibly empty). Each item: "
        '{"category": "<one of the ids above>", "severity": 0.0-1.0, '
        '"certainty": 0.0-1.0, "note": "one actionable sentence"}. '
        "severity = how big the missed opportunity; certainty = how sure you are.")


SYS = ("You are a terse pair-programming coach. Output ONLY a JSON array as "
       "instructed — no prose.")
HAIKU_API_MODEL = "claude-haiku-4-5"
HAIKU_IN_PER_M, HAIKU_OUT_PER_M = 1.0, 5.0  # $/M (Haiku 4.5), for budget math


def anthropic_score(api_model, prompt, key, timeout=30):
    """Direct minimal Anthropic API call — ~$0.001, ~1s. Used when the user
    is metered (ANTHROPIC_API_KEY present). Returns (text, usage) or (None,None).
    stdlib only; no SDK dependency."""
    import urllib.request
    body = json.dumps({"model": api_model, "max_tokens": 400, "system": SYS,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        obj = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        text = "".join(b.get("text", "") for b in obj.get("content", [])
                       if b.get("type") == "text")
        return text, obj.get("usage") or {}
    except Exception:
        return None, None


def claude_p_score(model, prompt, timeout=60):
    """Fallback for subscription users (no API key): `claude -p` uses their
    existing Claude Code auth. Costlier (CC's system-prompt overhead) but
    key-free. Recursion-guarded."""
    env = dict(os.environ, AGENT_COACH_ACTIVE="1")
    try:
        p = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "json", prompt],
            capture_output=True, text=True, timeout=timeout, env=env)
        if p.returncode != 0:
            return None, None
        obj = json.loads(p.stdout)
        return obj.get("result", ""), obj.get("usage") or {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError,
            OSError):
        return None, None


def call_model(model, prompt, timeout=60, api_model=None):
    """Prefer the cheap direct API when a key is available (metered users:
    ~$0.001), else `claude -p` (subscription users: key-free)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        text, usage = anthropic_score(api_model or HAIKU_API_MODEL, prompt, key)
        if text is not None:
            return text, {**usage, "_path": "api"}
    text, usage = claude_p_score(model, prompt, timeout)
    return text, ({**(usage or {}), "_path": "claude_p"} if text is not None
                  else None)


def usage_cost_usd(usage):
    """Best-effort $ from a usage dict (API tokens, or claude -p's own field)."""
    if not usage:
        return 0.0
    if usage.get("total_cost_usd") is not None:
        return float(usage["total_cost_usd"])
    tin = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    tout = usage.get("output_tokens") or 0
    return tin / 1e6 * HAIKU_IN_PER_M + tout / 1e6 * HAIKU_OUT_PER_M


def parse_scores(text):
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    valid = set(categories())
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict) or it.get("category") not in valid:
            continue
        out.append({"category": it["category"],
                    "severity": float(it.get("severity", 0) or 0),
                    "certainty": float(it.get("certainty", 0) or 0),
                    "note": str(it.get("note", ""))[:200]})
    return out


# ------------------------------------------------------------- decision + note
def format_note(fired):
    body = []
    for f in fired:
        tag = " ↑sonnet" if f.get("escalated") else ""
        body.append(f" ● {f['category']}{tag}\n   {f['note']}")
    return "\n".join([BANNER, *body, BANNER_END])


def update_dynamic(cfg, fired_cats):
    """Raise a category's threshold after a clean streak; reset streak on fire."""
    for c in categories():
        if c in fired_cats:
            cfg["clean_streak"][c] = 0
        else:
            cfg["clean_streak"][c] += 1
            if cfg["clean_streak"][c] >= CLEAN_STREAK_TO_RAISE:
                cfg["thresholds"][c] = round(
                    min(THRESHOLD_CAP, cfg["thresholds"][c] + RAISE_STEP), 3)
                cfg["clean_streak"][c] = 0


def log_event(cfg, project, scores, fired, usage):
    ev = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
          "user": whoami(), "machine": machine(), "project": project,
          "turn": cfg["turn"], "scorer": cfg["scorer_model"],
          "fired": [f["category"] for f in fired],
          "scored": [{"c": s["category"], "sev": round(s["severity"], 2),
                      "cert": round(s["certainty"], 2)} for s in scores],
          "usage": usage or {}}
    for shared in (False, True):
        f = events_file(shared)
        if f is None:
            continue
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a") as fh:
                fh.write(json.dumps(ev) + "\n")
        except OSError:
            pass


PENDING = lambda: COACH_DIR / "pending_note.txt"  # noqa: E731


def run_hook(stdin_data):
    """Non-blocking: surface any ready note from the PREVIOUS turn, then spawn
    a detached background scorer for THIS turn and return immediately (zero
    added latency — the coach runs in parallel with the user reading the
    answer). One-turn-delayed notes."""
    if os.environ.get("AGENT_COACH_ACTIVE"):
        return {}  # recursion guard
    cfg = load_config()
    if not cfg.get("enabled", True):
        return {}
    out = {}
    p = PENDING()
    if p.exists():
        try:
            note = p.read_text()
            p.unlink()
            if note.strip():
                out = {"systemMessage": note}
        except OSError:
            pass
    tp = (stdin_data or {}).get("transcript_path")
    if tp:
        env = dict(os.environ, AGENT_COACH_ACTIVE="1")
        try:  # fire-and-forget background scorer
            subprocess.Popen(
                [sys.executable, str(SKILL_DIR / "agent_coach.py"), "score",
                 "--transcript", tp,
                 "--cwd", (stdin_data or {}).get("cwd") or "."],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError:
            pass
    return out


def do_score(transcript_path, cwd):
    """The actual scoring — runs in the detached background process. Writes a
    pending note (surfaced next turn), logs the event, updates thresholds and
    the daily budget."""
    cfg = load_config()
    cfg["turn"] += 1
    turn = extract_last_turn(transcript_path)
    if turn is None:
        save_config(cfg)
        return
    # daily budget gate
    today = datetime.date.today().isoformat()
    if cfg.get("spend_date") != today:
        cfg["spend_date"], cfg["spend_today"] = today, 0.0
    cap = cfg.get("budget_daily_usd")
    if cap is not None and cfg["spend_today"] >= cap:
        save_config(cfg)
        return  # budget spent for today — stay silent

    user_text, summary, _meta = turn
    text, usage = call_model(cfg["scorer_model"], build_prompt(user_text, summary))
    cfg["spend_today"] = round(cfg.get("spend_today", 0.0) + usage_cost_usd(usage), 5)
    scores = parse_scores(text)

    fired = []
    for s in scores:
        thr = cfg["thresholds"].get(s["category"], START_THRESHOLD)
        if s["severity"] < thr:
            continue
        cut = cfg.get("escalation_cutoff", 0.0)
        if cut > 0 and s["certainty"] < cut:
            et, eu = call_model(cfg["escalation_model"],
                                build_prompt(user_text, summary),
                                api_model="claude-sonnet-5")
            cfg["spend_today"] = round(cfg["spend_today"] + usage_cost_usd(eu), 5)
            for e in parse_scores(et):
                if e["category"] == s["category"] and e["severity"] >= thr:
                    s = {**e, "escalated": True}
                    break
            else:
                continue  # smarter model disagreed -> drop the false positive
        fired.append(s)

    update_dynamic(cfg, {f["category"] for f in fired})
    log_event(cfg, Path(cwd).name, scores, fired, usage)
    save_config(cfg)
    if fired:
        COACH_DIR.mkdir(parents=True, exist_ok=True)
        note = format_note(fired)
        PENDING().write_text(note)
        (COACH_DIR / "last_note.txt").write_text(note)


# ----------------------------------------------------------------- installers
def settings_path():
    return Path.home() / ".claude" / "settings.json"


HOOK_CMD = f"python3 {SKILL_DIR / 'agent_coach.py'} hook"


def install():
    sp = settings_path()
    data = {}
    if sp.exists():
        try:
            data = json.loads(sp.read_text())
        except json.JSONDecodeError:
            print("settings.json is not valid JSON — fix it first"); return 1
    hooks = data.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    flat = json.dumps(stop)
    if "agent_coach.py" in flat:
        print("agent-coach Stop hook already installed"); return 0
    stop.append({"hooks": [{"type": "command", "command": HOOK_CMD}]})
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(data, indent=2))
    print(f"installed Stop hook -> {sp}\nRestart Claude Code to activate.")
    return 0


def uninstall():
    sp = settings_path()
    if not sp.exists():
        print("no settings.json"); return 0
    data = json.loads(sp.read_text())
    stop = data.get("hooks", {}).get("Stop", [])
    kept = [h for h in stop if "agent_coach.py" not in json.dumps(h)]
    data.setdefault("hooks", {})["Stop"] = kept
    sp.write_text(json.dumps(data, indent=2))
    print("agent-coach Stop hook removed")
    return 0


# ---------------------------------------------------------------------- rules
def rules_snapshot():
    ARCHIVE.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = ARCHIVE / f"best_practices_{stamp}.md"
    dest.write_text(RUBRIC.read_text())
    print(f"snapshot -> {dest.name}")


def rules_list():
    ARCHIVE.mkdir(exist_ok=True)
    snaps = sorted(ARCHIVE.glob("best_practices_*.md"))
    print("\n".join(s.name for s in snaps) or "(no snapshots yet)")


def rules_revert(stamp):
    cands = list(ARCHIVE.glob(f"best_practices_*{stamp}*.md"))
    if not cands:
        print(f"no snapshot matching {stamp}"); return 1
    src = sorted(cands)[-1]
    rules_snapshot()  # keep the current one before overwriting
    RUBRIC.write_text(src.read_text())
    print(f"reverted rubric <- {src.name}")
    return 0


# ------------------------------------------------------------------ dashboard
def read_events():
    evs = []
    dirs = [COACH_DIR]
    if os.environ.get("AGENT_COACH_SHARED_DIR"):
        dirs.append(Path(os.environ["AGENT_COACH_SHARED_DIR"]))
    for d in dirs:
        for f in Path(d).glob("coach-*.jsonl") if Path(d).is_dir() else []:
            for line in f.read_text().splitlines():
                try:
                    evs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    # de-dup identical events (same user+machine may appear via local + shared)
    seen, uniq = set(), []
    for e in evs:
        k = (e.get("user"), e.get("machine"), e.get("ts"), e.get("turn"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


def dashboard(out="agent_coach_dashboard.html"):
    from collections import Counter
    evs = read_events()
    turns = len(evs)
    fired = [c for e in evs for c in e.get("fired", [])]
    by_cat = Counter(fired)
    by_user = Counter(e.get("user", "?") for e in evs if e.get("fired"))
    rows = "".join(
        f'<div class="row"><span class="lbl">{c}</span>'
        f'<span class="bar" style="width:{max(6, v*260//max(by_cat.values() or [1]))}px"></span>'
        f'<span class="v">{v}</span></div>' for c, v in by_cat.most_common())
    users = "".join(f"<tr><td>{u}</td><td>{n}</td></tr>"
                    for u, n in by_user.most_common())
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>agent-coach usage</title><style>
body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:720px;margin:0 auto;
padding:22px;color:#1c2733}}h1{{font-size:1.15rem}}
.cards{{display:flex;gap:10px;margin:10px 0}}.card{{background:#fff;border:1px solid #e3e8ee;
border-radius:8px;padding:10px 14px;text-align:center}}.card b{{display:block;font-size:1.3rem}}
.card span{{font-size:.72rem;color:#5b6b7b}}.row{{display:flex;align-items:center;margin:4px 0}}
.lbl{{width:150px;font-size:.82rem;text-align:right;padding-right:8px}}
.bar{{height:16px;background:#2a78d6;border-radius:3px;display:inline-block}}
.v{{font-size:.8rem;color:#5b6b7b;margin-left:6px}}
table{{border-collapse:collapse;font-size:.82rem;margin-top:10px}}td{{border:1px solid #e3e8ee;
padding:3px 9px}}</style></head><body>
<h1>agent-coach usage</h1>
<div class="cards"><div class="card"><b>{turns}</b><span>turns scored</span></div>
<div class="card"><b>{len(fired)}</b><span>coaching notes shown</span></div>
<div class="card"><b>{len(by_user)}</b><span>people coached</span></div></div>
<h3 style="font-size:.95rem">Most-coached habits</h3>{rows or '<i>no data yet</i>'}
<h3 style="font-size:.95rem">By person</h3><table><tr><td><b>user</b></td>
<td><b>notes</b></td></tr>{users}</table></body></html>"""
    Path(out).write_text(html)
    print(f"wrote {out} ({turns} turns, {len(fired)} notes)")


# ---------------------------------------------------------------------- cli
def cmd_status(cfg):
    print(f"agent-coach: {'ON' if cfg['enabled'] else 'OFF'}  "
          f"scorer={cfg['scorer_model']}  escalation_cutoff={cfg['escalation_cutoff']}"
          f"  (>0 sends low-certainty calls to {cfg['escalation_model']})")
    print(f"{'category':18s} {'threshold':>9s} {'clean-streak':>13s}")
    for c in categories():
        print(f"{c:18s} {cfg['thresholds'][c]:>9.2f} {cfg['clean_streak'][c]:>13d}")
    print("lower threshold = more coaching; 1.00 = silent. Auto-raises as you improve.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook")
    p = sub.add_parser("score")  # internal: run by the background scorer
    p.add_argument("--transcript", required=True); p.add_argument("--cwd", default=".")
    p = sub.add_parser("budget"); p.add_argument("daily_usd")  # number or 'off'
    sub.add_parser("install"); sub.add_parser("uninstall")
    sub.add_parser("status")
    p = sub.add_parser("set"); p.add_argument("category"); p.add_argument("value", type=float)
    sub.add_parser("quieter"); sub.add_parser("louder")
    sub.add_parser("off"); sub.add_parser("on")
    p = sub.add_parser("escalate"); p.add_argument("cutoff", type=float)
    sub.add_parser("rules-snapshot"); sub.add_parser("rules-list")
    p = sub.add_parser("rules-revert"); p.add_argument("stamp")
    p = sub.add_parser("dashboard"); p.add_argument("out", nargs="?",
                                                    default="agent_coach_dashboard.html")
    a = ap.parse_args(argv)

    if a.cmd == "hook":
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            data = {}
        out = run_hook(data)
        if out:
            print(json.dumps(out))
        return 0
    if a.cmd == "score":
        do_score(a.transcript, a.cwd)
        return 0
    if a.cmd == "install":
        return install()
    if a.cmd == "uninstall":
        return uninstall()

    cfg = load_config()
    if a.cmd == "status":
        cmd_status(cfg)
    elif a.cmd == "set":
        if a.category not in categories():
            print(f"unknown category. valid: {', '.join(categories())}"); return 1
        cfg["thresholds"][a.category] = max(0.0, min(1.0, a.value)); save_config(cfg)
        print(f"{a.category} threshold -> {cfg['thresholds'][a.category]:.2f}")
    elif a.cmd in ("quieter", "louder"):
        step = 0.1 if a.cmd == "quieter" else -0.1
        for c in categories():
            cfg["thresholds"][c] = round(max(0.0, min(1.0, cfg["thresholds"][c] + step)), 3)
        save_config(cfg); print(f"all thresholds {'raised' if step > 0 else 'lowered'} 0.1")
    elif a.cmd == "off":
        cfg["enabled"] = False; save_config(cfg); print("agent-coach silenced (on to restore)")
    elif a.cmd == "on":
        cfg["enabled"] = True; save_config(cfg); print("agent-coach on")
    elif a.cmd == "escalate":
        cfg["escalation_cutoff"] = max(0.0, min(1.0, a.cutoff)); save_config(cfg)
        print(f"escalation_cutoff -> {cfg['escalation_cutoff']:.2f} "
              f"(low-certainty interventions go to {cfg['escalation_model']}; 0 = never)")
    elif a.cmd == "budget":
        cfg["budget_daily_usd"] = (None if str(a.daily_usd).lower() in ("off", "none")
                                   else max(0.0, float(a.daily_usd)))
        save_config(cfg)
        b = cfg["budget_daily_usd"]
        print(f"daily budget -> {'unlimited' if b is None else f'${b:.2f}'} "
              f"(scoring pauses for the day once hit; spent today "
              f"${cfg.get('spend_today', 0):.4f})")
    elif a.cmd == "rules-snapshot":
        rules_snapshot()
    elif a.cmd == "rules-list":
        rules_list()
    elif a.cmd == "rules-revert":
        return rules_revert(a.stamp)
    elif a.cmd == "dashboard":
        dashboard(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
