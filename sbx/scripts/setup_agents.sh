#!/usr/bin/env bash
# setup_agents.sh - lock the client's agent down on the host (Build Plan 2f)
#
# Run from sbx/, after setup_host.sh:   sudo scripts/setup_agents.sh
# Safe to rerun. Rerun it after setup_host.sh (which hands .env back to you) and after changing the agent.
#
# Install a client's agent (a folder already on this host), never during a running turn:
#   sudo AGENT_SRC=/path/to/my-bot AGENT_RUN="python -m mybot" scripts/setup_agents.sh
#   AGENT_RUN is how to start it from inside its folder (default "python agent.py").
#
# Steps:
#   1. User sbx-agent: the agent runs as it, never as root
#   2. /opt/agents: the example agent and the guardrail check, owned by root, readable by sbx-agent
#      (+ the client's folder in /opt/agents/<name>)
#   3. /opt/agents/.venv on the system python3, with openai and the SDK
#      (the repo's .venv uses uv's Python under /home, which the locked-down agent can't see)
#      (+ rebuilt from scratch with the client's requirements.txt)
#   4. /var/lib/sbx-turns: history and env files for each turn
#   5. Secrets: .env and every agent's secrets.env root only (+ AGENT_DIR and AGENT_CMD for the client's agent)
#      an OpenAI key an older install left in .env moves to the example agent's secrets.env
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
AGENT_SRC=${AGENT_SRC:-}                       # a client's agent folder; empty installs only the example
AGENT_RUN=${AGENT_RUN:-python agent.py}        # how to start it, from inside its folder
AGENT_NAME=${AGENT_SRC:+$(basename "$AGENT_SRC")}
CLIENT_AGENT_DIR=$AGENTS_DIR/$AGENT_NAME
VENV_PYTHON=$AGENTS_DIR/.venv/bin/python
SECRETS_FILE=secrets.env                       # an agent's provider keys; only the API's key proxy reads it
OLD_KEY_NAME=OPENAI_API_KEY                    # older installs kept this in .env

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

# Stops before anything changes if the client's agent can't be installed as asked.
check_client_agent() {
  [[ -d $AGENT_SRC ]] || die "AGENT_SRC $AGENT_SRC is not a folder"
  [[ $AGENT_NAME =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "agent folder name must be letters, digits, . _ or -: $AGENT_NAME"
  [[ -f $ENV_FILE ]] || die "no .env yet: write it first (install.sh does this)"
  check_agent_run
}

# Stops unless AGENT_RUN starts with python and every .py file it names is in the agent's folder.
check_agent_run() {
  local words word
  read -ra words <<< "$AGENT_RUN"
  [[ ${words[0]} =~ ^python3?$ ]] || die "AGENT_RUN must start with python or python3 (Python agents only for now)"
  for word in "${words[@]:1}"; do
    [[ $word != *.py || -f $AGENT_SRC/$word ]] || die "$word not found in $AGENT_SRC"
  done
}

# Copies the client's agent folder in, readable but not changeable by the agent.
install_client_agent() {
  rm -rf "$CLIENT_AGENT_DIR"
  cp -r "$AGENT_SRC" "$CLIENT_AGENT_DIR"
  chown -R root:"$AGENT_USER" "$CLIENT_AGENT_DIR"
  chmod -R u=rwX,g=rX,o= "$CLIENT_AGENT_DIR"
}

# Makes an agent folder's secrets.env readable by root only, so the agent itself can never read its keys.
lock_secrets_file() {
  local file=$1/$SECRETS_FILE
  [[ -f $file ]] || return 0
  chown root:root "$file"
  chmod 600 "$file"
}

# Gives the value of one NAME=value line in .env, or nothing.
env_value() {
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true
}

# Removes one NAME=value line from .env, if it's there.
remove_env_var() {
  local updated
  updated=$(grep -vE "^$1=" "$ENV_FILE" || true)
  printf '%s\n' "$updated" > "$ENV_FILE"
}

# Moves the OpenAI key an older install kept in .env into the example agent's secrets.env, root only.
move_old_key_out_of_env() {
  local key example_secrets=$AGENTS_DIR/$SECRETS_FILE
  key=$(env_value "$OLD_KEY_NAME")
  if [[ -n $key && ! -f $example_secrets ]]; then
    (umask 077 && printf '%s=%s\n' "$OLD_KEY_NAME" "$key" > "$example_secrets")
  fi
  remove_env_var "$OLD_KEY_NAME"
}

# Gives the folder of the agent the runner starts: the client's, or the example agent's.
active_agent_dir() {
  local dir
  dir=$(env_value AGENT_DIR)
  echo "${dir:-$AGENTS_DIR}"
}

# Installs the libraries the client's agent lists in requirements.txt, if it has one.
install_agent_requirements() {
  local requirements=$CLIENT_AGENT_DIR/requirements.txt
  [[ -f $requirements ]] || return 0
  "$UV" pip install -q --python "$VENV_PYTHON" -r "$requirements"
}

# Gives AGENT_RUN with its leading python swapped for the agent venv's python.
venv_command() {
  local program rest
  read -r program rest <<< "$AGENT_RUN"
  echo "$VENV_PYTHON${rest:+ $rest}"
}

# Sets one NAME=value line in .env, replacing the old line or adding a new one.
set_env_var() {
  local name=$1 value=$2 updated
  updated=$(awk -v name="$name" -v value="$value" '
    index($0, name "=") == 1 { print name "=" value; found = 1; next }
    { print }
    END { if (!found) print name "=" value }
  ' "$ENV_FILE")
  printf '%s\n' "$updated" > "$ENV_FILE"
}

[[ $EUID -eq 0 ]] || die "run with sudo: sudo scripts/setup_agents.sh"
command -v systemd-run >/dev/null || die "systemd-run not found; this needs a systemd host"

# uv, as setup_host.sh installed it
USER_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
UV=$(command -v uv || true)
[[ -n $UV ]] || UV=$USER_HOME/.local/bin/uv
[[ -x $UV ]] || die "uv not found; run setup_host.sh first"
if [[ -n $AGENT_SRC ]]; then check_client_agent; fi

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
if [[ -n $AGENT_SRC ]]; then
  install_client_agent
  lock_secrets_file "$CLIENT_AGENT_DIR"   # at once: the copy above left it readable by the agent
fi

## 3. AGENT VENV ##

log "3. agent venv -> $AGENTS_DIR/.venv"
export UV_PYTHON_DOWNLOADS=never   # the system python3, never one under a home folder
if [[ -n $AGENT_SRC ]]; then rm -rf "$AGENTS_DIR/.venv"; fi   # fresh, so an older agent's libraries don't linger
[[ -x $VENV_PYTHON ]] || "$UV" venv -q --python /usr/bin/python3 "$AGENTS_DIR/.venv"
# a normal (not editable) install copies the SDK into the venv
"$UV" pip install -q --python "$VENV_PYTHON" --reinstall-package persistent-sandboxes "$SBX_DIR"
if [[ -n $AGENT_SRC ]]; then install_agent_requirements; fi
chmod -R go-w,a+rX "$AGENTS_DIR/.venv"

## 4. TURN FILES ##

log "4. turn files -> $TURNS_DIR"
# 0710: sbx-agent can open a file it's told about, but can't list the folder
install -d -o root -g "$AGENT_USER" -m 0710 "$TURNS_DIR"

## 5. SECRETS ##

log "5. secrets: $ENV_FILE and every $SECRETS_FILE root only"
if [[ -n $AGENT_SRC ]]; then
  set_env_var AGENT_DIR "$CLIENT_AGENT_DIR"
  set_env_var AGENT_CMD "$(venv_command)"
fi
if [[ -f $ENV_FILE ]]; then
  move_old_key_out_of_env
  chown root:root "$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  echo "    no .env yet: cp .env.example .env, fill it in, then rerun this script"
fi
lock_secrets_file "$AGENTS_DIR"
ACTIVE_SECRETS=$(active_agent_dir)/$SECRETS_FILE

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
check ".env holds no provider key"             bash -c "! grep -qE '^$OLD_KEY_NAME=' '$ENV_FILE'"
if [[ -f $ACTIVE_SECRETS ]]; then
  check "$SECRETS_FILE is root only (600)"     test "$(stat -c %a:%U "$ACTIVE_SECRETS")" = 600:root
  check "$AGENT_USER can't read $SECRETS_FILE" bash -c "! sudo -u $AGENT_USER cat '$ACTIVE_SECRETS'"
else
  echo "  note: no $ACTIVE_SECRETS yet; the agent's model calls are refused until it has one"
fi
check "systemd-run starts $AGENT_USER"         bash -c "[[ \$(systemd-run --wait --pipe --quiet --collect --uid=$AGENT_USER -p ProtectHome=yes whoami) == $AGENT_USER ]]"
if [[ -n $AGENT_SRC ]]; then
  check "$AGENT_USER can read $CLIENT_AGENT_DIR" sudo -u "$AGENT_USER" ls "$CLIENT_AGENT_DIR"
fi
(( FAILS == 0 )) || die "$FAILS check(s) failed"

# only ask for an edit if .env doesn't already run an agent with the locked-down venv's python
WANTED_CMD="$VENV_PYTHON $AGENTS_DIR/agent.py"
CURRENT_CMD=$(grep -E '^AGENT_CMD=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)

echo
echo "Agents locked down."
if [[ -n $AGENT_SRC ]]; then echo "Installed $AGENT_NAME: AGENT_CMD=$CURRENT_CMD"; fi
if [[ $CURRENT_CMD != "$VENV_PYTHON "* ]]; then
  echo "Change AGENT_CMD in .env (sudo nano $ENV_FILE) to:"
  echo "  AGENT_CMD=$WANTED_CMD"
fi
echo "Restart the API server if it's running. To edit .env later, use sudo: it's root only now."
