# Skill regression matrix

Auto-rendered from `telemetry/*.jsonl` (append-only run ledgers; each skill's `run_tests.py --auto --model <id>` self-caps at 8 runs per model+version). Regenerate: `python3 telemetry/render_matrix.py`.

![pass-rate matrix](matrix.svg)

| skill | version | model | runs | pass | duration | last run |
|---|---|---|---|---|---|---|
| md-convert | 2026-07-25.1 | claude-fable-5 | 2 | 100% | 0.76±0.27s | 2026-07-25 |
| meeting-canvas | 2026-07-26.1 | claude-fable-5 | 1 | 100% | 0.00±0.00s | 2026-07-26 |
