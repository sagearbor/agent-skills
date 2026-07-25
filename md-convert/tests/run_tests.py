#!/usr/bin/env python3
"""
Regression tests + per-model telemetry for the md-convert skill.

Deterministic fixture tests (stdlib only) with an append-only JSONL ledger
so skill health accumulates into a model x skill-version matrix as models
change over time.

Usage:
    run_tests.py --model <model-id>            # run tests + record to ledger
    run_tests.py --model <model-id> --auto     # run+record ONLY if this
                                               #   (model, skill version) has
                                               #   fewer than 8 recorded runs
    run_tests.py --report                      # print the matrix (mean +/- SD)

Ledger: ~/.claude/skills/telemetry/md-convert.jsonl (one JSON object per run).
Convention for Claude: pass your own model id (e.g. claude-fable-5). See
SKILL.md rule 5.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEDGER = SKILL_DIR.parent / "telemetry" / "md-convert.jsonl"
CONVERT = SKILL_DIR / "convert.py"
AUTO_RUNS_PER_MODEL = 8


def skill_version() -> str:
    try:
        return (SKILL_DIR / "VERSION").read_text().strip()
    except OSError:
        return "unversioned"


def _toc_entries(docx: Path):
    """[(anchor, text)] for TOC hyperlinks + {bookmark: heading text}, plus
    mismatch count between each entry and the heading its anchor targets."""
    xml = zipfile.ZipFile(docx).read("word/document.xml").decode("utf8")
    links = re.findall(
        r'<w:hyperlink w:anchor="(_Toc9\d+)" w:history="1">'
        r"(?:(?!</w:hyperlink>).)*?<w:t[^>]*>([^<]*)</w:t>", xml)
    bms = {}
    for m in re.finditer(r'<w:bookmarkStart w:id="\d+" w:name="(_Toc9\d+)"/>', xml):
        seg = xml[m.end():xml.find("</w:p>", m.end())]
        bms[m.group(1)] = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", seg))
    mismatches = sum(1 for a, t in links if bms.get(a) != t)
    return [t for _, t in links], mismatches


def _convert(md: Path, out: Path, *flags: str) -> None:
    subprocess.run(
        [sys.executable, str(CONVERT), str(md), "--out", str(out), *flags],
        check=True, capture_output=True,
    )


def run_suite() -> dict:
    """Run all fixture tests; return {test_name: 'pass'|'FAIL: reason'}."""
    results: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        def check(name: str, cond: bool, why: str = "") -> None:
            results[name] = "pass" if cond else f"FAIL: {why}"

        # 1. H1-sectioned doc: 4 TOC entries, code-block '#' excluded,
        #    every entry hyperlinked to its own heading.
        out = tmp / "h1.docx"
        _convert(FIXTURES / "h1_sections.md", out, "--to", "docx", "--toc", "--no-stamp")
        entries, mm = _toc_entries(out)
        check("toc_h1_sections", entries == ["First Section", "Sub A", "Second Section", "Sub B"],
              f"entries={entries}")
        check("toc_pairing_h1", mm == 0, f"{mm} mismatched hyperlinks")
        check("toc_code_excluded", not any("code comment" in e for e in entries), str(entries))

        # 2. Title + ##/### doc: lone leading H1 excluded, levels normalized.
        out = tmp / "t2.docx"
        _convert(FIXTURES / "title_plus_h2.md", out, "--to", "docx", "--toc", "--no-stamp")
        entries, mm = _toc_entries(out)
        check("toc_title_excluded", entries == ["Background", "Detail One", "Results"],
              f"entries={entries}")
        check("toc_pairing_t2", mm == 0, f"{mm} mismatched hyperlinks")

        # 3. Stamp on/off.
        out = tmp / "stamped.docx"
        _convert(FIXTURES / "title_plus_h2.md", out, "--to", "docx", "--stamp")
        xml = zipfile.ZipFile(out).read("word/document.xml").decode("utf8")
        check("stamp_on", "Generated: " in xml, "no stamp line in docx")
        out = tmp / "unstamped.docx"
        _convert(FIXTURES / "title_plus_h2.md", out, "--to", "docx", "--no-stamp")
        xml = zipfile.ZipFile(out).read("word/document.xml").decode("utf8")
        check("stamp_off", "Generated: " not in xml, "unexpected stamp line")

        # 4. html conversion smoke.
        out = tmp / "t.html"
        _convert(FIXTURES / "title_plus_h2.md", out, "--to", "html", "--no-stamp")
        check("html_smoke", "Background" in out.read_text(), "heading missing from html")

        # 5. xlsx table extraction (needs openpyxl; skip cleanly if absent).
        try:
            import openpyxl  # noqa: F401
            out = tmp / "t.xlsx"
            _convert(FIXTURES / "title_plus_h2.md", out, "--to", "xlsx", "--no-stamp")
            ws = openpyxl.load_workbook(out)["content"]
            cells = [c.value for row in ws.iter_rows() for c in row if c.value]
            check("xlsx_table", "a" in cells and "1" in cells, f"cells={cells[:8]}")
        except ImportError:
            results["xlsx_table"] = "skip (no openpyxl)"
    return results


def record(model: str, results: dict, duration: float) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "skill": "md-convert",
        "skill_version": skill_version(),
        "model": model,
        "pandoc": subprocess.run(["pandoc", "--version"], capture_output=True,
                                 text=True).stdout.splitlines()[0],
        "n_pass": sum(1 for v in results.values() if v == "pass"),
        "n_fail": sum(1 for v in results.values() if v.startswith("FAIL")),
        "duration_s": round(duration, 2),
        "results": results,
    }
    with open(LEDGER, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]


def runs_for(model: str) -> int:
    v = skill_version()
    return sum(1 for e in load_ledger()
               if e.get("model") == model and e.get("skill_version") == v)


def report() -> None:
    rows: dict[tuple, list[dict]] = {}
    for e in load_ledger():
        rows.setdefault((e["skill_version"], e["model"]), []).append(e)
    if not rows:
        print("ledger empty — no recorded runs yet")
        return
    print(f"{'skill_version':16s} {'model':26s} {'runs':>4s} {'pass_rate':>9s} {'dur_s mean±SD':>14s}")
    for (ver, model), es in sorted(rows.items()):
        total = sum(e["n_pass"] + e["n_fail"] for e in es)
        passed = sum(e["n_pass"] for e in es)
        durs = [e["duration_s"] for e in es]
        sd = statistics.stdev(durs) if len(durs) > 1 else 0.0
        print(f"{ver:16s} {model:26s} {len(es):>4d} {passed/total:>8.0%} "
              f"{statistics.mean(durs):>7.2f}±{sd:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="model id of the calling assistant (e.g. claude-fable-5)")
    ap.add_argument("--auto", action="store_true",
                    help=f"only run+record if this (model, version) has < {AUTO_RUNS_PER_MODEL} runs")
    ap.add_argument("--report", action="store_true", help="print the model x version matrix")
    args = ap.parse_args()

    if args.report:
        report()
        return
    if not args.model:
        sys.exit("--model required (or use --report)")

    if args.auto and runs_for(args.model) >= AUTO_RUNS_PER_MODEL:
        print(f"telemetry: {AUTO_RUNS_PER_MODEL} runs already recorded for "
              f"{args.model} @ {skill_version()} — skipping (use without --auto to force)")
        return

    t0 = time.time()
    results = run_suite()
    duration = time.time() - t0
    record(args.model, results, duration)

    n_fail = sum(1 for v in results.values() if v.startswith("FAIL"))
    for k, v in results.items():
        print(f"  {k}: {v}")
    print(f"{'ALL PASS' if n_fail == 0 else f'{n_fail} FAILURES'} "
          f"({duration:.1f}s, run #{runs_for(args.model)} for {args.model})")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
