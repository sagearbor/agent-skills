#!/usr/bin/env bash
# sage-agent-tempo: Claude Code Stop hook
# Triggers the JSONL parser after each agent session ends.
#
# Install in .claude/settings.json:
# {
#   "hooks": {
#     "Stop": [{
#       "hooks": [{
#         "type": "command",
#         "command": "path/to/stop-hook.sh"
#       }]
#     }]
#   }
# }
#
# Reads JSON from stdin: { "session_id": "...", "transcript_path": "...", "cwd": "..." }

set -euo pipefail

# Read hook input from stdin
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | node -e "process.stdin.on('data',d=>{try{console.log(JSON.parse(d).session_id||'')}catch{console.log('')}})" 2>/dev/null || echo "")
CWD=$(echo "$INPUT" | node -e "process.stdin.on('data',d=>{try{console.log(JSON.parse(d).cwd||'')}catch{console.log('')}})" 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ] || [ -z "$CWD" ]; then
  exit 0
fi

# Check if sage-agent-tempo is available
if ! command -v sage-agent-tempo &>/dev/null; then
  # Try npx as fallback
  if [ -f "$CWD/node_modules/.bin/sage-agent-tempo" ]; then
    TEMPO="$CWD/node_modules/.bin/sage-agent-tempo"
  else
    exit 0
  fi
else
  TEMPO="sage-agent-tempo"
fi

# Check if project has a checklist
if [ ! -f "$CWD/developer_checklist.yaml" ]; then
  exit 0
fi

# Run parser (async, don't block Claude Code)
cd "$CWD"
$TEMPO parse --agent claude-code --checklist developer_checklist.yaml &>/dev/null &

exit 0
