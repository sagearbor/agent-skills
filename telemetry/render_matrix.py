#!/usr/bin/env python3
"""Render the skill-telemetry matrix as matrix.md + matrix.svg (committed,
so GitHub shows current test results graphically at any time).

Reads every telemetry/<skill>.jsonl ledger; one row per (skill, version,
model): runs, pass rate, duration mean±SD. The SVG is a simple pass-rate
bar chart grouped by skill. Stdlib only. Run from anywhere:

    python3 telemetry/render_matrix.py
"""
import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
GREEN, RED, INK, MUTED, GRID = "#008300", "#e34948", "#1c2733", "#5b6b7b", "#e3e8ee"


def rows():
    out = []
    for f in sorted(HERE.glob("*.jsonl")):
        agg = {}
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (e.get("skill", f.stem), e.get("skill_version", "?"),
                   e.get("model", "?"))
            agg.setdefault(key, []).append(e)
        for (skill, ver, model), es in sorted(agg.items()):
            tot = sum(e.get("n_pass", 0) + e.get("n_fail", 0) for e in es)
            ok = sum(e.get("n_pass", 0) for e in es)
            d = [e.get("duration_s", 0) for e in es]
            sd = statistics.stdev(d) if len(d) > 1 else 0.0
            out.append({"skill": skill, "version": ver, "model": model,
                        "runs": len(es), "pass": (ok / tot) if tot else 0.0,
                        "dur": statistics.mean(d) if d else 0.0, "sd": sd,
                        "last": max(e.get("ts", "") for e in es)[:10]})
    return out


def write_md(rs):
    lines = ["# Skill regression matrix",
             "",
             "Auto-rendered from `telemetry/*.jsonl` (append-only run "
             "ledgers; each skill's `run_tests.py --auto --model <id>` "
             "self-caps at 8 runs per model+version). Regenerate: "
             "`python3 telemetry/render_matrix.py`.",
             "",
             "![pass-rate matrix](matrix.svg)",
             "",
             "| skill | version | model | runs | pass | duration | last run |",
             "|---|---|---|---|---|---|---|"]
    for r in rs:
        lines.append(
            f"| {r['skill']} | {r['version']} | {r['model']} | {r['runs']} "
            f"| {r['pass']:.0%} | {r['dur']:.2f}±{r['sd']:.2f}s | {r['last']} |")
    (HERE / "matrix.md").write_text("\n".join(lines) + "\n")


def write_svg(rs):
    bh, gap, lw, w = 20, 3, 320, 720
    h = len(rs) * (bh + gap) + 30
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
         f'font-family="sans-serif">',
         f'<text x="{lw}" y="14" font-size="11" fill="{MUTED}">pass rate '
         f'(green = 100%) by skill / version / model</text>']
    for i, r in enumerate(rs):
        y = 24 + i * (bh + gap)
        label = f"{r['skill']} {r['version']} · {r['model']} ({r['runs']})"
        bw = (w - lw - 60) * r["pass"]
        color = GREEN if r["pass"] >= 0.999 else RED
        p.append(f'<text x="{lw-8}" y="{y+14}" text-anchor="end" '
                 f'font-size="11" fill="{INK}">{label[:52]}</text>')
        p.append(f'<rect x="{lw}" y="{y}" width="{w-lw-60}" height="{bh-4}" '
                 f'rx="2" fill="{GRID}"/>')
        p.append(f'<rect x="{lw}" y="{y}" width="{bw:.0f}" height="{bh-4}" '
                 f'rx="2" fill="{color}"/>')
        p.append(f'<text x="{lw+(w-lw-60)+6}" y="{y+13}" font-size="11" '
                 f'fill="{MUTED}">{r["pass"]:.0%}</text>')
    p.append("</svg>")
    (HERE / "matrix.svg").write_text("".join(p))


def update_readme(rs):
    """Rewrite the skills table in README.md between SKILLS markers —
    generated, so it can never drift from reality."""
    import re
    import subprocess
    root = HERE.parent
    skills = sorted(p.parent.name for p in root.glob("*/SKILL.md"))
    latest = {}
    for r in rs:
        latest.setdefault(r["skill"], r)
    lines = ["| skill | version | tests | last change | description |",
             "|---|---|---|---|---|"]
    for s in skills:
        ver = (root / s / "VERSION")
        ver = ver.read_text().strip() if ver.exists() else "—"
        try:
            changed = subprocess.run(
                ["git", "-C", str(root), "log", "-1", "--format=%as", "--", s],
                capture_output=True, text=True, timeout=10).stdout.strip() or "—"
        except Exception:
            changed = "—"
        ntests = "—"
        led = HERE / f"{s}.jsonl"
        if led.exists():
            try:
                last = json.loads(led.read_text().splitlines()[-1])
                ntests = str(len(last.get("results", {})))
            except (json.JSONDecodeError, IndexError):
                pass
        desc = "—"
        sk = (root / s / "SKILL.md").read_text()
        m = re.search(r"^description:\s*(.+)$", sk, re.M)
        if m:
            desc = m.group(1).strip().split(". ")[0][:110]
        lines.append(f"| **{s}** | {ver} | {ntests} | {changed} | {desc} |")
    block = ("<!-- SKILLS:START (auto-generated by telemetry/render_matrix.py"
             " — do not edit by hand) -->\n## Skills\n\n"
             + "\n".join(lines)
             + "\n\n<!-- SKILLS:END -->")
    readme = root / "README.md"
    t = readme.read_text()
    if "<!-- SKILLS:START" in t:
        t = re.sub(r"<!-- SKILLS:START.*?<!-- SKILLS:END -->", block,
                   t, flags=re.S)
    else:
        t = t.rstrip() + "\n\n" + block + "\n"
    readme.write_text(t)


if __name__ == "__main__":
    rs = rows()
    write_md(rs)
    write_svg(rs)
    update_readme(rs)
    print(f"rendered matrix.md + matrix.svg ({len(rs)} rows) + README skills table")
