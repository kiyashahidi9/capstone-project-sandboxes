#!/usr/bin/env bash
# install.sh - set up a sandbox host in your own Google Cloud project (Build Plan 6a)
#
# Runs on YOUR computer, unlike the other scripts here, which run on the host.
# Log in and pick your project first, then:   sbx/scripts/install.sh
# To install your own agent instead of the example:   sbx/scripts/install.sh --agent ~/my-bot [--cmd "python -m mybot"]
#   Put its provider keys in ~/my-bot/secrets.env (docs/agent_contract.md); the agent itself never reads them.
# To replace keys later, without reinstalling or restarting anything:
#   sbx/scripts/install.sh --update-secrets ~/my-bot   (your agent: copies its secrets.env again)
#   sbx/scripts/install.sh --update-secrets            (the example agent: asks for its OpenAI key again)
# Safe to rerun: every step skips what already exists.
#
# The VM gets the commit your copy of the repo is on, so push it first; uncommitted changes aren't installed.
# Everyone who installs logs in to the VM as the same user, sbx, so a rerun by anyone finds the same files.
#
# Steps:
#   1. Preflight: gcloud installed and logged in, a project picked, billing on
#   2. APIs: Compute, Storage, IAM
#   3. Bucket gs://<project>-sbx-snapshots for snapshot copies, never public
#   4. Service account sbx-host
#   5. sbx-host may use that bucket, and nothing else
#   6. VM sbx-node-1, running as sbx-host, with OS Login off
#   7. SSH in as sbx
#   8. The repo at your commit, in /home/sbx/capstone-project-sandboxes
#   9. setup_host.sh
#  10. .env with a new admin key (first install only)
#  11. Your agent's folder, with --agent
#  12. setup_agents.sh
#  13. Agent keys: the example agent's OpenAI key (asked once), or a check that your agent has secrets.env
#  14. The API as a service (scripts/sbx-api.service), restarted only if code, .env or agent changed
#  15. How to connect
#
# Settings (environment variables):
#   ZONE      default us-east1-b
#   VM_NAME   default sbx-node-1
set -Eeuo pipefail

ZONE=${ZONE:-us-east1-b}
REGION=${ZONE%-*}                 # us-east1-b -> us-east1
VM_NAME=${VM_NAME:-sbx-node-1}
SA_NAME=sbx-host
STORAGE_SCOPE=https://www.googleapis.com/auth/devstorage.read_write
GRANT_TRIES=6                     # a brand-new service account can take a few seconds to be usable
GRANT_WAIT_SECONDS=5
VM_USER=sbx                       # the one account everyone installs as
REPO_URL=https://github.com/kiyashahidi9/capstone-project-sandboxes.git
LOCAL_REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
REMOTE_REPO=/home/$VM_USER/capstone-project-sandboxes
AGENT_UPLOAD=/home/$VM_USER/agent-upload
AGENTS_DIR=/opt/agents            # where setup_agents.sh installs agents on the VM
SECRETS_FILE=secrets.env          # an agent's provider keys; root only on the VM, read only by the API's key proxy
SSH_TRIES=24                      # a new VM takes up to about two minutes to accept SSH
SSH_WAIT_SECONDS=5
ADMIN_KEY=""                      # set only when this run makes a new .env
SERVICE=sbx-api
API_TRIES=15                      # the API takes a few seconds to start
API_WAIT_SECONDS=2

die()  { echo "error: $*" >&2; exit 1; }
log()  { echo "==> install $*"; }   # "install": the setup scripts it runs on the VM print their own numbered steps
skip() { echo "    $* already exists"; }

USAGE='usage: install.sh [--agent <folder> [--cmd "<command>"]]   or   install.sh --update-secrets [<folder>]'

# Reads --agent, --cmd and --update-secrets from the command line.
parse_args() {
  AGENT_PATH="" AGENT_RUN="" UPDATE_SECRETS="" SECRETS_FROM=""
  while (( $# )); do
    case $1 in
      --agent) AGENT_PATH=${2:?--agent needs a folder}; shift 2 ;;
      --cmd)   AGENT_RUN=${2:?--cmd needs a command}; shift 2 ;;
      --update-secrets)
        UPDATE_SECRETS=yes; shift
        if (( $# )) && [[ $1 != --* ]]; then SECRETS_FROM=$1; shift; fi ;;
      *) die "$USAGE" ;;
    esac
  done
  [[ -z $AGENT_RUN || -n $AGENT_PATH ]] || die "--cmd only works together with --agent"
  [[ -z $AGENT_PATH || -d $AGENT_PATH ]] || die "--agent $AGENT_PATH is not a folder"
  [[ -z $UPDATE_SECRETS || -z $AGENT_PATH ]] || die "--update-secrets replaces keys only; use it without --agent"
}

# Stops unless the Google Cloud CLI is installed and logged in.
require_gcloud() {
  command -v gcloud >/dev/null || die "install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install"
  [[ -n $(gcloud auth list --filter=status:ACTIVE --format='value(account)') ]] || die "log in first: gcloud auth login"
}

# Gives the project gcloud is set to, or stops if none is picked.
current_project() {
  local project
  project=$(gcloud config get-value project 2>/dev/null)
  [[ -n $project ]] || die "pick a project first: gcloud config set project <project-id>"
  echo "$project"
}

# Stops unless billing is turned on for the project.
require_billing() {
  [[ $(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)') == True ]] \
    || die "turn on billing for $PROJECT: https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT"
}

# Turns on the Google Cloud services the host needs.
enable_apis() {
  gcloud services enable compute.googleapis.com storage.googleapis.com iam.googleapis.com
}

# Creates the bucket for snapshot copies, unless it already exists.
ensure_bucket() {
  if gcloud storage buckets describe "gs://$BUCKET" &>/dev/null; then skip "gs://$BUCKET"; return; fi
  gcloud storage buckets create "gs://$BUCKET" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
}

# Creates the host's service account, unless it already exists.
ensure_service_account() {
  if gcloud iam service-accounts describe "$SA_EMAIL" &>/dev/null; then skip "$SA_EMAIL"; return; fi
  gcloud iam service-accounts create "$SA_NAME" --display-name="Sandbox host"
}

# Lets the service account read and write the snapshot bucket, and nothing else in the project.
grant_bucket_access() {
  local try
  for ((try = 1; try <= GRANT_TRIES; try++)); do
    gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
      --member="serviceAccount:$SA_EMAIL" --role=roles/storage.objectAdmin >/dev/null && return
    echo "    service account not usable yet, trying again in ${GRANT_WAIT_SECONDS}s"
    sleep "$GRANT_WAIT_SECONDS"
  done
  die "couldn't give $SA_EMAIL access to gs://$BUCKET"
}

# Creates the host VM, running as the service account, unless it already exists.
ensure_vm() {
  if gcloud compute instances describe "$VM_NAME" --zone="$ZONE" &>/dev/null; then skip "VM $VM_NAME"; return; fi
  gcloud compute instances create "$VM_NAME" \
    --zone="$ZONE" \
    --machine-type=n2-standard-8 \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=200GB \
    --enable-nested-virtualization \
    --service-account="$SA_EMAIL" \
    --scopes="$STORAGE_SCOPE" \
    || die "couldn't create $VM_NAME. On a new project this is usually one of:
  - your organization blocks nested virtualization (policy compute.disableNestedVirtualization)
  - you aren't allowed to attach $SA_EMAIL to a VM (role iam.serviceAccountUser)"
}

# Stops unless the VM runs as the service account; a VM from the old setup steps has none, so it can't upload.
check_vm_service_account() {
  local email
  email=$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --format='value(serviceAccounts[0].email)')
  [[ $email == "$SA_EMAIL" ]] || die "$VM_NAME runs as '${email:-no service account}', not $SA_EMAIL, so it can't upload snapshots.
Fix it (stops the VM for a moment):
  gcloud compute instances stop $VM_NAME --zone=$ZONE
  gcloud compute instances set-service-account $VM_NAME --zone=$ZONE --service-account=$SA_EMAIL --scopes=$STORAGE_SCOPE
  gcloud compute instances start $VM_NAME --zone=$ZONE"
}

# Turns off OS Login on the VM, so everyone who installs shares the sbx account (an organization rule can still force it on).
turn_off_os_login() {
  gcloud compute instances add-metadata "$VM_NAME" --zone="$ZONE" --metadata=enable-oslogin=FALSE >/dev/null
}

# Runs a command on the VM as the shared sbx user.
vm_ssh() {
  # --command, not "-- cmd": gcloud then opens no terminal, so output has no stray \r and stdin can be piped in
  gcloud compute ssh "$VM_USER@$VM_NAME" --zone="$ZONE" --quiet --command="$*"
}

# Waits until the VM accepts SSH, which takes up to about two minutes after it's created.
wait_for_ssh() {
  local try
  for ((try = 1; try <= SSH_TRIES; try++)); do
    vm_ssh true &>/dev/null && return
    sleep "$SSH_WAIT_SECONDS"
  done
  die "couldn't connect to $VM_NAME over SSH"
}

# Stops unless SSH really lands in the sbx account; an organization rule forcing OS Login prevents that.
require_shared_user() {
  local user
  user=$(vm_ssh whoami)
  [[ $user == "$VM_USER" ]] \
    || die "SSH logged in as '$user', not $VM_USER: your organization probably requires OS Login, so everyone would get a different account on $VM_NAME. This installer doesn't support that yet."
}

# Gives the commit this computer's copy of the repo is on: the version the VM gets.
laptop_commit() {
  git -C "$LOCAL_REPO" rev-parse HEAD
}

# Puts the repo on the VM at that commit, cloning it the first time; stops if the commit isn't on GitHub.
install_repo() {
  vm_ssh "[ -d $REMOTE_REPO/.git ] || git clone -q $REPO_URL $REMOTE_REPO
          git -C $REMOTE_REPO fetch -q origin && git -C $REMOTE_REPO checkout -q $COMMIT" \
    || die "couldn't install commit ${COMMIT:0:7} on $VM_NAME. Push it to GitHub first."
}

# Prepares the VM to run sandboxes; quick when it's already done.
run_setup_host() {
  vm_ssh "cd $REMOTE_REPO/sbx && sudo bash scripts/setup_host.sh"
}

# Checks whether the VM already has a .env, so a rerun keeps its admin key.
env_exists() {
  vm_ssh "sudo test -f $REMOTE_REPO/.env"
}

# Writes a new .env with a fresh admin key; secrets go over SSH input, never a command line.
write_env() {
  ADMIN_KEY=$(openssl rand -hex 32)
  env_contents | vm_write_root_only "$REMOTE_REPO/.env"
}

# Saves what arrives on stdin as a file on the VM that only root can read, with no moment it's readable by others.
vm_write_root_only() {
  vm_ssh "sudo bash -c 'umask 077 && cat > $1'"
}

# Gives the text of a new .env for this host; provider keys don't go here, they go in the agent's secrets.env.
env_contents() {
  cat <<EOF
SANDBOX_ADMIN_KEY=$ADMIN_KEY
AGENT_CMD=$AGENTS_DIR/.venv/bin/python $AGENTS_DIR/agent.py
AGENT_DIR=
SNAPSHOT_BUCKET=gs://$BUCKET
DELETE_PAUSED_AFTER_DAYS=
EOF
}

# Copies the client's agent folder to the VM, leaving out secrets, virtualenvs and git history.
send_agent() {
  local parent name
  parent=$(dirname "$AGENT_PATH")
  name=$(basename "$AGENT_PATH")
  COPYFILE_DISABLE=1 tar -C "$parent" --no-xattrs -czf - \
      --exclude='.env*' --exclude=.venv --exclude=.git --exclude=__pycache__ "$name" \
    | vm_ssh "rm -rf $AGENT_UPLOAD && mkdir -p $AGENT_UPLOAD && tar -xzf - -C $AGENT_UPLOAD"
}

# Locks the agent down on the VM, installing the client's agent first when --agent was given.
run_setup_agents() {
  local settings=""
  if [[ -n $AGENT_PATH ]]; then
    settings="AGENT_SRC=$AGENT_UPLOAD/$(basename "$AGENT_PATH")"
    [[ -z $AGENT_RUN ]] || settings+=" AGENT_RUN=$(printf %q "$AGENT_RUN")"
  fi
  vm_ssh "cd $REMOTE_REPO/sbx && sudo $settings bash scripts/setup_agents.sh"
}

# Deletes the uploaded copy of the client's agent, so the only copy of its secrets.env on the VM is the root-only one.
remove_agent_upload() {
  vm_ssh "rm -rf $AGENT_UPLOAD"
}

# ---------------------------------------------------------------------------
# Agent keys: each agent's secrets.env, which only the API's key proxy reads
# ---------------------------------------------------------------------------

# Gives the folder on the VM an agent is installed in: the client's folder by name, or the example agent's.
vm_agent_dir() {
  if [[ -n $1 ]]; then echo "$AGENTS_DIR/$(basename "$1")"; else echo "$AGENTS_DIR"; fi
}

# Checks whether the example agent already has its secrets.env on the VM.
example_key_exists() {
  vm_ssh "sudo test -f $AGENTS_DIR/$SECRETS_FILE"
}

# Asks for the example agent's OpenAI key and saves it as its root-only secrets.env on the VM.
ask_example_key() {
  local openai_key
  read -rsp "OpenAI API key for the example agent (typing is hidden): " openai_key
  echo
  [[ -n $openai_key ]] || die "the example agent needs an OpenAI key; to use another provider, install your own agent with --agent"
  printf 'OPENAI_API_KEY=%s\n' "$openai_key" | vm_write_root_only "$AGENTS_DIR/$SECRETS_FILE"
}

# Warns when the client's agent folder has no secrets.env, since its model calls are refused without one.
warn_if_no_secrets() {
  [[ -f $AGENT_PATH/$SECRETS_FILE ]] && return
  echo "    warning: $AGENT_PATH has no $SECRETS_FILE, so its model calls will be refused."
  echo "    Add one (docs/agent_contract.md), then run: install.sh --update-secrets $AGENT_PATH"
}

# Copies secrets.env from the client's agent folder to that agent's folder on the VM, root only.
send_secrets() {
  local folder=$1 target
  target=$(vm_agent_dir "$folder")
  [[ -f $folder/$SECRETS_FILE ]] || die "$folder has no $SECRETS_FILE"
  vm_ssh "sudo test -d $target" || die "$(basename "$folder") isn't installed on $VM_NAME yet: run install.sh --agent $folder first"
  vm_write_root_only "$target/$SECRETS_FILE" < "$folder/$SECRETS_FILE"
}

# Replaces an agent's keys on the VM without reinstalling or restarting anything.
update_secrets() {
  require_gcloud
  if [[ -n $SECRETS_FROM ]]; then send_secrets "$SECRETS_FROM"; else ask_example_key; fi
  echo "Keys updated. The next model call uses them; nothing was restarted."
}

# Gives the commit the VM's repo is on, or nothing before the first install.
vm_commit() {
  vm_ssh "git -C $REMOTE_REPO rev-parse HEAD 2>/dev/null || true"
}

# Installs the API as a service that starts at boot and after a crash.
install_service() {
  vm_ssh "sudo install -m 644 $REMOTE_REPO/sbx/scripts/$SERVICE.service /etc/systemd/system/$SERVICE.service \
          && sudo systemctl daemon-reload && sudo systemctl enable --quiet $SERVICE"
}

# Checks whether the API must restart: it isn't running, or this run changed the code, .env or agent.
needs_restart() {
  [[ $OLD_COMMIT != "$COMMIT" || -n $ADMIN_KEY || -n $AGENT_PATH ]] && return 0
  ! vm_ssh "systemctl is-active --quiet $SERVICE"
}

# Restarts the API so it runs the new code, .env or agent; any agent turn in progress is cut off.
restart_api() {
  vm_ssh "sudo systemctl restart $SERVICE"
}

# Waits until the API answers; a 401 to a request without a key shows it's up and checking keys.
wait_for_api() {
  local try
  for ((try = 1; try <= API_TRIES; try++)); do
    [[ $(vm_ssh "curl -s -o /dev/null -w '%{http_code}' localhost:8000/sandboxes") == 401 ]] && return
    sleep "$API_WAIT_SECONDS"
  done
  die "the API didn't start. See why: gcloud compute ssh $VM_USER@$VM_NAME --zone=$ZONE -- sudo journalctl -u $SERVICE -n 50"
}

# Shows how to reach the API from this computer.
print_connect_instructions() {
  cat <<EOF

Done. To use the API from this computer:

  1. Open a tunnel and leave it running:
     gcloud compute ssh $VM_USER@$VM_NAME --zone=$ZONE -- -N -L 8000:localhost:8000

$(print_admin_key)

  3. Try it:
     curl -H "Authorization: Bearer \$SANDBOX_ADMIN_KEY" localhost:8000/sandboxes
EOF
}

# Shows the admin key if this run made it (the only time it's shown), or how to read the existing one.
print_admin_key() {
  if [[ -n $ADMIN_KEY ]]; then
    echo "  2. Your admin key, shown only this once, so save it:"
    echo "     export SANDBOX_ADMIN_KEY=$ADMIN_KEY"
  else
    echo "  2. Use the admin key from the first install. To read it again:"
    echo "     gcloud compute ssh $VM_USER@$VM_NAME --zone=$ZONE -- sudo grep SANDBOX_ADMIN_KEY $REMOTE_REPO/.env"
  fi
}

# Sets up the host in your Google Cloud project, one step at a time.
main() {
  parse_args "$@"
  if [[ -n $UPDATE_SECRETS ]]; then update_secrets; return; fi

  log "1. preflight"
  require_gcloud
  PROJECT=$(current_project)
  BUCKET=$PROJECT-sbx-snapshots
  SA_EMAIL=$SA_NAME@$PROJECT.iam.gserviceaccount.com
  COMMIT=$(laptop_commit)
  require_billing

  log "2. APIs (the first time takes about a minute)"
  enable_apis

  log "3. bucket gs://$BUCKET"
  ensure_bucket

  log "4. service account $SA_EMAIL"
  ensure_service_account

  log "5. $SA_NAME can use gs://$BUCKET"
  grant_bucket_access

  log "6. VM $VM_NAME in $ZONE"
  ensure_vm
  check_vm_service_account
  turn_off_os_login

  log "7. SSH in as $VM_USER"
  wait_for_ssh
  require_shared_user

  log "8. repo at commit ${COMMIT:0:7}"
  OLD_COMMIT=$(vm_commit)
  install_repo

  log "9. host setup (the first time takes several minutes)"
  run_setup_host

  log "10. .env"
  if env_exists; then skip ".env (keeping its admin key)"; else write_env; fi

  if [[ -n $AGENT_PATH ]]; then
    log "11. your agent $AGENT_PATH"
    send_agent
  else
    log "11. your agent: none, using the example agent (add --agent to install yours)"
  fi

  log "12. agent lockdown"
  run_setup_agents
  if [[ -n $AGENT_PATH ]]; then remove_agent_upload; fi

  log "13. agent keys"
  if [[ -n $AGENT_PATH ]]; then
    warn_if_no_secrets
  elif example_key_exists; then
    skip "the example agent's $SECRETS_FILE"
  else
    ask_example_key
  fi

  log "14. API service $SERVICE"
  install_service
  if needs_restart; then restart_api; else echo "    nothing changed, so the running API was left alone"; fi
  wait_for_api

  print_connect_instructions
}

main "$@"
