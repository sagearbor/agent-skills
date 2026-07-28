# Global skills (~/.claude/skills)

[![skill-tests](https://github.com/sagearbor/agent-skills/actions/workflows/skill-tests.yml/badge.svg)](https://github.com/sagearbor/agent-skills/actions/workflows/skill-tests.yml) · [**live regression matrix**](telemetry/matrix.md)

Version-controlled since 2026-07-25 so skill edits across model generations
are tracked (git log answers "what changed when model X landed").

## Telemetry convention (adopt in every new skill)

Each skill ships `tests/run_tests.py` with:
- deterministic fixture tests (stdlib-only where possible),
- `--model <id> [--auto]`: run + append one JSON line to
  `~/.claude/skills/telemetry/<skill>.jsonl`; `--auto` self-limits to 8
  recorded runs per (model, skill version) then becomes a no-op,
- `--report`: model x skill-version matrix (runs, pass rate, duration mean+/-SD).

SKILL.md rule 0 tells Claude to call `--auto` once per session with its own
model id. Result: as new models are used, the regression matrix fills itself.
The telemetry/ ledger directory is committed too — it IS the data.
