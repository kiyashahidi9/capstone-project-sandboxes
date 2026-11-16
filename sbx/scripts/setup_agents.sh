#!/usr/bin/env bash
# setup_agents.sh - lock the client's agent down on the host (Build Plan 2f)
#
# Run from sbx/, after setup_host.sh:   sudo scripts/setup_agents.sh
# Safe to rerun. Rerun it after setup_host.sh (which hands .env back to you) and after changing the agent.
#
# Steps:
#   1. User sbx-agent: the agent runs as it, never as root
#   2. /opt/agents: the example agent and the guardrail check, owned by root, readable by sbx-agent
#   3. /opt/agents/.venv on the system python3, with openai and the SDK
#      (the repo's .venv uses uv's Python under /home, which the locked-down agent can't see)
#   4. /var/lib/sbx-turns: history and env files for each turn
#   5. .env: root only
#   6. Checklist
set -Eeuo pipefail

SCRIPTS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)   # the folder holding this script
SBX_DIR=$(cd "$SCRIPTS/.." && pwd)
REPO=$(cd "$SBX_DIR/.." && pwd)
ENV_FILE=$REPO/.env
AGENT_USER=sbx-agent
AGENTS_DIR=/opt/agents
TURNS_DIR=/var/lib/sbx-turns
REAL_USER=${SUDO_USER:-$(id -un)}

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run with sudo: sudo scripts/setup_agents.sh"
command -v systemd-run >/dev/null || die "systemd-run not found; this needs a systemd host"

# uv, as setup_host.sh installed it
USER_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
UV=$(command -v uv || true)
[[ -n $UV ]] || UV=$USER_HOME/.local/bin/uv
[[ -x $UV ]] || die "uv not found; run setup_host.sh first"

## 1. USER ##

log "1. user $AGENT_USER"
id "$AGENT_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$AGENT_USER"

## 2. AGENTS ##

log "2. agents -> $AGENTS_DIR"
install -d -o root -g "$AGENT_USER" -m 0750 "$AGENTS_DIR"
install -o root -g "$AGENT_USER" -m 0640 \
  "$SBX_DIR/examples/agent/agent.py" \
  "$SBX_DIR/examples/agent/tools.py" \
  "$SBX_DIR/tests/guardrail_agent.py" \
  "$AGENTS_DIR/"

## 3. AGENT VENV ##

log "3. agent venv -> $AGENTS_DIR/.venv"
export UV_PYTHON_DOWNLOADS=never   # the system python3, never one under a home folder
[[ -x $AGENTS_DIR/.venv/bin/python ]] || "$UV" venv -q --python /usr/bin/python3 "$AGENTS_DIR/.venv"
# a normal (not editable) install copies the SDK into the venv
"$UV" pip install -q --python "$AGENTS_DIR/.venv/bin/python" --reinstall-package persistent-sandboxes "$SBX_DIR"
chmod -R go-w,a+rX "$AGENTS_DIR/.venv"

## 4. TURN FILES ##

log "4. turn files -> $TURNS_DIR"
# 0710: sbx-agent can open a file it's told about, but can't list the folder
install -d -o root -g "$AGENT_USER" -m 0710 "$TURNS_DIR"

## 5. SECRETS ##

log "5. $ENV_FILE root only"
if [[ -f $ENV_FILE ]]; then
  chown root:root "$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  echo "    no .env yet: cp .env.example .env, fill it in, then rerun this script"
fi

## 6. CHECKLIST ##

log "6. checklist"
FAILS=0
check() { local d=$1; shift
  if "$@" &>/dev/null; then echo "  [x] $d"; else echo "  [ ] $d"; FAILS=$((FAILS + 1)); fi; }
check "user $AGENT_USER exists"                id "$AGENT_USER"
check "agent installed in $AGENTS_DIR"         test -s "$AGENTS_DIR/agent.py"
check "$AGENT_USER can import openai and sdk"  sudo -u "$AGENT_USER" "$AGENTS_DIR/.venv/bin/python" -c "import openai, sdk.sandbox"
check ".env is root only (600)"                test "$(stat -c %a:%U "$ENV_FILE")" = 600:root
check "$AGENT_USER can't read .env"            bash -c "! sudo -u $AGENT_USER cat '$ENV_FILE'"
check "systemd-run starts $AGENT_USER"         bash -c "[[ \$(systemd-run --wait --pipe --quiet --collect --uid=$AGENT_USER -p ProtectHome=yes whoami) == $AGENT_USER ]]"
(( FAILS == 0 )) || die "$FAILS check(s) failed"

# only ask for an edit if .env doesn't already point at the locked-down agent
WANTED_CMD="$AGENTS_DIR/.venv/bin/python $AGENTS_DIR/agent.py"
CURRENT_CMD=$(grep -E '^AGENT_CMD=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)

echo
echo "Agents locked down."
if [[ $CURRENT_CMD != "$WANTED_CMD" ]]; then
  echo "Change AGENT_CMD in .env (sudo nano $ENV_FILE) to:"
  echo "  AGENT_CMD=$WANTED_CMD"
fi
echo "Restart the API server if it's running. To edit .env later, use sudo: it's root only now."
