#!/usr/bin/env python3
"""Regression tests + telemetry for the llm-usage-ledger global skill.

Fully deterministic — no LLM server or network required. Exercises the
ledger write/read/aggregate path (the part other repos and the org depend
on), the price series, ingest, and the HTML report, all with a temp ledger
dir, plus CLI integrity.

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
LEDGER = SKILL_DIR.parent / "telemetry" / "llm-usage-ledger.jsonl"
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
        import llm_usage_ledger as lp
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

        # 2b. provider param recorded (default stays lmstudio for back-compat)
        ev_p = lp.log_usage("test-model", {"prompt_tokens": 1,
                                           "completion_tokens": 1},
                            0.1, provider="azure")
        check("provider_param", ev["provider"] == "lmstudio"
              and ev_p["provider"] == "azure"
              and not lp.is_local_event(ev_p))

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
              agg.get("test-model", {}).get("prompt") == 106
              and agg.get("model-b", {}).get("calls") == 1,
              f"got {agg!r}")

        # 6. hourly windows + burst ratio derivable from raw events
        w, mean, peak, burst = lp.hourly_windows(lp.read_ledgers())
        check("windows_derivable", len(w) >= 1 and mean > 0 and peak,
              f"got {w!r}")

        # 7. corrupt ledger line skipped, not fatal
        files[0].write_text(files[0].read_text() + "NOT JSON\n")
        check("corrupt_line_tolerated", len(lp.read_ledgers()) == 4)

        # 8. scope='all' merges foreign-writer schemas (tokens_in/tokens_out)
        (Path(td) / "ledger.jsonl").write_text(json.dumps(
            {"schema": 1, "ts": "2026-07-27T12:00:00-04:00",
             "provider": "local", "model": "other-writer-model",
             "tokens_in": 500, "tokens_out": 40,
             "project": "another-repo"}) + "\n")
        merged = lp.read_ledgers(scope="all")
        norm = [e for e in merged if e.get("model") == "other-writer-model"]
        check("foreign_schema_merged", len(merged) == 5 and len(norm) == 1
              and norm[0]["prompt_tokens"] == 500
              and lp.is_local_event(norm[0]),
              f"merged={len(merged)} norm={norm!r}")

        # 8b. model-name normalization for price matching
        check("normalize_names",
              lp.normalize_model_name("lmstudio-community/Qwen3-32B-MLX-4bit")
              == "qwen3-32b"
              and lp.normalize_model_name("google/gemma-4-e4b:2")
              == "gemma-4-e4b")

        # 8c. cheapest-hosted match + as-of price join (no network)
        table = {"together/qwen3-32b": {"mode": "chat",
                                        "input_cost_per_token": 4e-7,
                                        "output_cost_per_token": 1.2e-6},
                 "groq/qwen3-32b": {"mode": "chat",
                                    "input_cost_per_token": 2.9e-7,
                                    "output_cost_per_token": 5.9e-7}}
        hit = lp.match_price("Qwen3-32B-MLX-4bit", table)
        check("cheapest_hosted_match", hit is not None
              and abs(hit[0] - 0.29) < 1e-6 and "groq" in hit[2],
              f"got {hit!r}")
        table["azure/qwen3-32b-fp8"] = {"mode": "chat",
                                        "input_cost_per_token": 1e-7,
                                        "output_cost_per_token": 2e-7}
        table["x/gpt-5-nano"] = {"mode": "chat",
                                 "input_cost_per_token": 5e-8,
                                 "output_cost_per_token": 4e-7}
        check("no_variant_overmatch",
              lp.match_price("gpt-5", table) is None  # nano is NOT gpt-5
              and abs(lp.match_price("qwen3-32b", table)[0] - 0.1) < 1e-6,
              "prefix over-match not blocked or fp8 suffix not accepted")
        (Path(td) / "prices.jsonl").write_text(
            '{"ts":"2026-07-01","model":"qwen3-32b","input_per_m":0.40,"output_per_m":1.2,"source":"t"}\n'
            '{"ts":"2026-07-20","model":"qwen3-32b","input_per_m":0.29,"output_per_m":0.59,"source":"t"}\n'
            '{"ts":"2026-07-01","model":"reference","input_per_m":2.5,"output_per_m":10.0,"source":"t"}\n')
        s = lp.load_price_series()
        early = lp.as_of_price("qwen3-32b", "2026-07-10T12:00:00", s)
        late = lp.as_of_price("qwen3-32b", "2026-07-26T12:00:00", s)
        fall = lp.as_of_price("unknown-model", "2026-07-26T12:00:00", s)
        check("as_of_join", early[0] == 0.40 and late[0] == 0.29
              and fall[2] == "reference",
              f"early={early} late={late} fall={fall}")

        # 9. html report renders from the merged dir (interactive version:
        # data island + filter controls + static fallback table)
        out = Path(td) / "report.html"
        lp.html_report(out)
        html = out.read_text()
        check("html_report_renders",
              'type="application/json"' in html
              and "another-repo" in html          # data island has the project
              and 'data-c="local"' in html        # class toggle chips
              and 'data-m="usd"' in html          # $/tokens metric toggle
              and 'id="mlist"' in html            # collapsible model filter
              and "<table>" in html,              # JS-independent fallback
              "missing data island / filters / table")

        # 9a2. claude-code ingest: parse fixture transcript, idempotent merge
        fake_home = Path(td) / "home"
        proj = fake_home / ".claude" / "projects" / "-Users-x-repo"
        proj.mkdir(parents=True)
        line = json.dumps({"uuid": "u-1", "timestamp": "2026-07-27T01:02:03Z",
                           "cwd": "/Users/x/repo",
                           "message": {"model": "claude-sonnet-5", "usage": {
                               "input_tokens": 10,
                               "cache_creation_input_tokens": 90,
                               "cache_read_input_tokens": 1000,
                               "output_tokens": 20}}})
        (proj / "sess.jsonl").write_text(line + "\nnot json but no usage\n")
        real_home = Path.home
        Path.home = staticmethod(lambda: fake_home)  # noqa
        try:
            lp.ingest_claude_code()
            lp.ingest_claude_code()  # second run must not duplicate
        finally:
            Path.home = real_home
        derived = list(Path(td).glob("claude-code-*.jsonl"))
        ok = False
        if len(derived) == 1:
            recs = [json.loads(l) for l in
                    derived[0].read_text().splitlines()]
            ok = (len(recs) == 1 and recs[0]["prompt_tokens"] == 10
                  and recs[0]["cache_w5_tokens"] == 90
                  and recs[0]["cache_read_tokens"] == 1000
                  and recs[0]["subscription"] is True
                  and recs[0]["project"] == "repo")
        check("claude_code_ingest", ok, f"derived={derived!r}")

        # 9a3. subscription rows separate in aggregation + cache discount
        (Path(td) / "prices.jsonl").write_text(
            (Path(td) / "prices.jsonl").read_text()
            + '{"ts":"2026-07-01","model":"claude-sonnet-5",'
              '"input_per_m":3.0,"output_per_m":15.0,"source":"t"}\n')
        ev = lp._all_usage_events()
        sub = [e for e in ev if e["sub"]]
        rows2, _g = lp._agg_rows(ev, lp.load_price_series())
        subrow = [r for r in rows2 if r["u"]]
        # 10 in @$3 + 20 out @$15 + 1000 reads @10% + 90 5m-writes @1.25x
        expect = (10/1e6*3.0 + 20/1e6*15.0 + 1000/1e6*0.3
                  + 90/1e6*3.0*1.25)
        check("subscription_agg_and_cache_price",
              len(sub) == 1 and len(subrow) == 1
              and abs(subrow[0]["s"] - round(expect, 4)) < 1e-6,
              f"sub={sub!r} subrow={subrow!r} expect={expect}")

        # 9b. hostile model name cannot break out of the static table
        lp.log_usage("<script>alert(1)</script>", {"prompt_tokens": 1,
                                                   "completion_tokens": 1}, 0.1)
        lp.html_report(out)
        check("html_escapes_model_names",
              "<script>alert(1)</script>" not in out.read_text())

    os.environ.pop("LLM_TOKEN_LEDGER_DIR", None)

    # 10. project auto-detect returns a non-empty string
    check("project_autodetect", isinstance(lp.detect_project(), str)
          and len(lp.detect_project()) > 0)

    # 11. CLI help exits 0
    p = subprocess.run([sys.executable,
                        str(SKILL_DIR / "llm_usage_ledger.py"),
                        "--help"], capture_output=True, timeout=20)
    check("cli_help", p.returncode == 0)
    return r


def record(model, results, dur):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "skill": "llm-usage-ledger", "skill_version": version(),
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
