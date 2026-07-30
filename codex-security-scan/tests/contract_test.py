#!/usr/bin/env python3
"""Upstream contract test — the drift alarm for codex-security-scan.

Every assumption this skill makes about @openai/codex-security is asserted
here. CI runs it weekly against @latest; when OpenAI ships a release that
breaks one, this goes red and files an issue naming the broken assumption, so
the skill is updated deliberately instead of failing in someone's scan.

  python3 tests/contract_test.py                 # test the pinned version
  python3 tests/contract_test.py --version latest
  python3 tests/contract_test.py --version latest --json

Exit 0 = contract holds. Exit 1 = drift.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

# Upstream skills this skill's SKILL.md sends the model to read.
REQUIRED_SKILLS = ["threat-model", "security-scan", "finding-discovery",
                   "validation", "attack-path-analysis", "security-diff-scan",
                   "triage-finding", "fix-finding"]
# Workbench subcommands csec.py drives.
REQUIRED_CMDS = ["create-workspace", "disable-setup-ui", "start-prompt-only-scan",
                 "complete-scan", "list-global-findings", "list-repositories",
                 "database-info"]
REQUIRED_REFS = ["security-guidance.md", "shared-hard-rules.md",
                 "scan-artifacts.md", "final-report.md"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=None,
                    help="npm version spec (default: UPSTREAM_PIN)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    results: dict[str, str] = {}

    def check(name, cond, why=""):
        results[name] = "pass" if cond else f"FAIL: {why}"

    with tempfile.TemporaryDirectory() as td:
        os.environ["CODEX_SECURITY_SKILL_CACHE"] = td + "/cache"
        os.environ["CODEX_SECURITY_STATE_DIR"] = td + "/state"
        import csec
        # re-read module-level paths under the temp env
        csec.CACHE = Path(td) / "cache"
        csec.STATE = Path(td) / "state"
        if a.version:
            csec.PIN = a.version

        try:
            info = csec.bootstrap()
        except subprocess.CalledProcessError as e:
            print(f"FATAL: npm install failed: {e}", file=sys.stderr)
            return 1
        version = info["version"]
        root = Path(info["pluginRoot"])

        check("bundled_plugin_present", root.is_dir(),
              "_bundled_plugin missing from the npm package")
        check("workbench_present", Path(info["workbench"]).is_file(),
              "scripts/workbench_db.py moved or renamed")

        # --- methodology still ships as readable prose ----------------------
        for s in REQUIRED_SKILLS:
            p = root / "skills" / s / "SKILL.md"
            ok = p.is_file()
            check(f"skill_{s.replace('-', '_')}", ok, f"skills/{s}/SKILL.md missing")
            if ok:
                fm = re.match(r"^---\n(.*?)\n---\n", p.read_text(), re.S)
                check(f"skill_{s.replace('-', '_')}_frontmatter", bool(fm),
                      f"skills/{s}/SKILL.md lost its YAML frontmatter")
        for r in REQUIRED_REFS:
            check(f"ref_{r.replace('.', '_').replace('-', '_')}",
                  (root / "references" / r).is_file(), f"references/{r} missing")

        # --- the prompt-only escape hatch this whole skill depends on -------
        ss = (root / "skills" / "security-scan" / "SKILL.md")
        txt = ss.read_text() if ss.is_file() else ""
        check("prompt_only_path_documented", "prompt-only" in txt,
              "security-scan/SKILL.md no longer documents the prompt-only path — "
              "the non-MCP route may be gone")

        # --- workbench CLI surface ------------------------------------------
        helptext = subprocess.run([sys.executable, info["workbench"], "--help"],
                                  capture_output=True, text=True).stdout
        for c in REQUIRED_CMDS:
            check(f"cmd_{c.replace('-', '_')}", c in helptext,
                  f"workbench subcommand '{c}' gone")

        # --- schemas + enums csec.py validates against ----------------------
        sch = root / "schemas"
        check("schemas_present",
              all((sch / f"{n}.schema.json").is_file()
                  for n in ("coverage", "findings", "scan-manifest")),
              "a contract schema was removed")
        if (sch / "coverage.schema.json").is_file():
            cov = json.loads((sch / "coverage.schema.json").read_text())
            props = cov.get("properties", {})
            check("enum_coverage_mode",
                  set(props.get("mode", {}).get("enum", [])) == set(csec.COVERAGE_MODES),
                  f"coverage.mode enum changed: {props.get('mode', {}).get('enum')}")
            disp = ((props.get("surfaces", {}).get("items", {})
                     .get("properties", {}).get("disposition", {})).get("enum", []))
            check("enum_disposition", set(disp) == set(csec.DISPOSITIONS),
                  f"surface disposition enum changed: {disp}")
            req = set(cov.get("required", []))
            check("coverage_required_stable",
                  {"documentType", "scanId", "mode", "completeness",
                   "inventoryStrategy", "surfaces"} <= req,
                  f"coverage required fields changed: {sorted(req)}")
        if (sch / "findings.schema.json").is_file():
            fs = json.loads((sch / "findings.schema.json").read_text())
            fr = set(fs["properties"]["findings"]["items"].get("required", []))
            sev_enum = (fs["properties"]["findings"]["items"]["properties"]
                        ["severity"]["properties"]["level"].get("enum", []))
            check("enum_severity", set(sev_enum) == set(csec.SEVERITIES),
                  f"finding severity enum changed: {sev_enum} vs {csec.SEVERITIES}")
            check("findings_required_stable",
                  {"findingId", "occurrenceId", "ruleId", "identity",
                   "fingerprints", "title", "summary", "severity", "confidence",
                   "taxonomy", "locations", "remediation", "provenance"} <= fr,
                  f"finding required fields changed: {sorted(fr)}")

        # --- upstream python stays local-only -------------------------------
        net = []
        for p in (root / "scripts").rglob("*.py"):
            t = p.read_text(errors="ignore")
            if re.search(r"^\s*import\s+(requests|httpx|urllib\.request)", t, re.M) \
               or "urlopen(" in t or "socket.socket(" in t:
                net.append(p.name)
        check("upstream_scripts_offline", not net,
              f"upstream scripts gained network calls: {net} — review before trusting")

        # --- end-to-end lifecycle on a throwaway repo -----------------------
        repo = Path(td) / "fixture"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "x.py").write_text("import os\n\ndef f(p):\n    open(p,'w')\n")
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-qm", "f"]):
            subprocess.run(["git", *cmd], cwd=repo, check=True,
                           capture_output=True)
        try:
            recipe = csec.start(repo, ".", "contract-fixture")
            check("lifecycle_start", bool(recipe.get("scanId") and recipe.get("scanDir")),
                  "start-prompt-only-scan returned no scan")
            check("lifecycle_recipe_fields",
                  all(recipe.get(k) for k in ("targetId", "targetKind", "displayName")),
                  "scan contract no longer exposes target binding fields")
            an = Path(td) / "analysis.json"
            an.write_text(json.dumps({
                "findings": [{"ruleId": "t.t", "anchor": "a", "title": "T",
                              "summary": "S", "severity": "medium",
                              "category": "other", "cwe": [],
                              "locations": [{"path": "src/x.py", "startLine": 4,
                                             "endLine": 4, "role": "sink"}],
                              "remediation": "R"}],
                "surfaces": [{"id": "s", "label": "L", "disposition": "reported"}]}))
            out = csec.finish(Path(recipe["scanDir"]), an)
            check("lifecycle_complete", out.get("status") == "complete",
                  f"complete-scan did not complete: {out.get('status')}")
            check("lifecycle_report", Path(out["report"]).is_file(),
                  "report.md not generated")
            check("lifecycle_sarif", Path(out["sarif"]).is_file(),
                  "SARIF export not generated")
            got = csec.collect()
            check("lifecycle_registered",
                  any(f.get("title") == "T" for f in got["findings"]),
                  "finding did not land in the cross-repo workbench")
        except SystemExit as e:
            check("lifecycle_complete", False, f"lifecycle aborted: {e}")

    fails = {k: v for k, v in results.items() if v != "pass"}
    if a.json:
        print(json.dumps({"upstreamVersion": version, "pinned": csec.PIN,
                          "failures": fails, "results": results}, indent=2))
    else:
        for k, v in results.items():
            print(f"  {k}: {v}")
        print(f"\nupstream @openai/codex-security {version}")
        print("CONTRACT HOLDS" if not fails else f"{len(fails)} CONTRACT BREAKS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
