---
name: md-convert
description: Convert a Markdown file to .docx, .html, .pdf, or .xlsx (pandoc-based, with optional generated-datetime stamping). Use whenever the user asks to convert/export markdown to another document format in ANY repo.
---

# md-convert

Convert Markdown to shareable formats:

```
python3 ~/.claude/skills/md-convert/convert.py INPUT.md --to docx|html|pdf|xlsx [--out PATH] [--stamp|--no-stamp] [--toc]
```

## --toc (docx only): Word-native hyperlinked table of contents

Adds a real Word TOC (proven on the llm-as-judge manuscript review loop,
2026-07): every Heading 1–3 gets a `_Toc` bookmark, entries are TOC-styled
paragraphs hyperlinked to them (Ctrl/Cmd+click jumps to the section),
pre-cached so the TOC is visible immediately on open, all inside a live TOC
field (right-click → Update Field in Word regenerates it and adds page
numbers — build time can't know pagination).

- **No custom markdown needed** — plain `#`/`##`/`###` headings are enough.
  A lone leading `#` is treated as the document title (excluded from the
  TOC); levels are normalized so docs whose sections start at `##` still
  get top-level TOC entries. Code-block `#` lines can't pollute the TOC
  (headings are read from the rendered Heading styles, not the raw text).
- **Self-verifying**: every entry must pair with its own bookmark or the
  conversion fails loudly (also the canary if a pandoc upgrade ever changes
  its docx XML shape, which the post-processing is coupled to).
- Conversions parse md with GitHub-style heading leniency
  (`markdown-blank_before_header`), so headings without a preceding blank
  line still count.

## Behavior rules for Claude

1. **First-use stamp question.** The `--stamp` option prepends a
   "Generated: <datetime>" line (these files get overwritten often, so the
   stamp shows readers which vintage they hold). The default lives in
   `~/.claude/skills/md-convert/config.json` as `{"stamp_default": true|false}`.
   If that file is missing or `stamp_default` is null, **ASK the user** what
   the default should be (Sage has said he'll likely want it ON), write their
   answer to config.json, then proceed. Explicit `--stamp`/`--no-stamp` always
   override the default without asking.
2. **Dependencies.** `pandoc` (installed via brew 2026-07-16) handles
   docx/html/pdf. PDF additionally needs an engine (weasyprint, wkhtmltopdf,
   or a LaTeX) — the script names the brew commands if none is found; ask the
   user before installing one. `xlsx` needs `openpyxl` in the python you run
   the script with — prefer the current repo's venv python if it has it
   (`.venv/bin/python`), else `pip install openpyxl --user`.
3. **xlsx semantics.** Markdown tables become spreadsheet rows (columns
   preserved); non-table lines land in column A. Best for docs that are
   mostly tables; warn the user if the source has none.
4. **Repo-specific converters take precedence** when the user wants their
   styling: e.g. `irb_agent_dcri` has `scripts/md_to_html.py` (interactive
   checklists with tooltips) and `scripts/md_to_docx.py` (mermaid support).
   This skill is the generic cross-repo fallback.
