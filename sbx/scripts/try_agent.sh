#!/usr/bin/env bash
# Try the example agent by hand (Build Plan 2d), before the runner exists.
# Makes a sandbox, gives the agent one task in it, then destroys the sandbox.
#
#   bash scripts/try_agent.sh "create hello.py that prints hello, and run it"
#
# Run it on the host, or on your computer with the SSH tunnel to the host open.
# Exits with the agent's exit code.
set -uo pipefail

TASK=${1:?usage: try_agent.sh "<task>"}
API=${SANDBOX_API:-http://127.0.0.1:8000}
SBX_DIR=$(cd "$(dirname "$0")/.." && pwd)

# uv run installs anything missing from the venv first, so no separate uv sync is needed
cd "$SBX_DIR"

## CREATE ##

echo "creating a sandbox..."
RESPONSE=$(curl -fsS -X POST "$API/sandboxes") \
  || { echo "could not create a sandbox; is the API running at $API?" >&2; exit 1; }
SANDBOX_ID=$(echo "$RESPONSE" | uv run python -c 'import json, sys; print(json.load(sys.stdin)["id"])')
echo "sandbox $SANDBOX_ID ready"

# destroy the sandbox however the script ends, even on Ctrl+C
destroy() {
  curl -fsS -o /dev/null -X DELETE "$API/sandboxes/$SANDBOX_ID" && echo "sandbox $SANDBOX_ID destroyed"
}
trap destroy EXIT

## RUN THE AGENT ##

# no SANDBOX_HISTORY: a first turn has no earlier events
echo "$TASK" | \
  SANDBOX_ID=$SANDBOX_ID \
  SANDBOX_API=$API \
  uv run python examples/agent/agent.py
CODE=$?

echo "agent exited with $CODE"
exit $CODE
