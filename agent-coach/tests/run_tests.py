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
import re
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

        # cost math: spend_today must be divided by turns SCORED today, never
        # by the lifetime `turn` counter (which counts ramp-skipped turns and
        # so deflates the quotient by roughly the ramp factor).
        check("scored_today_defaulted", cfg.get("scored_today") == 0,
              f"got {cfg.get('scored_today')!r}")
        cfg["turn"], cfg["scored_today"], cfg["spend_today"] = 100, 4, 0.0920
        honest = cfg["spend_today"] / cfg["scored_today"]
        naive = cfg["spend_today"] / cfg["turn"]
        check("cost_per_scored_turn", abs(honest - 0.023) < 1e-9,
              f"got {honest}")
        check("cost_naive_denominator_rejected", naive < honest / 20,
              f"naive={naive} honest={honest} — the bug must stay reproducible "
              "as a contrast case")

        # upgrade artifact: pre-scored_today config with same-day spend must be
        # flagged unattributable, and the runtime flag must never be persisted
        ac.CONFIG.write_text(json.dumps(
            {"spend_date": "2026-08-07", "spend_today": 0.096, "turn": 61}))
        legacy = ac.load_config()
        check("legacy_cost_day_flagged", legacy["_cost_day_partial"] is True)
        ac.save_config(legacy)
        check("runtime_keys_not_persisted",
              "_cost_day_partial" not in json.loads(ac.CONFIG.read_text()))
        check("legacy_flag_clears_after_save",
              ac.load_config()["_cost_day_partial"] is False)
        ac.CONFIG.unlink()

        # base URL is configurable for orgs that route inference elsewhere
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        check("base_url_default",
              ac.anthropic_base_url() == "https://api.anthropic.com",
              f"got {ac.anthropic_base_url()}")
        os.environ["ANTHROPIC_BASE_URL"] = "https://gw.example.org/anthropic/"
        check("base_url_override_strips_slash",
              ac.anthropic_base_url() == "https://gw.example.org/anthropic",
              f"got {ac.anthropic_base_url()}")
        os.environ.pop("ANTHROPIC_BASE_URL", None)

        # keyless auth: a bearer token alone must select the metered path
        for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(v, None)
        check("credential_none", ac.metered_credential() == (None, None))
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "tok-abc"
        k, t = ac.metered_credential()
        check("credential_token_only", k is None and t == "tok-abc", f"got {(k, t)}")
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

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

        # cost + budget helpers
        check("usage_cost", abs(ac.usage_cost_usd(
            {"input_tokens": 1_000_000, "output_tokens": 0}) - 1.0) < 1e-6)

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

        # do_score (async worker) with a MOCKED model -> writes a pending note
        # + logs an event; run_hook then SURFACES that pending note next turn
        ac.call_model = lambda m, p, timeout=60, api_model=None: (
            '[{"category":"model-selection","severity":0.9,"certainty":0.95,'
            '"note":"Opus for a typo — use Haiku"}]', {"input_tokens": 50})
        cfg = ac.default_config(); ac.save_config(cfg)
        ac.do_score(str(tj), "/proj/demo")
        check("do_score_writes_pending", ac.PENDING().exists()
              and "model-selection" in ac.PENDING().read_text())
        out = ac.run_hook({"transcript_path": str(tj), "cwd": "/proj/demo"})
        check("hook_surfaces_pending", "systemMessage" in out
              and "AGENT COACH" in out["systemMessage"]
              and not ac.PENDING().exists(), f"got {out}")  # note consumed
        check("event_logged", ac.events_file().exists()
              and "model-selection" in ac.events_file().read_text())

        # frequency ramp: turns 1-3 always score; turn 4 announces switch to
        # every-5 (no model call); turn 5 scores; turn 6 skipped -> 4 calls,
        # and a ramp note appears on turn 4
        cfg = ac.default_config(); ac.save_config(cfg)
        mc = {"n": 0}
        ac.call_model = lambda m, p, timeout=60, api_model=None: (
            mc.__setitem__("n", mc["n"] + 1) or ('[]', {}))
        ramp_seen = False
        for i in range(6):
            if ac.PENDING().exists():
                ac.PENDING().unlink()
            ac.do_score(str(tj), "/proj/demo")
            if i == 3 and ac.PENDING().exists() and "every 5" in ac.PENDING().read_text():
                ramp_seen = True
        check("frequency_ramp", mc["n"] == 4 and ramp_seen,
              f"model calls={mc['n']} (expect 4), ramp_seen={ramp_seen}")

        # budget gate: once spend >= cap, do_score stays silent
        cfg = ac.default_config(); cfg["budget_daily_usd"] = 0.001
        cfg["spend_date"] = datetime.date.today().isoformat()
        cfg["spend_today"] = 0.002; ac.save_config(cfg)
        if ac.PENDING().exists():
            ac.PENDING().unlink()
        ac.do_score(str(tj), "/proj/demo")
        check("budget_gate_silences", not ac.PENDING().exists())

        # escalation: low certainty + cutoff>0 -> smarter model consulted; if it
        # disagrees (severity below thr) the finding is dropped (no pending note)
        cfg = ac.default_config(); cfg["escalation_cutoff"] = 0.9
        cfg["thresholds"]["model-selection"] = 0.5; ac.save_config(cfg)
        if ac.PENDING().exists():
            ac.PENDING().unlink()
        calls = {"n": 0}

        def flaky(m, p, timeout=60, api_model=None):
            calls["n"] += 1
            if calls["n"] == 1:  # scorer: high severity, LOW certainty
                return ('[{"category":"model-selection","severity":0.8,'
                        '"certainty":0.2,"note":"maybe"}]', {})
            return ('[]', {})  # escalation model disagrees -> drop
        ac.call_model = flaky
        ac.do_score(str(tj), "/proj/demo")
        check("escalation_drops_false_positive",
              not ac.PENDING().exists() and calls["n"] == 2,
              f"calls={calls}")

        # dashboard renders from the logged events
        dpath = Path(td) / "dash.html"
        ac.dashboard(str(dpath))
        check("dashboard_renders", dpath.exists()
              and "agent-coach usage" in dpath.read_text())

        # ---------------- new-schema telemetry ----------------
        ev_lines = [json.loads(l) for l in
                    ac.events_file().read_text().splitlines() if l.strip()]
        e0 = ev_lines[0]
        check("event_has_date_dow", "date" in e0 and "dow" in e0 and "gap_s" in e0,
              f"keys={sorted(e0)}")
        check("event_no_wallclock_by_default", "time" not in e0)
        check("event_has_project_fields",
              e0.get("project") == "demo" and "d1" in e0 and "tier" in e0,
              f"got project={e0.get('project')}")
        check("event_flags_thrash", "thrash" in e0)

        # precision full -> `time` locally, but NEVER in the shared payload
        shared_dir = Path(td) / "shared"; shared_dir.mkdir()
        os.environ["AGENT_COACH_SHARED_DIR"] = str(shared_dir)
        cfg = ac.default_config(); cfg["precision"] = "full"; ac.save_config(cfg)
        ac.call_model = lambda m, p, timeout=60, api_model=None: (
            '[{"category":"model-selection","severity":0.9,"certainty":0.95,'
            '"note":"x"}]', {"input_tokens": 10})
        ac.do_score(str(tj), "/proj/demo", "sess-A")
        loc = [json.loads(l) for l in ac.events_file().read_text().splitlines()][-1]
        sh = [json.loads(l) for l in ac.events_file(True).read_text().splitlines()][-1]
        check("precision_full_local_only",
              "time" in loc and "time" not in sh,
              f"local_time={'time' in loc} shared_time={'time' in sh}")
        os.environ.pop("AGENT_COACH_SHARED_DIR", None)

        # ---------------- URL never reaches the scoring model ----------------
        # Real guard test: pollute the rubric with a link, assert it is stripped.
        real_rubric = ac.RUBRIC.read_text()
        poll = Path(td) / "polluted.md"
        poll.write_text(real_rubric +
                        "\n11. **link-bait** — see https://evil.example/course "
                        "and www.other.example now.\n")
        ac.RUBRIC = poll
        prompt = ac.build_prompt("do a thing", ["tools used: 1"])
        check("url_stripped_from_prompt",
              "http" not in prompt and "evil.example" not in prompt
              and "www." not in prompt and "[link]" in prompt,
              "a URL survived into the model prompt")
        ac.RUBRIC = Path(real_rubric and str(ac.SKILL_DIR / "best_practices.md"))

        # ---------------- course-pointer gates ----------------
        cmap = ac.load_course_map()
        check("course_map_loads", len(cmap.get("courses") or {}) >= 3)

        cs = Path(td) / "course_state.json"

        def fresh_state():
            if cs.exists():
                cs.unlink()
            return ac.load_course_state()

        st = fresh_state()
        # hit 1 and 2 in DISTINCT sessions -> no pointer; hit 3 -> pointer
        r1 = ac.consider_course({"delegate-search"}, "s1", st, cmap)
        r2 = ac.consider_course({"delegate-search"}, "s2", st, cmap)
        r3 = ac.consider_course({"delegate-search"}, "s3", st, cmap)
        check("course_silent_at_hit_1_2", r1[0] is None and r2[0] is None,
              f"{r1[0]} {r2[0]}")
        check("course_fires_at_hit_3", r3[0] is not None, f"{r3}")

        # three hits in ONE session must NOT trigger (distinct-session rule)
        st = fresh_state()
        for _ in range(3):
            rr = ac.consider_course({"delegate-search"}, "same-session", st, cmap)
        check("course_needs_distinct_sessions", rr[0] is None, f"{rr}")

        # cooldown suppresses a second pointer for a DIFFERENT category
        st = fresh_state()
        for s in ("s1", "s2", "s3"):
            ac.consider_course({"delegate-search"}, s, st, cmap)
        st["last_pointer"] = datetime.date.today().isoformat()
        for s in ("s1", "s2", "s3"):
            rc = ac.consider_course({"use-skills"}, s, st, cmap)
        check("course_cooldown_suppresses", rc[0] is None, f"{rc}")

        # dismiss / done permanently suppress
        st = fresh_state()
        ac._course_rec(st, "skilljar-portal")["dismissed"] = True
        for s in ("s1", "s2", "s3"):
            rd = ac.consider_course({"delegate-search"}, s, st, cmap)
        check("course_dismiss_suppresses", rd[0] is None, f"{rd}")

        # per-course cap: never a third suggestion
        st = fresh_state()
        ac._course_rec(st, "skilljar-portal")["times_suggested"] = ac.COURSE_MAX_SUGGESTS
        for s in ("s1", "s2", "s3"):
            rp = ac.consider_course({"delegate-search"}, s, st, cmap)
        check("course_max_two_suggestions", rp[0] is None, f"{rp}")

        # unmapped categories never produce a pointer, however many hits
        st = fresh_state()
        for cat in ("protect-secrets", "verify-before-done"):
            for s in ("s1", "s2", "s3", "s4"):
                ru = ac.consider_course({cat}, s, st, cmap)
            check(f"course_unmapped_{cat}", ru[0] is None, f"{cat} -> {ru}")

        # project label must come from the REPO ROOT, not the subdir you are in
        # (real bug: sitting in <repo>/tmp/wrapups logged project="wrapups")
        import subprocess as _sp
        repo = Path(td) / "myrepo"
        (repo / "tmp" / "deep").mkdir(parents=True)
        _sp.run(["git", "init", "-q", str(repo)], capture_output=True)
        check("project_label_is_repo_not_subdir",
              ac.repo_root_name(str(repo / "tmp" / "deep")) == "myrepo",
              f'got {ac.repo_root_name(str(repo / "tmp" / "deep"))}')
        check("repo_root_name_falls_back_outside_git",
              ac.repo_root_name(str(Path(td))) == Path(td).name)

        # An unreachable network must NEVER downgrade a verified course.
        # Real incident: TLS inspection on VPN made urllib fail for every URL,
        # and the refresh marked all 7 courses unverified -> catalog emptied.
        import shutil as _sh
        real_map = ac.COURSE_MAP
        tmp_map = Path(td) / "cmap.json"
        tmp_map.write_text(json.dumps({
            "verified_on": "2026-07-30",
            "courses": {"c1": {"title": "T", "provider": "P", "credit": "auto",
                               "url": "https://example.invalid/x",
                               "verified": True, "last_status": 200}},
            "categories": {"delegate-search": ["c1"]}}))
        ac.COURSE_MAP = tmp_map
        _real_http = ac.http_ok
        ac.http_ok = lambda u, timeout=15: (0, "", False)   # unreachable
        ac.courses_refresh()
        after = json.loads(tmp_map.read_text())["courses"]["c1"]
        check("unreachable_keeps_verified", after["verified"] is True,
              f"verified was downgraded to {after['verified']}")
        check("unreachable_records_error",
              after.get("last_check_error") == "unreachable")
        # a real 404 SHOULD unverify
        ac.http_ok = lambda u, timeout=15: (404, "", True)
        ac.courses_refresh()
        after = json.loads(tmp_map.read_text())["courses"]["c1"]
        check("real_404_unverifies", after["verified"] is False
              and after["last_status"] == 404, f"got {after}")
        ac.http_ok = _real_http
        ac.COURSE_MAP = real_map

        # ---------------- learning-intent -> course pointer ----------------
        check("learning_topics_exist", "mcp" in ac.topic_names()
              and "agents" in ac.topic_names(), f"got {ac.topic_names()}")
        check("learning_parses_topic",
              ac.parse_learning_topic("[]\nLEARNING: mcp") == "mcp")
        check("learning_none_is_none",
              ac.parse_learning_topic("[]\nLEARNING: none") is None)
        check("learning_rejects_unknown_topic",
              ac.parse_learning_topic("[]\nLEARNING: quantumbasketweaving") is None)
        st_l = ac.default_course_state()
        cid_l, crs_l = ac.topic_course("mcp", ac.load_course_map(), st_l)
        check("learning_topic_prefers_coursera",
              cid_l == "coursera-mcp-intro"
              and crs_l["credit"] == "auto", f"got {cid_l}")
        # an explicit ask bypasses min_hits/cooldown, but NOT a dismissal
        st_l["last_pointer"] = datetime.date.today().isoformat()  # cooldown active
        cid_c, _ = ac.topic_course("mcp", ac.load_course_map(), st_l)
        check("learning_bypasses_cooldown", cid_c == "coursera-mcp-intro")
        ac._course_rec(st_l, "coursera-mcp-intro")["dismissed"] = True
        cid_d, _ = ac.topic_course("mcp", ac.load_course_map(), st_l)
        check("learning_honours_dismissal", cid_d == "coursera-mcp-advanced",
              f"got {cid_d}")

        # ---------------- A/B variant B extraction ----------------
        abj = Path(td) / "ab.jsonl"
        abj.write_text("\n".join(json.dumps(x) for x in [
            {"type": "user", "message": {"content": "cBoth do a thing"}},
            {"type": "assistant", "message": {"model": "claude-opus-5", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "grep -r x ."}},
                {"type": "text", "text": 'ok\n<coach-self>[{"category":"delegate-search",'
                 '"severity":0.7,"certainty":0.8,"note":"n"}]</coach-self>'}]}}]))
        b = ac.extract_self_scores(str(abj))
        check("ab_extracts_self_block",
              b and len(b) == 1 and b[0]["category"] == "delegate-search", f"got {b}")

        # no block -> None, NOT an empty list (silence and "nothing to flag"
        # are different findings in the comparison)
        noj = Path(td) / "noab.jsonl"
        noj.write_text("\n".join(json.dumps(x) for x in [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "no block here"}]}}]))
        check("ab_absent_block_is_none", ac.extract_self_scores(str(noj)) is None)

        # a stale block from an EARLIER turn must not be picked up
        stalej = Path(td) / "stale.jsonl"
        stalej.write_text("\n".join(json.dumps(x) for x in [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": '<coach-self>[{"category":"use-skills",'
                 '"severity":0.9,"certainty":0.9,"note":"old"}]</coach-self>'}]}},
            {"type": "user", "message": {"content": "next turn"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "no block this time"}]}}]))
        check("ab_ignores_stale_earlier_block",
              ac.extract_self_scores(str(stalej)) is None,
              "picked up a block from a previous turn")

        # bogus categories are filtered exactly like variant A's output
        badj = Path(td) / "bad.jsonl"
        badj.write_text(json.dumps(
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": '<coach-self>[{"category":"made-up",'
                 '"severity":1,"certainty":1,"note":"x"}]</coach-self>'}]}}))
        check("ab_filters_bogus_category", ac.extract_self_scores(str(badj)) == [])

        # the injector stays silent unless armed
        import subprocess as _sp2
        inj = str(ac.SKILL_DIR / "ab_inject.py")
        env = dict(os.environ, AGENT_COACH_DIR=str(td))
        off = _sp2.run(["python3", inj], input=json.dumps({"prompt": "hello"}),
                       capture_output=True, text=True, env=env)
        check("ab_injector_silent_when_off", off.stdout.strip() == "",
              f"emitted {off.stdout[:80]!r}")
        on = _sp2.run(["python3", inj], input=json.dumps({"prompt": "cBoth please"}),
                      capture_output=True, text=True, env=env)
        check("ab_injector_fires_on_cboth",
              "<coach-self>" in on.stdout and "RUBRIC:" in on.stdout)
        check("ab_injector_strips_urls", "http" not in on.stdout,
              "a URL reached the main model's context")

        # dashboards must not land in $HOME or cwd
        rp2 = ac.default_report_path()
        check("coach_report_not_in_home", rp2.parent != Path.home(),
              f"default dashboard path is directly in $HOME: {rp2}")
        check("coach_report_in_state_dir", "reports" in str(rp2))

        # catalog staleness surfaces in `courses status`
        check("catalog_staleness_helper",
              ac._days_since("2000-01-01") > ac.CATALOG_STALE_DAYS
              and ac._days_since(datetime.date.today().isoformat()) == 0)

    os.environ.pop("AGENT_COACH_DIR", None)

    # rubric snapshot + revert roundtrip (in a temp archive)
    orig = ac.RUBRIC.read_text()
    ac.rules_snapshot()
    snaps = sorted(ac.ARCHIVE.glob("best_practices_*.md"))
    check("rules_snapshot", len(snaps) >= 1)
    if snaps:
        snaps[-1].unlink()  # cleanup test artifact

    # install into a temp settings.json — and prove the hook path is
    # VERSION-INDEPENDENT (the silent-death bug: a versioned plugin path in
    # settings.json breaks for every user at the next release)
    with tempfile.TemporaryDirectory() as td2:
        sp = Path(td2) / "settings.json"
        ac.settings_path = lambda: sp
        ac.COACH_DIR = Path(td2) / "state"
        ac.install()
        raw = sp.read_text()
        check("install_adds_hook", sp.exists() and ac.LAUNCHER in raw)
        check("hook_path_version_independent",
              not re.search(r"/\d+\.\d+\.\d+/", raw) and "plugins/cache" not in raw,
              f"versioned path leaked into settings.json: {raw}")
        check("launcher_written", ac.launcher_path().exists())
        ac.uninstall()
        check("uninstall_removes_hook", ac.LAUNCHER not in sp.read_text())

        # doctor runs and reports a nonzero failure count when nothing is wired
        try:
            rc = ac.doctor()
            check("doctor_runs", rc in (0, 1))
        except Exception as e:
            check("doctor_runs", False, repr(e))
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
