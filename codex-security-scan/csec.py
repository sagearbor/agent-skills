#!/usr/bin/env python3
"""Lifecycle helper for the codex-security-scan skill.

Wraps the upstream @openai/codex-security workbench (Apache-2.0, OpenAI) so a
Claude Code session can drive it with NO OpenAI credentials. Upstream ships the
methodology (skills + reference prose + schemas + SQLite workbench + SARIF); this
file owns only the contract plumbing that is easy to get wrong, so the model
supplies security judgement and nothing else.

Upstream is NOT vendored. It is installed from npm into a cache dir, so their
updates arrive without edits here. `tests/contract_test.py` fails loudly if an
upstream release breaks an assumption below.

Subcommands:
  bootstrap  install/refresh upstream from npm, print resolved paths
  doctor     verify the toolchain end to end
  start      open a headless prompt-only scan, emit recipe.json
  finish     assemble contract artifacts from a findings file, seal, register
  list       cross-repo findings / repositories from the shared workbench
  dashboard  render a self-contained cross-repo HTML dashboard
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

PKG = "@openai/codex-security"
CACHE = Path(os.environ.get("CODEX_SECURITY_SKILL_CACHE",
                            Path.home() / ".cache" / "codex-security-skill"))
STATE = Path(os.environ.get("CODEX_SECURITY_STATE_DIR",
                            Path.home() / ".codex-security-state"))
PIN = (Path(__file__).resolve().parent / "UPSTREAM_PIN").read_text().strip() \
    if (Path(__file__).resolve().parent / "UPSTREAM_PIN").exists() else "latest"

# Upstream enum surfaces the model must stay inside (asserted by contract_test).
COVERAGE_MODES = ("repository", "scoped_path", "diff", "commit", "branch_diff",
                  "working_tree", "deep_repository")
INVENTORY = ("repository", "scoped_path", "diff", "directory", "custom")
DISPOSITIONS = ("reported", "no_issue_found", "rejected", "not_applicable",
                "needs_follow_up")
SEVERITIES = ("critical", "high", "medium", "low", "info")


# ---------------------------------------------------------------- paths / npm

def plugin_root() -> Path:
    return CACHE / "node_modules" / PKG / "_bundled_plugin"


def workbench() -> Path:
    return plugin_root() / "scripts" / "workbench_db.py"


def bootstrap(force: bool = False) -> dict:
    """Install upstream from npm into the cache. Idempotent and offline-safe."""
    if force or not workbench().exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        if not (CACHE / "package.json").exists():
            (CACHE / "package.json").write_text(
                json.dumps({"name": "codex-security-skill-cache", "private": True}))
        spec = f"{PKG}@{PIN}"
        subprocess.run(["npm", "install", spec, "--no-audit", "--no-fund",
                        "--loglevel", "error"],
                       cwd=CACHE, check=True)
    ver = json.loads((CACHE / "node_modules" / PKG / "package.json").read_text())["version"]
    return {"version": ver, "pluginRoot": str(plugin_root()),
            "workbench": str(workbench()), "stateDir": str(STATE),
            "pinned": PIN}


def wb(*args: str, check: bool = True) -> dict | str:
    """Invoke the upstream workbench CLI with our shared state dir."""
    env = {**os.environ, "CODEX_SECURITY_STATE_DIR": str(STATE)}
    STATE.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([sys.executable, str(workbench()), *args],
                       capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise SystemExit(f"workbench {args[0]} failed: {p.stderr.strip() or p.stdout.strip()}")
    out = p.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


# ------------------------------------------------------------------ lifecycle

def start(target: Path, scope: str, title: str | None) -> dict:
    """Open a headless prompt-only scan and write recipe.json into the scan dir."""
    bootstrap()
    target = target.resolve()
    ws, th = str(uuid.uuid4()), str(uuid.uuid4())
    title = title or target.name
    wb("create-workspace", "--workspace-id", ws, "--thread-id", th,
       "--target-path", str(target), "--target-title", title,
       "--scope", scope, "--mode", "standard")
    wb("disable-setup-ui", "--workspace-id", ws)
    res = wb("start-prompt-only-scan", "--thread-id", th,
             "--target-path", str(target), "--scope", scope, "--mode", "standard")
    scan = res["scan"]
    contract = scan["contract"]
    recipe = {
        "scanId": scan["scanId"],
        "scanDir": scan["scanDir"],
        "targetPath": scan["targetPath"],
        "targetRevision": scan.get("targetRevision"),
        "targetId": contract["target"]["targetId"],
        "targetKind": (contract["target"]["allowedKinds"] or ["git_revision"])[0],
        "displayName": contract["target"]["displayName"],
        "includePaths": contract["scope"]["requiredIncludePaths"],
        "excludePaths": contract["scope"]["requiredExcludePaths"],
        # scope "." (whole repo) reports as repository coverage; anything else is scoped.
        "coverageMode": "repository" if scope.strip(".") in ("", "/") else "scoped_path",
        "filesTotal": scan.get("progress", {}).get("coverage", {}).get("filesTotal"),
    }
    sd = Path(recipe["scanDir"])
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "recipe.json").write_text(json.dumps(recipe, indent=2))
    return recipe


def _fid(prefix: str, *parts: str) -> str:
    """Stable id so a re-scan of unchanged code produces the same finding id."""
    return prefix + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]


def finish(scan_dir: Path, analysis_path: Path) -> dict:
    """Turn a model-authored analysis file into sealed, registered artifacts.

    analysis.json shape (everything optional except findings/surfaces):
      {"findings": [ {ruleId, anchor, title, summary, severity, confidence,
                      rationale, category, cwe: [...], locations: [{path,
                      startLine, endLine, role}], remediation} ],
       "surfaces": [ {id, label, disposition, riskArea, notes} ],
       "completeness": "complete", "explicitExclusions": [], "deferred": [],
       "threatModel": {"summary": "..."}}
    """
    scan_dir = Path(scan_dir)
    recipe = json.loads((scan_dir / "recipe.json").read_text())
    a = json.loads(Path(analysis_path).read_text())
    sid, repo = recipe["scanId"], recipe["displayName"]

    findings = []
    for f in a.get("findings", []):
        sev = str(f.get("severity", "medium")).lower()
        if sev not in SEVERITIES:
            raise SystemExit(f"severity {sev!r} not in {SEVERITIES}")
        locs = f.get("locations") or []
        if not locs:
            raise SystemExit(f"finding {f.get('title')!r} has no locations")
        anchor = f.get("anchor") or f.get("ruleId", "finding")
        key = (repo, f.get("ruleId", ""), anchor, locs[0].get("path", ""))
        findings.append({
            "findingId": _fid("csf_", *key),
            "occurrenceId": _fid("occ_", *key, str(locs[0].get("startLine", 0))),
            "ruleId": f.get("ruleId", "generic.finding"),
            "identity": {"anchor": anchor},
            "fingerprints": {
                "algorithm": "codex-security/v1",
                "primary": "codex-security/v1:sha256:" +
                           hashlib.sha256("\x1f".join(key).encode()).hexdigest()},
            "title": f["title"],
            "summary": f["summary"],
            "severity": {"level": sev},
            "confidence": {"level": f.get("confidence", "medium"),
                           "rationale": f.get("rationale", "See summary.")},
            "taxonomy": {"category": f.get("category", "other"),
                         "cwe": f.get("cwe", [])},
            "locations": locs,
            "remediation": f.get("remediation", "See summary."),
            "validation": f.get("validation"),
            "attackPath": f.get("attackPath"),
            "provenance": {"source": "local_plugin"},
            "extensions": {},
        })

    (scan_dir / "findings.json").write_text(json.dumps({
        "documentType": "codex-security.findings", "schemaVersion": "1.0",
        "scanId": sid, "findings": findings}, indent=2))

    surfaces = a.get("surfaces") or [{
        "id": "surface_repository", "label": f"{repo} reviewed files",
        "disposition": "reported" if findings else "no_issue_found",
        "receiptRefs": []}]
    for s in surfaces:
        s.setdefault("receiptRefs", [])
        if s.get("disposition") not in DISPOSITIONS:
            raise SystemExit(f"disposition {s.get('disposition')!r} not in {DISPOSITIONS}")
    mode = recipe["coverageMode"]
    (scan_dir / "coverage.json").write_text(json.dumps({
        "documentType": "codex-security.coverage", "schemaVersion": "1.0",
        "scanId": sid, "mode": mode,
        "completeness": a.get("completeness", "complete"),
        "inventoryStrategy": mode if mode in INVENTORY else "custom",
        "includePaths": recipe["includePaths"],
        "excludePaths": recipe["excludePaths"],
        "surfaces": surfaces,
        "explicitExclusions": a.get("explicitExclusions", []),
        "deferred": a.get("deferred", [])}, indent=2))

    # UNSEALED draft only. The workbench populates startedAt/completedAt/
    # producer/sealedAt/artifacts during complete-scan; pre-sealing here makes
    # complete-scan reject the manifest (see tests/contract_test.py).
    scan_obj = {
        "id": sid, "status": "completed",
        "target": {"kind": recipe["targetKind"], "targetId": recipe["targetId"],
                   "displayName": recipe["displayName"]},
        "scope": {"includePaths": recipe["includePaths"],
                  "excludePaths": recipe["excludePaths"]},
        "coverageRef": "coverage.json", "findingsRef": "findings.json"}
    if recipe.get("targetRevision"):
        scan_obj["target"]["revision"] = recipe["targetRevision"]
    if a.get("threatModel"):
        scan_obj["threatModel"] = a["threatModel"]
    (scan_dir / "scan-manifest.json").write_text(json.dumps({
        "documentType": "codex-security.scan-manifest", "schemaVersion": "1.0",
        "scan": scan_obj}, indent=2))

    res = wb("complete-scan", "--scan-id", sid)
    scan = res.get("scan", res)
    return {"scanId": sid, "scanDir": str(scan_dir),
            "status": scan.get("progress", {}).get("status"),
            "findingCount": scan.get("findingCount"),
            "severityCounts": scan.get("severityCounts"),
            "report": str(scan_dir / "report.md"),
            "sarif": str(scan_dir / "exports" / "results.sarif")}


# ------------------------------------------------------------------- readouts

def collect() -> dict:
    """Everything the dashboard needs, straight from the shared workbench.

    The global findings table carries identity + severity but not taxonomy or
    remediation, so enrich from each repository's sealed findings.json.
    """
    repos = wb("list-repositories").get("repositories", [])
    findings, offset = [], 0
    while True:
        page = wb("list-global-findings", "--offset", str(offset))
        rows = page.get("findings", [])
        findings.extend(rows)
        nxt = page.get("nextOffset")
        if not nxt or not rows:
            break
        offset = nxt

    detail, repo_of = {}, {}
    for r in repos:
        repo_of[r.get("targetId")] = r.get("displayName")
        sd = (r.get("latestScan") or {}).get("scanDir")
        if not sd:
            continue
        fp = Path(sd) / "findings.json"
        if not fp.exists():
            continue
        try:
            for f in json.loads(fp.read_text()).get("findings", []):
                detail[f["findingId"]] = f
        except (OSError, json.JSONDecodeError, KeyError):
            continue

    for f in findings:
        d = detail.get(f.get("findingId"), {})
        f["repository"] = repo_of.get(f.get("targetId")) or Path(
            f.get("targetPath", "")).name or "unknown"
        f["category"] = (d.get("taxonomy") or {}).get("category")
        f["cwe"] = (d.get("taxonomy") or {}).get("cwe", [])
        f["remediation"] = d.get("remediation")
        f["confidence"] = (d.get("confidence") or {}).get("level")
        f["ruleId"] = d.get("ruleId")
        locs = d.get("locations") or []
        f["line"] = locs[0].get("startLine") if locs else None
    return {"repositories": repos, "findings": findings}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap").add_argument("--force", action="store_true")
    sub.add_parser("doctor")
    s = sub.add_parser("start")
    s.add_argument("--target", required=True)
    s.add_argument("--scope", default=".")
    s.add_argument("--title")
    f = sub.add_parser("finish")
    f.add_argument("--scan-dir", required=True)
    f.add_argument("--analysis", required=True)
    sub.add_parser("list")
    d = sub.add_parser("dashboard")
    d.add_argument("--output", default="security-dashboard.html")
    d.add_argument("--title", default="Security posture")
    a = ap.parse_args()

    if a.cmd == "bootstrap":
        print(json.dumps(bootstrap(force=a.force), indent=2))
    elif a.cmd == "doctor":
        info = bootstrap()
        ok = {"npm": bool(shutil.which("npm")),
              "workbench": workbench().exists(),
              "skills": (plugin_root() / "skills").is_dir(),
              "schemas": (plugin_root() / "schemas").is_dir(),
              "db": bool(wb("database-info"))}
        print(json.dumps({**info, "checks": ok}, indent=2))
        sys.exit(0 if all(ok.values()) else 1)
    elif a.cmd == "start":
        print(json.dumps(start(Path(a.target), a.scope, a.title), indent=2))
    elif a.cmd == "finish":
        print(json.dumps(finish(Path(a.scan_dir), Path(a.analysis)), indent=2))
    elif a.cmd == "list":
        print(json.dumps(collect(), indent=2))
    elif a.cmd == "dashboard":
        from dashboard import page  # local module, same dir
        out = Path(a.output).resolve()
        out.write_text(page(collect(), a.title))
        print(json.dumps({"dashboard": str(out)}, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
