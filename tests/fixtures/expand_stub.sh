#!/usr/bin/env bash
# tests/fixtures/expand_stub.sh — a fake "claude -p --output-format json" for
# testing POST /api/expand without shelling out to the real claude CLI.
#
# Reads stdin (the prompt — ignored, this is a canned response) and prints a
# claude -p JSON envelope on stdout, same shape the real `claude -p
# --output-format json` produces: a top-level object whose "result" field
# carries the actual answer text (here, a fenced JSON blob, to also exercise
# the code-fence-tolerant parser).
#
# Behavior is switched by $EXPAND_STUB_MODE (defaults to "ok"):
#   ok       - print a valid envelope+fields for either kind
#   fail     - exit 1 (simulates the expander erring out)
#   timeout  - sleep past any reasonable test timeout
#   badjson  - print an envelope whose "result" isn't valid JSON
#
# Which kind (edge/agent) to answer as is sniffed from the prompt text on
# stdin, since the stub has no other way to know what was asked.
set -euo pipefail

mode="${EXPAND_STUB_MODE:-ok}"
prompt="$(cat)"

case "$mode" in
  fail)
    echo "stub: simulated expander failure" >&2
    exit 1
    ;;
  timeout)
    sleep 30
    exit 0
    ;;
esac

if echo "$prompt" | head -1 | grep -q "^AGENT-DESCRIBE"; then
  fields='{"name": "stubagent", "role": "stub role from fixture", "identity": "stub identity from fixture"}'
else
  fields='{"label": "stub label", "conditions": ["when stub fires"], "target_action": "stub action", "reply_expected": true, "back_conditions": [], "back_action": "", "back_reply": false, "directed": false, "max_turns": 5, "token_cap": 0, "cost_cap": 0}'
fi

if [ "$mode" = "badjson" ]; then
  result="\`\`\`json
not actually json
\`\`\`"
else
  result="\`\`\`json
${fields}
\`\`\`"
fi

python3 - "$result" <<'PY'
import json, sys
print(json.dumps({"type": "result", "subtype": "success", "result": sys.argv[1]}))
PY
