# Ledger schema notes

Format: **JSONL** (one JSON object per line), one file per `(user, machine)` in
`$LLM_TOKEN_LEDGER_DIR` (default `~/.llm_token_ledger/`). Append-only.

Why JSONL and not CSV or a JSON array:

- **Append safety.** One line per call is a single small write, so many
  processes across many repos append concurrently without locking. A JSON
  *array* would require read-parse-rewrite per call (O(n), and a crash
  mid-write destroys the file).
- **Schema evolution.** Fields have been added over time; rows written before a
  field existed simply lack the key, and readers cope. CSV would need a fixed
  header plus migration of every historical file.
- **Size is a non-issue.** ~271 bytes/event raw; gzip is **18.4x** smaller
  because the repeated keys compress away. Measured 2026-07-29: 15,436 events =
  4.19 MB raw, 0.23 MB gzipped. At ~1,700 calls/day that projects to ~168 MB/yr
  raw, ~9 MB/yr gzipped.

Readers: `pandas.read_json(path, lines=True)`, DuckDB `read_json_auto` (reads
`.gz` directly), or `jq`.

## Field history — WHICH FIELDS EXIST WHEN

This section exists because on 2026-07-29 the question "is `content_seen`
populated in this run?" was the difference between re-scoring historical results
for free and re-running them for money. Absence of a field is information; record
when each one arrived.

| field | since | notes |
|---|---|---|
| `ts` | v1 | ISO-8601. **Mixed tz-awareness**: this repo's `token_ledger.py` writes an offset (`...-04:00`); some ingest paths wrote naive local time. Readers must normalise (`ts.astimezone()` when `tzinfo is None`) — comparing the two raised `TypeError` and broke `--days` until fixed 2026-07-29. |
| `provider` | v1 | `local`, `claude-code`, or a cloud vendor. |
| `model` | v1 | Raw model id as the caller saw it. Not normalised on write. |
| `project` | v1 | Auto-detected from cwd. |
| `tokens_in` / `tokens_out` | v1 | This repo's `token_ledger.py` writer. |
| `prompt_tokens` / `completion_tokens` | v1 | The skill's own writer. **Both schemas coexist** and the reader normalises them — do not "fix" one into the other; historical rows would change meaning. |
| `reasoning_tokens` | v1 | `null` when the model does not report it. **Never write 0 for "unknown"** — the distinction matters for thinking models. |
| `duration_s` | v1 | Wall time. Absent on ingested subscription rows (transcripts carry no per-call timing). |
| `machine`, `user` | v1 | Also encoded in the filename. |
| `task_tag` | v1 | Optional caller-supplied label. |
| `subscription` | v1 | `true` for flat-fee sources (Claude Code); such rows have no marginal cost and must not be priced per-token. |
| `schema` | v1 | Integer version marker. |

## Deliberately NOT done (revisit dates)

- **Monthly rotation + gzip** — paused 2026-07-29 at ~4 MB/file. Revisit
  2026-01 or when any single file passes ~50 MB.
- **Parquet** — declined 2026-07-29. If ever added it must be a *derived*
  artifact; JSONL stays the source of truth (Parquet is a poor append target).

## Cost fields (design note, not yet implemented)

Savings should be stored as **separate** fields, never a blended figure:

1. `actual_cost` — electricity only, for local runs.
2. `same_model_cloud_cost` — the same model's hosted price. Conservative floor;
   this is the number to show finance.
3. `displaced_model_cost` — requires a per-run `displaced_model` tag naming the
   cloud judge the local run replaced. Larger and defensible *only* because the
   tag is recorded at run time rather than asserted retrospectively.

Never sum 2 and 3. Prices join **as-of** each event's date from
`prices.jsonl`, which is append-only, so old reports stay reproducible.
