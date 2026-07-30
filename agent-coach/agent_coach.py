#!/usr/bin/env python3
"""agent-coach — a pair-programming coach for people driving AI coding agents.

A Stop hook scores each finished turn against best_practices.md using a cheap
model (Haiku) via `claude -p` (so it rides the user's EXISTING Claude Code
auth — no API keys, works for everyone). Per-category thresholds gate whether
a note is shown; thresholds start low (heavy coaching for a beginner) and rise
per-category as the user stops tripping that habit. Optional escalation sends
only low-certainty borderline calls to a smarter model (Sonnet).

When the SAME habit is missed repeatedly across DISTINCT sessions, the coach
can point at a specific training course. Course links never reach the scoring
model — the model decides *whether* a habit was missed, Python decides *what*
link to render, and links only enter the catalog if a real HTTP fetch returned
200 (`courses refresh`).

Zero cloud keys. Cheap (one small Haiku call per substantive turn). Every
coaching decision is logged for a rollup dashboard and the quarterly scrub.

CLI:
  agent_coach.py hook              # (called by the Stop hook; reads stdin JSON)
  agent_coach.py install|uninstall # wire/unwire the Stop hook in settings.json
  agent_coach.py doctor            # health check: is any of this actually working?
  agent_coach.py status            # per-category threshold table
  agent_coach.py set <cat> <0-1>   # set one category's threshold
  agent_coach.py quieter|louder    # nudge ALL thresholds +/-0.1
  agent_coach.py off|on            # silence (all -> 1.0) / restore
  agent_coach.py escalate <0-1>    # escalation certainty cutoff (0 = never)
  agent_coach.py precision date|full   # wall-clock time in the LOCAL log only
  agent_coach.py project <code|poc|skip|show>   # tag this repo (asked once)
  agent_coach.py ask-after <n>     # turns in a repo before it asks for a code
  agent_coach.py courses <status|on|off|done|dismiss|snooze|preview|refresh|watch|share|min-hits|cooldown>
  agent_coach.py rules-snapshot|rules-list|rules-revert <date>
  agent_coach.py dashboard [out.html]
"""
import argparse
import datetime
import getpass
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
RUBRIC = SKILL_DIR / "best_practices.md"
COURSE_MAP = SKILL_DIR / "course_map.json"
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
ASK_AFTER_DEFAULT = 25      # scored turns in a repo before asking for a D1 code
COURSE_MIN_HITS = 3         # distinct sessions missing a habit before a course
COURSE_COOLDOWN_DAYS = 7    # at most one course pointer per week, all categories
COURSE_MAX_SUGGESTS = 2     # never suggest the same course more than twice
CATALOG_STALE_DAYS = 180
WATCH_COOLDOWN_DAYS = 30    # "new modules published" news, for opted-in superusers

# Any link-shaped text is stripped from rule text before it reaches the scoring
# model. A small model asked to echo a URL will eventually invent one, and a
# hallucinated course link shipped to staff is worse than no link at all.
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)


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


def projects_file():
    return COACH_DIR / "projects.json"


def course_state_file():
    return COACH_DIR / "course_state.json"


# --------------------------------------------------------------------- rubric
def load_rules():
    """[(category, text)] parsed from best_practices.md."""
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
            "budget_daily_usd": None,   # None = unlimited (opt-in cap only)
            "spend_date": "", "spend_today": 0.0,
            "score_frequency": 1,       # 1 = every turn; N = every Nth
            "ramp_after": 3, "ramp_to": 5, "frequency_ramped": False,
            "turn": 0,
            "precision": "date",        # "full" adds wall-clock time LOCALLY only
            "ask_after": ASK_AFTER_DEFAULT,
            "last_event_epoch": None,   # for gap_s (burst analysis)
            "repo_turns": {},           # repo key -> scored turns seen
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
    cfg.setdefault("repo_turns", {})
    cfg.setdefault("precision", "date")
    cfg.setdefault("ask_after", ASK_AFTER_DEFAULT)
    cfg.setdefault("last_event_epoch", None)
    return cfg


def save_config(cfg):
    COACH_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1))


def _read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=1))


# -------------------------------------------------------------- project tags
def repo_key(cwd):
    """Stable identity for a working directory. The git remote URL is identical
    on every clone on every machine, so one person registering a repo registers
    it for every colleague; it also survives renaming the folder. Falls back to
    the absolute path for scratch dirs with no remote."""
    try:
        p = subprocess.run(["git", "-C", str(cwd), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        url = p.stdout.strip()
        if p.returncode == 0 and url:
            return re.sub(r"\.git$", "", url.strip().lower())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "path:" + str(Path(cwd).resolve())


def load_projects():
    return _read_json(projects_file(), {})


def project_info(cwd):
    """{project, d1, tier, asked} for this repo. `project` is always present
    with zero effort; d1/tier are the optional one-time enrichment."""
    projs = load_projects()
    key = repo_key(cwd)
    rec = projs.get(key) or {}
    return {"key": key,
            "project": rec.get("project") or Path(cwd).name,
            "d1": rec.get("d1"),
            "tier": rec.get("tier", "unset"),
            "asked": bool(rec.get("asked"))}


def set_project(cwd, value):
    projs = load_projects()
    key = repo_key(cwd)
    rec = projs.get(key) or {"project": Path(cwd).name}
    rec["project"] = rec.get("project") or Path(cwd).name
    if value == "poc":
        rec.update({"d1": None, "tier": "poc", "asked": True})
    elif value == "skip":
        rec.update({"asked": False, "tier": rec.get("tier", "unset")})
    else:
        rec.update({"d1": value, "tier": "project", "asked": True})
    projs[key] = rec
    _write_json(projects_file(), projs)
    return rec


def project_ask_note(info):
    return "\n".join([
        BANNER,
        f" ● project tag — {info['project']}",
        "   This repo has been busy. Tag it once and I'll never ask again:",
        "     agent_coach.py project D1-XXXX   (tracked in D1)",
        "     agent_coach.py project poc       (no D1 code — never ask again)",
        "     agent_coach.py project skip      (ask me later)",
        BANNER_END])


# ------------------------------------------------------------ transcript read
def session_id_from(transcript_path):
    """Each session writes its own transcript file, so the filename IS the
    session id. Needed for 'N hits across DISTINCT sessions'."""
    try:
        return Path(transcript_path).stem
    except Exception:
        return "unknown"


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
    """Rule text is URL-stripped on the way to the model — see URL_RE."""
    rules = "\n".join(f"- {c}: {URL_RE.sub('[link]', t)}" for c, t in load_rules())
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


# ------------------------------------------------------------------- courses
def default_course_state():
    return {"enabled": True, "share": False, "watch": False,
            "min_hits": COURSE_MIN_HITS, "cooldown_days": COURSE_COOLDOWN_DAYS,
            "snooze_until": None, "last_pointer": None, "last_watch": None,
            "hits": {},        # category -> [session ids]
            "courses": {},     # course id -> {times_suggested,last,dismissed,completed}
            "seen_catalog": []}


def load_course_state():
    st = default_course_state()
    st.update(_read_json(course_state_file(), {}))
    st.setdefault("hits", {})
    st.setdefault("courses", {})
    return st


def save_course_state(st):
    _write_json(course_state_file(), st)


def load_course_map():
    return _read_json(COURSE_MAP, {"verified_on": None, "courses": {},
                                   "categories": {}})


def _course_rec(st, cid):
    return st["courses"].setdefault(
        cid, {"times_suggested": 0, "last": None, "dismissed": False,
              "completed": False})


def _days_since(iso):
    if not iso:
        return 10 ** 6
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
    except ValueError:
        return 10 ** 6
    return (datetime.date.today() - d).days


def course_candidate(category, cmap, st):
    """First mapped course for this category that is still offerable."""
    for cid in (cmap.get("categories") or {}).get(category, []):
        c = (cmap.get("courses") or {}).get(cid)
        if not c or not c.get("url"):
            continue
        rec = _course_rec(st, cid)
        if rec["dismissed"] or rec["completed"]:
            continue
        if rec["times_suggested"] >= COURSE_MAX_SUGGESTS:
            continue
        return cid, c
    return None, None


def consider_course(fired_cats, session, st, cmap):
    """Second gating layer on top of severity. Returns (cid, course) or
    (None, None). Records distinct-session hits as a side effect."""
    for cat in fired_cats:
        seen = st["hits"].setdefault(cat, [])
        if session not in seen:
            seen.append(session)
    if not st.get("enabled", True):
        return None, None
    if _days_since(st.get("snooze_until")) < 0 or (
            st.get("snooze_until") and _days_since(st["snooze_until"]) <= 0):
        return None, None
    if _days_since(st.get("last_pointer")) < st.get("cooldown_days",
                                                    COURSE_COOLDOWN_DAYS):
        return None, None
    min_hits = st.get("min_hits", COURSE_MIN_HITS)
    for cat in sorted(fired_cats):
        if len(st["hits"].get(cat, [])) < min_hits:
            continue
        cid, c = course_candidate(cat, cmap, st)
        if cid:
            return cid, {**c, "category": cat, "id": cid}
    return None, None


def format_course_note(course):
    credit = ("counts automatically toward your Duke training record"
              if course.get("credit") == "auto"
              else "certificate is manual — log it yourself")
    return "\n".join([
        f" ● {course['category']} — worth some training time",
        f"   You've hit this in {COURSE_MIN_HITS}+ separate sessions.",
        f"   {course.get('title', course['id'])} ({course.get('provider', '?')})",
        f"   {course.get('url', '')}",
        f"   Credit: {credit}",
        "   Not interested?  agent_coach.py courses dismiss "
        f"{course['id']}   |   Already done?  courses done {course['id']}"])


def format_watch_note(new_ids, cmap):
    lines = [" ● new training published"]
    for cid in new_ids[:4]:
        c = (cmap.get("courses") or {}).get(cid, {})
        lines.append(f"   {c.get('title', cid)} — {c.get('url', '')}")
    lines.append("   Turn this off:  agent_coach.py courses watch off")
    return "\n".join(lines)


# ------------------------------------------------------------- decision + note
def format_note(fired, extra=None):
    body = []
    for f in fired:
        tag = " ↑sonnet" if f.get("escalated") else ""
        body.append(f" ● {f['category']}{tag}\n   {f['note']}")
    if extra:
        body.append(extra)
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


def build_event(cfg, cwd, scores, fired, usage, session, proj):
    """The telemetry record.

    Shared by default: date, gap_s, dow, user, machine, project/d1/tier,
    habits, cost. That's enough for burst analysis and per-project token
    variance without a wall clock.
    Local only: `time` (second-resolution), and only when precision == full.
    """
    now = datetime.datetime.now().astimezone()
    gap = None
    if cfg.get("last_event_epoch"):
        gap = max(0, int(now.timestamp() - float(cfg["last_event_epoch"])))
    cfg["last_event_epoch"] = now.timestamp()
    fired_cats = [f["category"] for f in fired]
    ev = {"date": now.date().isoformat(),
          "gap_s": gap,
          "dow": now.weekday(),                 # 0 = Monday
          "user": whoami(), "machine": machine(),
          "session": session,
          "project": proj["project"],           # Option A: folder name ships
          "d1": proj["d1"], "tier": proj["tier"],
          "repo": proj["key"],
          "turn": cfg["turn"], "scorer": cfg["scorer_model"],
          "fired": fired_cats,
          "thrash": "avoid-thrash" in fired_cats,
          "scored": [{"c": s["category"], "sev": round(s["severity"], 2),
                      "cert": round(s["certainty"], 2)} for s in scores],
          "usage": usage or {}}
    if cfg.get("precision") == "full":
        ev["time"] = now.strftime("%H:%M:%S")   # stripped from the shared copy
    return ev


def log_event(ev, course_event=None, share_courses=False):
    """Local gets everything. Shared gets everything EXCEPT wall-clock time,
    and except course events unless explicitly opted in."""
    shared_ev = {k: v for k, v in ev.items() if k != "time"}
    for shared in (False, True):
        f = events_file(shared)
        if f is None:
            continue
        payload = shared_ev if shared else ev
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a") as fh:
                fh.write(json.dumps(payload) + "\n")
        except OSError:
            pass
    if not course_event:
        return
    # course pointers live in their own stream, local-first
    for shared in (False, True):
        if shared and not share_courses:
            continue
        f = events_file(shared)
        if f is None:
            continue
        cf = f.parent / f.name.replace("coach-", "courses-")
        try:
            cf.parent.mkdir(parents=True, exist_ok=True)
            with open(cf, "a") as fh:
                fh.write(json.dumps(course_event) + "\n")
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
                 "--cwd", (stdin_data or {}).get("cwd") or ".",
                 "--session", (stdin_data or {}).get("session_id")
                 or session_id_from(tp)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError:
            pass
    return out


def do_score(transcript_path, cwd, session=None):
    """The actual scoring — runs in the detached background process. Writes a
    pending note (surfaced next turn), logs the event, updates thresholds and
    the daily budget."""
    session = session or session_id_from(transcript_path)
    cfg = load_config()
    cfg["turn"] += 1
    turn_no = cfg["turn"]

    # frequency ramp: score every turn for the first `ramp_after` turns (so a
    # quick user who only does a few turns gets full coverage), then switch to
    # every `ramp_to`-th turn to save cost — announced once, revertible with
    # `agent_coach.py frequency 1`.
    ramp_note = None
    if turn_no > cfg.get("ramp_after", 3) and not cfg.get("frequency_ramped"):
        cfg["frequency_ramped"] = True
        cfg["score_frequency"] = cfg.get("ramp_to", 5)
        ramp_note = (BANNER + "\n coaching now runs every "
                     f"{cfg['score_frequency']} turns to save cost.\n"
                     " keep every turn:  agent_coach.py frequency 1\n" + BANNER_END)
    freq = cfg.get("score_frequency", 1)
    should_score = (turn_no <= cfg.get("ramp_after", 3) or freq <= 1
                    or turn_no % freq == 0)
    if not should_score:
        if ramp_note:  # still announce the switch on the ramp turn
            COACH_DIR.mkdir(parents=True, exist_ok=True)
            PENDING().write_text(ramp_note)
        save_config(cfg)
        return

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

    fired_cats = {f["category"] for f in fired}
    update_dynamic(cfg, fired_cats)

    # ---- project tagging: ask once, only for repos that got busy
    proj = project_info(cwd)
    rt = cfg["repo_turns"]
    rt[proj["key"]] = rt.get(proj["key"], 0) + 1
    ask_note = None
    if (not proj["asked"] and proj["tier"] == "unset"
            and rt[proj["key"]] >= cfg.get("ask_after", ASK_AFTER_DEFAULT)):
        ask_note = project_ask_note(proj)

    # ---- course pointers: second gating layer, own state file
    st = load_course_state()
    cmap = load_course_map()
    course_extra, course_event = None, None
    cid, course = consider_course(fired_cats, session, st, cmap)
    if cid:
        rec = _course_rec(st, cid)
        rec["times_suggested"] += 1
        rec["last"] = datetime.date.today().isoformat()
        st["last_pointer"] = rec["last"]
        course_extra = format_course_note(course)
        course_event = {"date": rec["last"], "user": whoami(),
                        "project": proj["project"], "category": course["category"],
                        "course_id": cid, "event": "suggested"}
    # ---- new-module news: opt-in, separate cadence, never a rebuke
    watch_extra = None
    if st.get("watch") and _days_since(st.get("last_watch")) >= WATCH_COOLDOWN_DAYS:
        known = set(st.get("seen_catalog") or [])
        allc = set((cmap.get("courses") or {}).keys())
        new = sorted(allc - known)
        if known and new:
            watch_extra = format_watch_note(new, cmap)
        st["seen_catalog"] = sorted(allc)
        if new or not known:
            st["last_watch"] = datetime.date.today().isoformat()
    save_course_state(st)

    ev = build_event(cfg, cwd, scores, fired, usage, session, proj)
    log_event(ev, course_event, st.get("share", False))
    save_config(cfg)

    extras = "\n".join(x for x in (course_extra, watch_extra) if x)
    if fired or extras:
        note = format_note(fired, extras or None)
    else:
        note = ""
    for tail in (ask_note, ramp_note):
        if tail:
            note = (note + "\n" + tail) if note else tail
    if note:
        COACH_DIR.mkdir(parents=True, exist_ok=True)
        PENDING().write_text(note)
        (COACH_DIR / "last_note.txt").write_text(note)


# ----------------------------------------------------------------- installers
def settings_path():
    return Path.home() / ".claude" / "settings.json"


LAUNCHER = "coach_hook.py"
LAUNCHER_SRC = '''#!/usr/bin/env python3
"""agent-coach hook launcher — version-independent.

settings.json points here, NOT at a versioned plugin path. Plugin installs live
under .../research-skills/<version>/agent-coach/, so baking that path into
settings.json means the hook silently dies at the next version bump. This stub
never moves; it finds whichever copy of the skill currently exists.
"""
import runpy, sys, glob, os
from pathlib import Path

CANDIDATES = []
CANDIDATES += sorted(glob.glob(str(Path.home() /
    ".claude/plugins/cache/*/*/*/agent-coach/agent_coach.py")), reverse=True)
CANDIDATES += [str(Path.home() / ".claude/skills/agent-coach/agent_coach.py")]
CANDIDATES += sorted(glob.glob(str(Path.home() /
    ".claude/plugins/marketplaces/*/agent-coach/agent_coach.py")))

for c in CANDIDATES:
    if os.path.exists(c):
        sys.argv = [c] + sys.argv[1:]
        runpy.run_path(c, run_name="__main__")
        break
else:
    sys.exit(0)  # skill uninstalled — stay silent, never break the user's turn
'''


def launcher_path():
    return COACH_DIR / LAUNCHER


def write_launcher():
    COACH_DIR.mkdir(parents=True, exist_ok=True)
    p = launcher_path()
    p.write_text(LAUNCHER_SRC)
    try:
        p.chmod(0o755)
    except OSError:
        pass
    return p


def hook_cmd():
    return f"python3 {launcher_path()} hook"


def install():
    sp = settings_path()
    data = {}
    if sp.exists():
        try:
            data = json.loads(sp.read_text())
        except json.JSONDecodeError:
            print("settings.json is not valid JSON — fix it first"); return 1
    write_launcher()
    hooks = data.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    flat = json.dumps(stop)
    if LAUNCHER in flat or "agent_coach.py" in flat:
        print("agent-coach Stop hook already installed"); return 0
    stop.append({"hooks": [{"type": "command", "command": hook_cmd()}]})
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(data, indent=2))
    print(f"installed Stop hook -> {sp}")
    print(f"launcher (version-independent) -> {launcher_path()}")
    print("Restart Claude Code to activate.")
    return 0


def uninstall():
    sp = settings_path()
    if not sp.exists():
        print("no settings.json"); return 0
    data = json.loads(sp.read_text())
    stop = data.get("hooks", {}).get("Stop", [])
    kept = [h for h in stop
            if LAUNCHER not in json.dumps(h) and "agent_coach.py" not in json.dumps(h)]
    data.setdefault("hooks", {})["Stop"] = kept
    sp.write_text(json.dumps(data, indent=2))
    print("agent-coach Stop hook removed")
    return 0


# -------------------------------------------------------------------- doctor
def doctor():
    """Install and re-check are the same operation. Every entry has a check and
    a one-line fix, so this works as first-run setup AND as an ongoing health
    check — which is what catches a hook that silently stopped firing."""
    checks = []

    def add(name, ok, detail, fix=""):
        checks.append((name, ok, detail, fix))

    sp = settings_path()
    raw = sp.read_text() if sp.exists() else ""
    wired = LAUNCHER in raw or "agent_coach.py" in raw
    add("Stop hook wired", wired,
        str(sp) if wired else "no agent-coach entry in settings.json",
        "agent_coach.py install")

    lp = launcher_path()
    add("launcher exists", lp.exists(), str(lp), "agent_coach.py install")

    # the failure this whole command exists for: a stale versioned path
    stale = bool(re.search(r"agent-coach/agent_coach\.py", raw)) and LAUNCHER not in raw
    if stale:
        m = re.search(r'"command":\s*"python3 ([^"]+) hook"', raw)
        tgt = m.group(1) if m else "?"
        add("hook target exists", Path(tgt).exists() if m else False,
            f"legacy versioned path: {tgt}",
            "agent_coach.py uninstall && agent_coach.py install")

    add("state dir", COACH_DIR.exists(), str(COACH_DIR), "runs on first score")
    cats = categories()
    add("rubric parses", len(cats) >= 8, f"{len(cats)} categories", "check best_practices.md")

    cmap = load_course_map()
    n_c = len(cmap.get("courses") or {})
    age = _days_since(cmap.get("verified_on"))
    add("course catalog", n_c > 0,
        f"{n_c} courses, verified {age if age < 10**5 else 'never'} days ago",
        "agent_coach.py courses refresh")
    if n_c and age > CATALOG_STALE_DAYS:
        add("catalog freshness", False, f"stale ({age}d > {CATALOG_STALE_DAYS}d)",
            "agent_coach.py courses refresh")

    ef = events_file()
    n_ev = len(ef.read_text().splitlines()) if ef and ef.exists() else 0
    add("scored turns logged", n_ev > 0, f"{n_ev} events",
        "work normally for a few turns")

    shared = os.environ.get("AGENT_COACH_SHARED_DIR")
    add("org rollup", bool(shared), shared or "not configured (local only)",
        "export AGENT_COACH_SHARED_DIR=...")

    width = max(len(c[0]) for c in checks)
    bad = 0
    for name, ok, detail, fix in checks:
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {name:{width}s}  {detail}")
        if not ok:
            bad += 1
            if fix:
                print(f"         └─ fix: {fix}")
    print(f"\n{len(checks) - bad}/{len(checks)} checks pass")
    return 1 if bad else 0


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


# ---------------------------------------------------------- catalog refresh
def http_ok(url, timeout=12):
    """(status, title) — a course only enters the catalog if this returns 200.
    No model is ever asked to produce a link, so a hallucinated URL cannot
    reach staff."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": "agent-coach/1.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(200_000).decode("utf-8", "replace")
            m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
            title = " ".join(m.group(1).split())[:120] if m else ""
            return r.status, title
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, type(e).__name__


def courses_refresh():
    cmap = load_course_map()
    courses = cmap.get("courses") or {}
    if not courses:
        print("course_map.json has no courses"); return 1
    print(f"verifying {len(courses)} course URLs (200 = keep, anything else = flagged)\n")
    ok = 0
    for cid, c in sorted(courses.items()):
        st, title = http_ok(c["url"])
        c["last_status"] = st
        c["verified"] = (st == 200)
        if title and st == 200:
            c["fetched_title"] = title
        ok += st == 200
        print(f"  [{st or 'ERR':>3}] {cid:34s} {c['url']}")
        if title and st == 200:
            print(f"        title: {title}")
    cmap["verified_on"] = datetime.date.today().isoformat()
    cmap["courses"] = courses
    _write_json(COURSE_MAP, cmap)
    print(f"\n{ok}/{len(courses)} verified 200 · verified_on={cmap['verified_on']}")
    print("Entries not returning 200 stay in the file but are never suggested.")
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
        k = (e.get("user"), e.get("machine"),
             e.get("date") or e.get("ts"), e.get("turn"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


def dashboard(out="agent_coach_dashboard.html"):
    from collections import Counter, defaultdict
    evs = read_events()
    turns = len(evs)
    fired = [c for e in evs for c in e.get("fired", [])]
    by_cat = Counter(fired)
    by_user = Counter(e.get("user", "?") for e in evs if e.get("fired"))
    thrash = sum(1 for e in evs if e.get("thrash"))
    effort = turns - thrash            # item 8: thrash is not effort

    # per-project token spend — the 20x-variance question
    ptok = defaultdict(int)
    pturn = Counter()
    for e in evs:
        u = e.get("usage") or {}
        ptok[e.get("project", "?")] += (u.get("input_tokens") or 0) + \
                                       (u.get("output_tokens") or 0)
        pturn[e.get("project", "?")] += 1
    # burst structure from gaps alone — no wall clock needed
    gaps = [e["gap_s"] for e in evs if isinstance(e.get("gap_s"), int)]
    gaps_sorted = sorted(gaps)
    med = gaps_sorted[len(gaps_sorted) // 2] if gaps_sorted else 0
    bursty = sum(1 for g in gaps if g < 120)

    rows = "".join(
        f'<div class="row"><span class="lbl">{c}</span>'
        f'<span class="bar" style="width:{max(6, v*260//max(by_cat.values() or [1]))}px"></span>'
        f'<span class="v">{v}</span></div>' for c, v in by_cat.most_common())
    users = "".join(f"<tr><td>{u}</td><td>{n}</td></tr>"
                    for u, n in by_user.most_common())
    projs = "".join(
        f"<tr><td>{p}</td><td>{pturn[p]}</td><td>{t:,}</td>"
        f"<td>{t // max(1, pturn[p]):,}</td></tr>"
        for p, t in sorted(ptok.items(), key=lambda kv: -kv[1])[:15])
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>agent-coach usage</title><style>
body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:760px;margin:0 auto;
padding:22px;color:#1c2733}}h1{{font-size:1.15rem}}
.cards{{display:flex;gap:10px;margin:10px 0;flex-wrap:wrap}}.card{{background:#fff;border:1px solid #e3e8ee;
border-radius:8px;padding:10px 14px;text-align:center}}.card b{{display:block;font-size:1.3rem}}
.card span{{font-size:.72rem;color:#5b6b7b}}.row{{display:flex;align-items:center;margin:4px 0}}
.lbl{{width:150px;font-size:.82rem;text-align:right;padding-right:8px}}
.bar{{height:16px;background:#2a78d6;border-radius:3px;display:inline-block}}
.v{{font-size:.8rem;color:#5b6b7b;margin-left:6px}}
table{{border-collapse:collapse;font-size:.82rem;margin-top:10px}}td,th{{border:1px solid #e3e8ee;
padding:3px 9px;text-align:left}}
.note{{font-size:.74rem;color:#5b6b7b;margin-top:6px}}</style></head><body>
<h1>agent-coach usage</h1>
<div class="cards"><div class="card"><b>{turns}</b><span>turns scored</span></div>
<div class="card"><b>{len(fired)}</b><span>coaching notes shown</span></div>
<div class="card"><b>{len(by_user)}</b><span>people coached</span></div>
<div class="card"><b>{effort}</b><span>effort turns<br>(thrash removed)</span></div>
<div class="card"><b>{med}s</b><span>median gap<br>between turns</span></div>
<div class="card"><b>{bursty}</b><span>turns inside<br>a burst (&lt;2min)</span></div></div>
<h3 style="font-size:.95rem">Most-coached habits</h3>{rows or '<i>no data yet</i>'}
<h3 style="font-size:.95rem">Token spend by project</h3>
<table><tr><th>project</th><th>turns</th><th>tokens</th><th>tokens/turn</th></tr>
{projs or '<tr><td colspan=4><i>no data yet</i></td></tr>'}</table>
<p class="note">Tokens measure activity, not value — {thrash} thrash-flagged turns are
excluded from "effort turns" above. Pair this with merged PRs or days-to-done
before drawing any conclusion about productivity.</p>
<h3 style="font-size:.95rem">By person</h3><table><tr><th>user</th>
<th>notes</th></tr>{users}</table></body></html>"""
    Path(out).write_text(html)
    print(f"wrote {out} ({turns} turns, {len(fired)} notes)")


# ---------------------------------------------------------------------- cli
def cmd_status(cfg):
    print(f"agent-coach: {'ON' if cfg['enabled'] else 'OFF'}  "
          f"scorer={cfg['scorer_model']}  escalation_cutoff={cfg['escalation_cutoff']}"
          f"  (>0 sends low-certainty calls to {cfg['escalation_model']})")
    print(f"precision={cfg.get('precision', 'date')} "
          f"(full = wall-clock time in the LOCAL log only, never shared)")
    print(f"{'category':18s} {'threshold':>9s} {'clean-streak':>13s}")
    for c in categories():
        print(f"{c:18s} {cfg['thresholds'][c]:>9.2f} {cfg['clean_streak'][c]:>13d}")
    print("lower threshold = more coaching; 1.00 = silent. Auto-raises as you improve.")


def cmd_courses(a):
    st = load_course_state()
    cmap = load_course_map()
    sub, arg = a.sub, a.arg
    if sub == "status":
        age = _days_since(cmap.get("verified_on"))
        print(f"course pointers: {'ON' if st['enabled'] else 'OFF'}   "
              f"share-to-org: {'ON' if st['share'] else 'OFF (local only)'}   "
              f"watch-new: {'ON' if st['watch'] else 'OFF'}")
        print(f"gates: {st['min_hits']} distinct sessions | "
              f"{st['cooldown_days']}d global cooldown | "
              f"{COURSE_MAX_SUGGESTS} suggestions max per course")
        print(f"catalog: {len(cmap.get('courses') or {})} courses, verified_on="
              f"{cmap.get('verified_on')}"
              + ("  ** STALE — run `courses refresh` **"
                 if age > CATALOG_STALE_DAYS else ""))
        print(f"\n{'category':18s} {'sessions':>8s}  {'course':34s} {'sug':>3s}  state")
        for c in categories():
            hits = len(st["hits"].get(c, []))
            cid, course = course_candidate(c, cmap, st)
            mapped = (cmap.get("categories") or {}).get(c) or []
            if not mapped:
                print(f"{c:18s} {hits:>8d}  {'(no course — by design)':34s}")
                continue
            for m in mapped:
                rec = _read_json(course_state_file(), {}).get("courses", {}).get(m, {})
                state = ("dismissed" if rec.get("dismissed") else
                         "completed" if rec.get("completed") else
                         "spent" if rec.get("times_suggested", 0) >= COURSE_MAX_SUGGESTS
                         else "available")
                print(f"{c:18s} {hits:>8d}  {m:34s} "
                      f"{rec.get('times_suggested', 0):>3d}  {state}")
        return 0
    if sub in ("on", "off"):
        st["enabled"] = sub == "on"
    elif sub == "share":
        st["share"] = str(arg).lower() in ("on", "true", "yes")
        print("course events " + ("WILL" if st["share"] else "will NOT")
              + " be written to the org shared dir")
    elif sub == "watch":
        st["watch"] = str(arg).lower() in ("on", "true", "yes")
    elif sub in ("done", "dismiss"):
        if not arg:
            print("need a course id (see `courses status`)"); return 1
        ids = [arg] if arg in (cmap.get("courses") or {}) else \
              (cmap.get("categories") or {}).get(arg, [])
        if not ids:
            print(f"unknown course or category: {arg}"); return 1
        for i in ids:
            rec = _course_rec(st, i)
            rec["completed" if sub == "done" else "dismissed"] = True
        print(f"{sub}: {', '.join(ids)} — will never be suggested again")
    elif sub == "snooze":
        d = int(arg or 30)
        st["snooze_until"] = (datetime.date.today()
                              + datetime.timedelta(days=d)).isoformat()
        print(f"course pointers snoozed until {st['snooze_until']}")
    elif sub == "min-hits":
        st["min_hits"] = max(1, int(arg or COURSE_MIN_HITS))
    elif sub == "cooldown":
        st["cooldown_days"] = max(0, int(arg or COURSE_COOLDOWN_DAYS))
    elif sub == "preview":
        cat = arg or "delegate-search"
        cid, c = course_candidate(cat, cmap, st)
        if not cid:
            print(f"no course mapped to '{cat}' (or all spent/dismissed)"); return 1
        print(format_note([], format_course_note({**c, "category": cat, "id": cid})))
        print("\n(preview only — no state changed)")
        return 0
    elif sub == "refresh":
        return courses_refresh()
    else:
        print(f"unknown: {sub}"); return 1
    save_course_state(st)
    print("ok")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook")
    p = sub.add_parser("score")  # internal: run by the background scorer
    p.add_argument("--transcript", required=True)
    p.add_argument("--cwd", default=".")
    p.add_argument("--session", default=None)
    p = sub.add_parser("budget"); p.add_argument("daily_usd")  # number or 'off'
    p = sub.add_parser("frequency"); p.add_argument("n", type=int)  # 1=every turn
    sub.add_parser("install"); sub.add_parser("uninstall"); sub.add_parser("doctor")
    sub.add_parser("status")
    p = sub.add_parser("set"); p.add_argument("category"); p.add_argument("value", type=float)
    sub.add_parser("quieter"); sub.add_parser("louder")
    sub.add_parser("off"); sub.add_parser("on")
    p = sub.add_parser("escalate"); p.add_argument("cutoff", type=float)
    p = sub.add_parser("precision"); p.add_argument("mode", choices=["date", "full"])
    p = sub.add_parser("project"); p.add_argument("value")  # code | poc | skip | show
    p = sub.add_parser("ask-after"); p.add_argument("n", type=int)
    p = sub.add_parser("courses")
    p.add_argument("sub", default="status", nargs="?")
    p.add_argument("arg", nargs="?")
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
        do_score(a.transcript, a.cwd, a.session)
        return 0
    if a.cmd == "install":
        return install()
    if a.cmd == "uninstall":
        return uninstall()
    if a.cmd == "doctor":
        return doctor()
    if a.cmd == "courses":
        return cmd_courses(a)

    cfg = load_config()
    if a.cmd == "status":
        cmd_status(cfg)
    elif a.cmd == "project":
        cwd = os.getcwd()
        if a.value == "show":
            print(json.dumps(project_info(cwd), indent=1)); return 0
        rec = set_project(cwd, a.value)
        print(f"{repo_key(cwd)}\n  project={rec['project']} d1={rec.get('d1')} "
              f"tier={rec.get('tier')}")
        return 0
    elif a.cmd == "ask-after":
        cfg["ask_after"] = max(1, a.n); save_config(cfg)
        print(f"will ask for a project code after {cfg['ask_after']} scored turns in a repo")
    elif a.cmd == "precision":
        cfg["precision"] = a.mode; save_config(cfg)
        print(f"precision -> {a.mode}"
              + ("  (wall-clock time recorded LOCALLY only — never written to "
                 "the shared dir)" if a.mode == "full" else "  (date only)"))
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
        print(f"daily budget -> {'unlimited (off)' if b is None else f'${b:.2f}'} "
              f"(optional; scoring pauses for the day once hit; spent today "
              f"${cfg.get('spend_today', 0):.4f})")
    elif a.cmd == "frequency":
        cfg["score_frequency"] = max(1, a.n)
        cfg["frequency_ramped"] = True  # explicit choice — don't auto-ramp again
        save_config(cfg)
        print(f"scoring frequency -> every {cfg['score_frequency']} turn(s) "
              f"({'every turn' if cfg['score_frequency'] == 1 else 'cost-saving'})")
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
