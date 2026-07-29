#!/usr/bin/env python3
"""Regression tests + telemetry for codex-security-scan (convention: skills README).

Offline and deterministic: no npm, no network, no OpenAI. The parts that depend
on a live upstream release are covered by tests/contract_test.py, which the
weekly CI cron runs against @latest.

Usage: run_tests.py --model <id> [--auto] | --report
"""
import argparse
import datetime
import json
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
LEDGER = SKILL_DIR.parent / "telemetry" / "codex-security-scan.jsonl"
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

    # ---- SKILL.md contract -------------------------------------------------
    sm = (SKILL_DIR / "SKILL.md").read_text()
    fm = re.match(r"^---\n(.*?)\n---\n", sm, re.S)
    check("frontmatter_present", bool(fm), "no YAML frontmatter")
    if fm:
        block = fm.group(1)
        check("frontmatter_name", "name: codex-security-scan" in block, "name mismatch")
        desc = re.search(r"description:\s*(.+)", block)
        check("frontmatter_description", bool(desc) and len(desc.group(1)) > 80,
              "description missing or too short to trigger reliably")
    check("skill_documents_unsealed_rule", "sealedAt" in sm and "unsealed" in sm,
          "the seal-ordering trap is undocumented")
    check("skill_has_translation_map", "prompt-only" in sm and "pluginRoot" in sm,
          "Codex-ism translation map missing")
    check("skill_attributes_upstream", "Apache-2.0" in sm and "codex-security" in sm,
          "upstream attribution missing")
    check("notice_present", (SKILL_DIR / "NOTICE").is_file(), "NOTICE missing")
    check("upstream_pinned",
          re.fullmatch(r"\d+\.\d+\.\d+", (SKILL_DIR / "UPSTREAM_PIN").read_text().strip() or ""),
          "UPSTREAM_PIN must be an exact version")

    # ---- csec: enums + id stability ---------------------------------------
    import csec
    check("severities", csec.SEVERITIES == ("critical", "high", "medium", "low", "info"))
    check("dispositions_match_upstream",
          set(csec.DISPOSITIONS) == {"reported", "no_issue_found", "rejected",
                                     "not_applicable", "needs_follow_up"})
    check("coverage_modes_include_scoped",
          {"repository", "scoped_path"} <= set(csec.COVERAGE_MODES))
    a = csec._fid("csf_", "repo", "rule", "anchor", "src/x.py")
    b = csec._fid("csf_", "repo", "rule", "anchor", "src/x.py")
    c = csec._fid("csf_", "repo", "rule", "anchor", "src/y.py")
    check("finding_id_stable", a == b, "same input produced different ids")
    check("finding_id_distinct", a != c, "different paths collided")
    check("finding_id_shape", a.startswith("csf_") and len(a) == 28)

    # ---- finish(): artifact assembly, without touching a real workbench ----
    calls = []
    orig = csec.wb
    csec.wb = lambda *args, **kw: (calls.append(args) or {"scan": {
        "progress": {"status": "complete"}, "findingCount": 1,
        "severityCounts": {"high": 1}}})
    try:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            (sd / "recipe.json").write_text(json.dumps({
                "scanId": "sid", "scanDir": str(sd), "targetId": "tid",
                "targetKind": "git_revision", "displayName": "demo",
                "targetRevision": "abc123", "includePaths": ["."],
                "excludePaths": [], "coverageMode": "repository"}))
            good = {"findings": [{
                "ruleId": "r.x", "anchor": "anc", "title": "T", "summary": "S",
                "severity": "high", "category": "injection", "cwe": ["CWE-89"],
                "locations": [{"path": "a.py", "startLine": 3, "endLine": 4,
                               "role": "sink"}],
                "remediation": "Fix"}],
                "surfaces": [{"id": "s1", "label": "L", "disposition": "reported"}]}
            ap = sd / "analysis.json"
            ap.write_text(json.dumps(good))
            out = csec.finish(sd, ap)

            man = json.loads((sd / "scan-manifest.json").read_text())
            check("manifest_unsealed", "sealedAt" not in man["scan"]
                  and "artifacts" not in man["scan"],
                  "manifest was pre-sealed — complete-scan would reject it")
            check("manifest_docttype",
                  man["documentType"] == "codex-security.scan-manifest")
            check("manifest_binds_target",
                  man["scan"]["target"]["targetId"] == "tid"
                  and man["scan"]["target"]["revision"] == "abc123")
            fnd = json.loads((sd / "findings.json").read_text())
            check("findings_scanid", fnd["scanId"] == "sid")
            check("findings_required_keys",
                  {"findingId", "occurrenceId", "ruleId", "identity",
                   "fingerprints", "title", "summary", "severity", "confidence",
                   "taxonomy", "locations", "remediation", "provenance"}
                  <= set(fnd["findings"][0]))
            check("fingerprint_algorithm",
                  fnd["findings"][0]["fingerprints"]["algorithm"] == "codex-security/v1")
            cov = json.loads((sd / "coverage.json").read_text())
            check("coverage_required_keys",
                  {"documentType", "schemaVersion", "scanId", "mode",
                   "completeness", "inventoryStrategy", "includePaths",
                   "excludePaths", "surfaces", "explicitExclusions", "deferred"}
                  <= set(cov))
            check("coverage_receiptrefs_defaulted",
                  cov["surfaces"][0].get("receiptRefs") == [])
            check("finish_calls_complete_scan",
                  any(x and x[0] == "complete-scan" for x in calls))
            check("finish_returns_counts", out["findingCount"] == 1)

            # rejections
            def rejects(mutate, label):
                bad = json.loads(json.dumps(good))
                mutate(bad)
                ap.write_text(json.dumps(bad))
                try:
                    csec.finish(sd, ap)
                    return False
                except SystemExit:
                    return True
                finally:
                    ap.write_text(json.dumps(good))

            check("rejects_bad_severity",
                  rejects(lambda b: b["findings"][0].update(severity="urgent"), "sev"))
            check("rejects_missing_location",
                  rejects(lambda b: b["findings"][0].update(locations=[]), "loc"))
            check("rejects_bad_disposition",
                  rejects(lambda b: b["surfaces"][0].update(disposition="maybe"), "disp"))
    finally:
        csec.wb = orig

    # ---- dashboard ---------------------------------------------------------
    from dashboard import page, META, ORDER
    data = {"repositories": [{"displayName": "alpha", "targetId": "t1"},
                             {"displayName": "beta", "targetId": "t2"}],
            "findings": [
                {"findingId": "f1", "severity": {"level": "critical"}, "status": "open",
                 "title": "Crit <script>x</script>", "summary": "s", "repository": "alpha",
                 "locationPath": "a.py", "line": 2, "cwe": ["CWE-1"], "remediation": "r"},
                {"findingId": "f2", "severity": {"level": "low"}, "status": "open",
                 "title": "Low one", "summary": "s", "repository": "alpha",
                 "locationPath": "b.py", "line": 3, "cwe": [], "remediation": "r"}]}
    h = page(data, "T")
    check("dash_doctype", h.startswith("<!doctype html>"))
    check("dash_charset", 'charset="utf-8"' in h)
    check("dash_self_contained",
          not re.search(r'(?:src|href)\s*=\s*["\']https?://', h),
          "external asset reference")
    check("dash_dark_both_scopes",
          "prefers-color-scheme:dark" in h and '[data-theme="dark"]' in h,
          "dark mode must answer OS setting AND theme toggle")
    check("dash_escapes_html", "<script>x</script>" not in h,
          "finding title was not escaped — XSS in the report")
    check("dash_severity_icon_and_label",
          all(META[s]["icon"] in h for s in ("critical", "low")),
          "status colour must ship with an icon, never colour alone")
    check("dash_counts_repos", ">2<" in h, "repository tile count wrong")
    check("dash_clean_repo_marked", "no open findings" in h,
          "repo with zero findings not shown as clean")
    check("dash_has_table_view", "<table" in h, "no table view for accessibility")
    check("dash_order_worst_first", ORDER[0] == "critical")
    return r


def record(model, results, dur):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "skill": "codex-security-scan", "skill_version": version(),
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
        print(f"telemetry: {AUTO_RUNS} runs recorded for {a.model} @ {version()} — skipping")
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
