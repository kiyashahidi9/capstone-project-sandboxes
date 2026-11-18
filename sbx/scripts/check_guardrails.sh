#!/usr/bin/env bash
# check_guardrails.sh - run the 2f guardrail checks through a real, locked-down turn
#
# 1. Start the API with the guardrail agent instead of the example agent (from sbx/):
#      sudo env AGENT_CMD="/opt/agents/.venv/bin/python /opt/agents/guardrail_agent.py" \
#        .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
# 2. In another terminal on the host (from sbx/):
#      sudo bash scripts/check_guardrails.sh
#
# Makes two sandboxes, asks the agent in the first to attack the second, the API and the host,
# prints each check, and destroys both sandboxes. Exits 0 only if every check passes.
set -uo pipefail

SBX_DIR=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE=$(cd "$SBX_DIR/.." && pwd)/.env
API=http://127.0.0.1:8000
PYTHON=$SBX_DIR/.venv/bin/python

[[ -r $ENV_FILE ]] || { echo "can't read $ENV_FILE; run with sudo" >&2; exit 1; }
KEY=$(grep -E '^SANDBOX_ADMIN_KEY=' "$ENV_FILE" | cut -d= -f2-)
[[ -n $KEY ]] || { echo "SANDBOX_ADMIN_KEY is not set in $ENV_FILE" >&2; exit 1; }

api() { curl -fsS -H "Authorization: Bearer $KEY" "$@"; }
json() { "$PYTHON" -c "import json, sys; print(json.load(sys.stdin)$1)"; }

## TWO SANDBOXES ##

echo "creating two sandboxes..."
OWN=$(api -X POST "$API/sandboxes" | json '["id"]') || { echo "is the API running at $API?" >&2; exit 1; }
OTHER=$(api -X POST "$API/sandboxes" | json '["id"]') || { api -X DELETE "$API/sandboxes/$OWN" >/dev/null; exit 1; }

destroy() {
  api -o /dev/null -X DELETE "$API/sandboxes/$OWN"
  api -o /dev/null -X DELETE "$API/sandboxes/$OTHER"
  echo "sandboxes destroyed"
}
trap destroy EXIT

## ONE TURN ##

# the agent's message: the sandbox to attack and the file to try to read
BODY=$("$PYTHON" -c 'import json, sys; print(json.dumps({"text": sys.argv[1] + " " + sys.argv[2]}))' "$OTHER" "$ENV_FILE")
api -o /dev/null -X POST -H "Content-Type: application/json" -d "$BODY" "$API/sandboxes/$OWN/messages"

# show what the agent prints until the turn ends
LAST=0
while true; do
  EVENTS=$(api "$API/sandboxes/$OWN/events?after=$LAST")
  while IFS=$'\t' read -r SEQ TYPE TEXT; do
    [[ -z $SEQ ]] && continue
    LAST=$SEQ
    case $TYPE in
      agent_output)  echo "$TEXT" ;;
      turn_finished) echo "all checks passed"; exit 0 ;;
      turn_failed)   echo "FAILED: $TEXT"; exit 1 ;;
    esac
  done < <(echo "$EVENTS" | "$PYTHON" -c '
import json, sys
for event in json.load(sys.stdin)["events"]:
    data = event["data"]
    text = data.get("text") or json.dumps(data)
    print(event["seq"], event["type"], text.replace("\t", " "), sep="\t")
')
  sleep 1
done
