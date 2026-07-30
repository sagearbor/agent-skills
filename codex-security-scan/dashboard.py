#!/usr/bin/env python3
"""Render a self-contained cross-repo security dashboard.

No external assets, no network, no build step: one HTML file that opens
anywhere. Severity is a *status* scale, so every chip carries an icon and a
text label — colour never carries the meaning alone.
"""
from __future__ import annotations

import datetime
import html
import json

# Status palette (fixed roles, not series colours). Light/dark pairs.
SEV = [
    ("critical", "Critical", "⬤", "#d03b3b", "#d03b3b"),
    ("high",     "High",     "▲", "#ec835a", "#ec835a"),
    ("medium",   "Medium",   "◆", "#fab219", "#fab219"),
    ("low",      "Low",      "▬", "#0ca30c", "#0ca30c"),
    # Upstream's enum spells this "informational"; "info" is normalised to it.
    ("informational", "Info", "○", "#8a8983", "#a8a79f"),
]
ORDER = [s[0] for s in SEV]
META = {s[0]: {"label": s[1], "icon": s[2], "light": s[3], "dark": s[4]} for s in SEV}

CSS = """
*,*::before,*::after{box-sizing:border-box}
.viz-root{
  --surface-0:#f4f4f1; --surface-1:#fcfcfb; --border:#e2e1db;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#77766f;
  --sev-critical:#d03b3b; --sev-high:#ec835a; --sev-medium:#fab219;
  --sev-low:#0ca30c; --sev-informational:#8a8983;
  color-scheme:light; background:var(--surface-0); color:var(--text-primary);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  min-height:100vh; padding:32px 24px 64px;
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
  color-scheme:dark; --surface-0:#121211; --surface-1:#1a1a19; --border:#33322e;
  --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#8f8e85;
  --sev-informational:#a8a79f;}}
:root[data-theme="dark"] .viz-root{
  color-scheme:dark; --surface-0:#121211; --surface-1:#1a1a19; --border:#33322e;
  --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#8f8e85;
  --sev-informational:#a8a79f;}
.wrap{max-width:1120px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--text-secondary);font-size:.875rem;margin:0 0 28px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:28px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:16px 18px}
.tile .n{font-size:2rem;font-weight:650;letter-spacing:-.02em;line-height:1.1}
.tile .k{color:var(--text-secondary);font-size:.8rem;margin-top:2px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:20px}
.card h2{font-size:.95rem;margin:0 0 14px;font-weight:600}
.mix{display:flex;gap:2px;height:26px;border-radius:4px;overflow:hidden;margin-bottom:12px}
.mix i{display:block;transition:opacity .15s}
.mix i:hover{opacity:.78}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:.8rem;color:var(--text-secondary)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.dot{font-size:.7em;line-height:1}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--text-secondary);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;
   cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--text-primary)}
tbody tr:hover{background:color-mix(in srgb,var(--text-primary) 4%,transparent)}
/* Status hue rides the glyph, border and wash only. Label text keeps a text
   token: several status steps are sub-3:1 on the light surface by design. */
.chip{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:999px;
      font-size:.74rem;font-weight:600;white-space:nowrap;
      border:1px solid color-mix(in srgb,currentColor 40%,transparent);
      background:color-mix(in srgb,currentColor 15%,transparent)}
.chip .tx{color:var(--text-primary)}
.bars{display:grid;grid-template-columns:minmax(120px,auto) 1fr auto;gap:8px 12px;align-items:center}
.bars .nm{font-size:.85rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bars .ct{font-size:.8rem;color:var(--text-secondary);font-variant-numeric:tabular-nums}
.row{display:flex;gap:2px;height:14px}
.row i{display:block;border-radius:2px;min-width:3px}
.row i:first-child{border-radius:3px 2px 2px 3px}
.row i:last-child{border-radius:2px 3px 3px 2px}
.clean{color:var(--text-secondary);font-size:.8rem}
.clean b{color:var(--sev-low)}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;align-items:center}
.filters button,.filters select,.filters input{
  font:inherit;font-size:.8rem;padding:5px 11px;border-radius:7px;
  border:1px solid var(--border);background:var(--surface-0);color:var(--text-primary)}
.filters button{cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.filters button .tx{color:var(--text-primary)}
.filters button[aria-pressed="true"]{border-color:currentColor;
  background:color-mix(in srgb,currentColor 15%,transparent)}
.filters input{flex:1;min-width:160px}
.path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem;color:var(--text-secondary)}
.muted{color:var(--text-muted)}
details.f summary{cursor:pointer;font-weight:600;list-style:none}
details.f summary::-webkit-details-marker{display:none}
details.f summary::before{content:"\\25b8 ";color:var(--text-muted)}
details.f[open] summary::before{content:"\\25be "}
details.f p{margin:8px 0 0;color:var(--text-secondary);font-size:.83rem;max-width:78ch}
.empty{color:var(--text-secondary);text-align:center;padding:32px 0;font-size:.9rem}
"""

JS = """
const rows=[...document.querySelectorAll('#ftbl tbody tr')];
const sevSel=new Set(), q=document.getElementById('q'), repo=document.getElementById('repo');
function apply(){
  const t=(q.value||'').toLowerCase(), r=repo.value;
  let shown=0;
  rows.forEach(tr=>{
    const ok=(!sevSel.size||sevSel.has(tr.dataset.sev))
      &&(!r||tr.dataset.repo===r)
      &&(!t||tr.textContent.toLowerCase().includes(t));
    tr.hidden=!ok; if(ok)shown++;
  });
  document.getElementById('count').textContent=shown+' of '+rows.length+' findings';
  document.getElementById('none').hidden=shown>0;
}
document.querySelectorAll('.sevf').forEach(b=>b.onclick=()=>{
  const s=b.dataset.sev;
  sevSel.has(s)?sevSel.delete(s):sevSel.add(s);
  b.setAttribute('aria-pressed',sevSel.has(s));apply();});
q.oninput=apply; repo.onchange=apply;
const RANK={critical:0,high:1,medium:2,low:3,informational:4};
document.querySelectorAll('#ftbl th[data-k]').forEach(th=>{
  th.onclick=()=>{
    const k=th.dataset.k, tb=document.querySelector('#ftbl tbody');
    const dir=th.dataset.dir==='a'?-1:1; th.dataset.dir=dir===1?'a':'d';
    [...tb.querySelectorAll('tr')].sort((x,y)=>{
      const a=k==='sev'?RANK[x.dataset.sev]:(x.dataset[k]||'').toLowerCase();
      const b=k==='sev'?RANK[y.dataset.sev]:(y.dataset[k]||'').toLowerCase();
      return a<b?-dir:a>b?dir:0;}).forEach(tr=>tb.appendChild(tr));};
});
apply();
"""


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def render(data: dict, title: str = "Security posture") -> str:
    findings = data.get("findings", [])
    repos = data.get("repositories", [])
    for f in findings:
        sev = f.get("severity")
        lvl = (sev.get("level") if isinstance(sev, dict) else sev) or "informational"
        lvl = {"info": "informational"}.get(str(lvl).lower(), str(lvl).lower())
        f["_sev"] = lvl if lvl in META else "informational"

    open_f = [f for f in findings if (f.get("status") or "open") == "open"]
    totals = {s: sum(1 for f in open_f if f["_sev"] == s) for s in ORDER}
    total = len(open_f)
    urgent = totals["critical"] + totals["high"]

    by_repo: dict[str, dict] = {}
    for r in repos:
        by_repo[r.get("displayName") or "unknown"] = {s: 0 for s in ORDER}
    for f in open_f:
        by_repo.setdefault(f.get("repository") or "unknown", {s: 0 for s in ORDER})
        by_repo[f["repository"]][f["_sev"]] += 1
    clean = sum(1 for c in by_repo.values() if not sum(c.values()))
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    def chip(sev: str) -> str:
        m = META[sev]
        return (f'<span class="chip" style="color:var(--sev-{sev})">'
                f'<span class="dot">{m["icon"]}</span>'
                f'<span class="tx">{m["label"]}</span></span>')

    # severity mix strip (2px gaps come from flex gap on .mix)
    mix = "".join(
        f'<i style="flex:{totals[s]};background:var(--sev-{s})" '
        f'title="{META[s]["label"]}: {totals[s]}"></i>'
        for s in ORDER if totals[s]) or '<i style="flex:1;background:var(--border)"></i>'
    legend = "".join(
        f'<span style="color:var(--sev-{s})"><span class="dot">{META[s]["icon"]}</span>'
        f'<span style="color:var(--text-secondary)">{META[s]["label"]} {totals[s]}</span></span>'
        for s in ORDER)

    worst = max((sum(c.values()) for c in by_repo.values()), default=0) or 1
    # Rank by worst-severity-first, then volume, then name: one critical
    # outranks any number of lows.
    weight = {"critical": 10_000, "high": 500, "medium": 25, "low": 2, "informational": 1}
    def risk(kv):
        name, c = kv
        return (-sum(weight[s] * n for s, n in c.items()), -sum(c.values()), name)

    bars = []
    for name, c in sorted(by_repo.items(), key=risk):
        n = sum(c.values())
        seg = "".join(
            f'<i style="flex:{c[s]};background:var(--sev-{s})" '
            f'title="{_esc(name)} — {META[s]["label"]}: {c[s]}"></i>'
            for s in ORDER if c[s])
        width = max(4, round(100 * n / worst))
        bar = (f'<div class="row" style="width:{width}%">{seg}</div>' if n
               else '<span class="clean"><b>\u2713</b> no open findings</span>')
        bars.append(f'<div class="nm" title="{_esc(name)}">{_esc(name)}</div>'
                    f'<div>{bar}</div><div class="ct">{n or ""}</div>')

    opts = "".join(f'<option value="{_esc(r)}">{_esc(r)}</option>'
                   for r in sorted(by_repo))
    sevbtns = "".join(
        f'<button class="sevf" data-sev="{s}" aria-pressed="false" '
        f'style="color:var(--sev-{s})"><span class="dot">{META[s]["icon"]}</span>'
        f'<span class="tx">{META[s]["label"]}</span></button>' for s in ORDER if totals[s])

    trs = []
    for f in sorted(open_f, key=lambda x: (ORDER.index(x["_sev"]),
                                           x.get("repository") or "")):
        cwe = ", ".join(f.get("cwe") or [])
        loc = f.get("locationPath") or ""
        if f.get("line"):
            loc += f":{f['line']}"
        body = _esc(f.get("summary"))
        if f.get("remediation"):
            body += f'</p><p><strong>Fix:</strong> {_esc(f["remediation"])}'
        trs.append(
            f'<tr data-sev="{f["_sev"]}" data-repo="{_esc(f.get("repository"))}" '
            f'data-title="{_esc(f.get("title"))}">'
            f'<td>{chip(f["_sev"])}</td>'
            f'<td>{_esc(f.get("repository"))}</td>'
            f'<td><details class="f"><summary>{_esc(f.get("title"))}</summary>'
            f'<p>{body}</p></details></td>'
            f'<td class="path">{_esc(loc)}</td>'
            f'<td class="muted">{_esc(cwe)}</td></tr>')

    table = f"""<table id="ftbl"><thead><tr>
      <th data-k="sev">Severity</th><th data-k="repo">Repository</th>
      <th data-k="title">Finding</th><th>Location</th><th>CWE</th>
      </tr></thead><tbody>{"".join(trs)}</tbody></table>
      <div class="empty" id="none" hidden>No findings match these filters.</div>"""

    return f"""<div class="viz-root"><div class="wrap">
<h1>{_esc(title)}</h1>
<p class="sub">{len(by_repo)} repositories &middot; generated {stamp} &middot;
  scanned locally with the <code>codex-security-scan</code> skill &mdash; no code left this machine</p>

<div class="tiles">
  <div class="tile"><div class="n">{len(by_repo)}</div><div class="k">Repositories scanned</div></div>
  <div class="tile"><div class="n">{total}</div><div class="k">Open findings</div></div>
  <div class="tile"><div class="n">{urgent}</div>
    <div class="k">Critical or high</div></div>
  <div class="tile"><div class="n">{clean}</div>
    <div class="k">Repositories with none</div></div>
</div>

<div class="card"><h2>Severity mix</h2>
  <div class="mix">{mix}</div><div class="legend">{legend}</div></div>

<div class="card"><h2>Findings by repository</h2>
  <div class="bars">{"".join(bars)}</div></div>

<div class="card"><h2>All findings</h2>
  <div class="filters">{sevbtns}
    <select id="repo"><option value="">All repositories</option>{opts}</select>
    <input id="q" type="search" placeholder="Search findings…" aria-label="Search findings">
    <span class="ct muted" id="count"></span>
  </div>
  {table}
</div>
</div></div>
<style>{CSS}</style>
<script>{JS}</script>"""


def page(data: dict, title: str = "Security posture") -> str:
    """Standalone document — render() alone is a fragment (no charset/doctype)."""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title>'
            f'<style>html,body{{margin:0;padding:0}}</style></head><body>'
            f'{render(data, title)}</body></html>')


if __name__ == "__main__":
    import sys
    print(page(json.load(open(sys.argv[1])),
               sys.argv[2] if len(sys.argv) > 2 else "Security posture"))
