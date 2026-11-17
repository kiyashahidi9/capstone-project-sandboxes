#!/usr/bin/env bash
# Try the example agent by hand (Build Plan 2d), without the runner.
# Makes a sandbox, gives the agent one task in it, then destroys the sandbox.
#
#   bash scripts/try_agent.sh "create hello.py that prints hello, and run it"
#
# Run it on your computer with the SSH tunnel to the host open and SANDBOX_ADMIN_KEY in .env.
# The agent runs here, not locked down: this tests the agent, not the runner.
# Exits with the agent's exit code.
set -uo pipefail

TASK=${1:?usage: try_agent.sh "<task>"}
API=${SANDBOX_API:-http://127.0.0.1:8000}
SBX_DIR=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE=$(cd "$SBX_DIR/.." && pwd)/.env

# the admin key: from the environment, or from .env
KEY=${SANDBOX_ADMIN_KEY:-$(grep -E '^SANDBOX_ADMIN_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)}
[[ -n $KEY ]] || { echo "SANDBOX_ADMIN_KEY is not set; add it to $ENV_FILE" >&2; exit 1; }

# uv run installs anything missing from the venv first, so no separate uv sync is needed
cd "$SBX_DIR"

## CREATE ##

echo "creating a sandbox..."
RESPONSE=$(curl -fsS -H "Authorization: Bearer $KEY" -X POST "$API/sandboxes") \
  || { echo "could not create a sandbox; is the API running at $API?" >&2; exit 1; }
SANDBOX_ID=$(echo "$RESPONSE" | uv run python -c 'import json, sys; print(json.load(sys.stdin)["id"])')
echo "sandbox $SANDBOX_ID ready"

# destroy the sandbox however the script ends, even on Ctrl+C
destroy() {
  curl -fsS -o /dev/null -H "Authorization: Bearer $KEY" -X DELETE "$API/sandboxes/$SANDBOX_ID" \
    && echo "sandbox $SANDBOX_ID destroyed"
}
trap destroy EXIT

## RUN THE AGENT ##

# no SANDBOX_HISTORY: a first turn has no earlier events
# no runner here to make a turn token, so the agent uses the admin key
echo "$TASK" | \
  SANDBOX_ID=$SANDBOX_ID \
  SANDBOX_API=$API \
  SANDBOX_TOKEN=$KEY \
  uv run python examples/agent/agent.py
CODE=$?

echo "agent exited with $CODE"
exit $CODE
