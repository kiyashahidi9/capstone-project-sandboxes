#!/usr/bin/env bash
# install_guest_agent.sh - bake the guest agent into the base image (Milestone 1e)
#
# The guest agent (guest_agent/guest_agent.py) is a small HTTP server inside every VM
# on port 49983. The host uses it to run commands and read/write files in the VM.
#
# install does:
#   1. Checks guest_agent.py parses (on the host) and that the image has python3.
#   2. Mounts ~/fc/rootfs.ext4 and copies the agent to /opt/guest_agent/.
#   3. Writes guest-agent.service: at boot it sets the VM's network
#      (172.16.0.2/30, eth0 up, default via 172.16.0.1), then runs the agent.
#   4. Enables the service at boot and unmounts (always, even on error).
#
# Usage:
#   sudo install_guest_agent.sh           install (rerun whenever guest_agent.py changes)
#   sudo install_guest_agent.sh verify    checklist: is the image up to date?
#   install_guest_agent.sh test N         checklist: does the agent work in running VM N?
#                                         (AGENT_URL=http://host:port overrides the address)
#
# VMs boot from copies (~/fc/vmN.ext4), not rootfs.ext4. Copies made before an install
# don't have the new agent: remove them, and `netns.sh start N` makes fresh ones.
#
# 1e test:
#   chmod +x scripts/install_guest_agent.sh                            # once
#   sudo scripts/install_guest_agent.sh && sudo scripts/install_guest_agent.sh verify
#   sudo scripts/netns.sh delete 1; sudo rm -f ~/fc/vm1.ext4          # drop the stale copy
#   sudo scripts/netns.sh start 1
#   scripts/install_guest_agent.sh test 1
set -Eeuo pipefail

readonly PORT=49983
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
readonly AGENT_SRC=$REPO/guest_agent/guest_agent.py
REAL_HOME=$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)   # under sudo, $HOME is /root
FC_DIR=${FC_DIR:-$REAL_HOME/fc}
readonly ROOTFS=$FC_DIR/rootfs.ext4

# Paths inside the image
readonly AGENT_DST=/opt/guest_agent/guest_agent.py
readonly UNIT=/etc/systemd/system/guest-agent.service
readonly UNIT_LINK=/etc/systemd/system/multi-user.target.wants/guest-agent.service

read -r -d '' SERVICE <<EOF || true
[Unit]
Description=Sandbox guest agent
After=network.target

[Service]
# Network setup (the leading "-" means: ignore the error if it's already set)
ExecStartPre=-/sbin/ip addr add 172.16.0.2/30 dev eth0
ExecStartPre=-/sbin/ip link set eth0 up
ExecStartPre=-/sbin/ip route add default via 172.16.0.1 dev eth0
ExecStart=/usr/bin/python3 $AGENT_DST
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF

die()       { echo "error: $*" >&2; exit 1; }
log()       { echo "==> $*"; }
need_root() { (( EUID == 0 )) || die "run as root (sudo)"; }

# ---------------------------------------------------------------- mount helpers

MNT=""
mount_rootfs() {   # mount_rootfs rw|ro
  [[ -f $ROOTFS ]] || die "missing $ROOTFS (see step 1b)"
  MNT=$(mktemp -d /tmp/rootfs.XXXXXX)
  trap unmount_rootfs EXIT
  mount -o "loop,$1" "$ROOTFS" "$MNT"
}
unmount_rootfs() {
  [[ -n $MNT ]] || return 0
  mountpoint -q "$MNT" && umount "$MNT"
  rmdir "$MNT"
  MNT=""
}
image_has_python() { [[ -L $MNT/usr/bin/python3 || -x $MNT/usr/bin/python3 ]]; }

# ---------------------------------------------------------------- install

cmd_install() {
  need_root
  [[ -f $AGENT_SRC ]] || die "missing $AGENT_SRC"
  log "1. checking $AGENT_SRC parses"
  python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])' "$AGENT_SRC"

  log "   mounting $ROOTFS"
  mount_rootfs rw
  image_has_python || die "image has no /usr/bin/python3"

  log "2. copying agent -> $AGENT_DST"
  install -D -m 0755 "$AGENT_SRC" "$MNT$AGENT_DST"

  log "3. writing $UNIT"
  printf '%s\n' "$SERVICE" > "$MNT$UNIT"

  log "4. enabling it at boot, unmounting"
  mkdir -p "$(dirname "$MNT$UNIT_LINK")"
  ln -sf "$UNIT" "$MNT$UNIT_LINK"
  unmount_rootfs

  log "installed"
  local copies=("$FC_DIR"/vm*.ext4)
  if [[ -e ${copies[0]} ]]; then
    echo "note: these VM disks were made before this install and have the old agent:"
    printf '  %s\n' "${copies[@]}"
    echo "remove them (sudo rm -f $FC_DIR/vm*.ext4) and netns.sh start makes fresh ones"
  fi
}

# ---------------------------------------------------------------- checks

FAILS=0
check() {   # check "description" command args...
  local desc=$1; shift
  if "$@" &>/dev/null; then printf '  [x] %s\n' "$desc"
  else printf '  [ ] %s\n' "$desc"; FAILS=$((FAILS + 1)); fi
}
summary() {
  if (( FAILS )); then echo "FAIL: $FAILS check(s) failed"; return 1; fi
  echo "OK"
}

service_matches() { [[ $(cat "$MNT$UNIT") == "$SERVICE" ]]; }

cmd_verify() {
  need_root
  FAILS=0
  mount_rootfs ro
  echo "image $ROOTFS"
  check "image has python3"                    image_has_python
  check "agent installed at $AGENT_DST"        test -f "$MNT$AGENT_DST"
  check "agent matches the repo copy"          cmp -s "$AGENT_SRC" "$MNT$AGENT_DST"
  check "service file is up to date"           service_matches
  check "service enabled at boot"              test -L "$MNT$UNIT_LINK"
  local copy
  for copy in "$FC_DIR"/vm*.ext4; do
    [[ -e $copy ]] || continue
    check "$(basename "$copy") made after the install" not_older "$copy" "$MNT$AGENT_DST"
  done
  unmount_rootfs
  summary
}
not_older() { ! [[ $1 -ot $2 ]]; }

# ---------------------------------------------------------------- live test

URL=""
api()       { local method=$1 path=$2; shift 2; curl -sS --max-time "${MAX_TIME:-15}" -X "$method" "$URL$path" "$@"; }
expect()    { local filter=$1; shift; api "$@" | jq -e "$filter"; }
file_is()   { [[ $(api GET "/files?path=$2") == "$1" ]]; }
status_is() { local want=$1; shift; [[ $(api "$@" -o /dev/null -w '%{http_code}') == "$want" ]]; }

health_during_long_exec() {
  api POST /exec -d '{"cmd":"sleep 3"}' >/dev/null &
  local job=$! rc=0
  sleep 0.5
  MAX_TIME=1 expect '.ok == true' GET /health || rc=1
  wait "$job" || true
  return $rc
}

background_job() {
  local pid
  pid=$(api POST /exec -d '{"cmd":"echo started; sleep 30","background":true}' | jq -e '.pid') || return 1
  sleep 0.5
  expect '.exit_code == 0' POST /exec -d "{\"cmd\":\"kill -0 $pid\"}" || return 1     # still running
  file_is started "/tmp/bg-$pid.log" || return 1                                      # output logged
  api POST /exec -d "{\"cmd\":\"kill -- -$pid\"}" >/dev/null                            # clean up its group
}

cmd_test() {
  [[ ${1:-} =~ ^[0-9]+$ ]] || die "usage: $0 test N"
  command -v jq >/dev/null || die "jq not installed (sudo apt install jq)"
  URL=${AGENT_URL:-http://10.201.0.$1:$PORT}
  FAILS=0
  echo "guest agent in VM $1  ($URL)"

  echo " basics"
  check "GET /health -> ok"                    expect '.ok == true' GET /health
  check "exec: echo hi"                        expect '.exit_code == 0 and .stdout == "hi\n"' POST /exec -d '{"cmd":"echo hi"}'
  check "exec: exit code passes through"       expect '.exit_code == 3' POST /exec -d '{"cmd":"exit 3"}'
  check "exec: stderr captured"                expect '.stderr == "oops\n"' POST /exec -d '{"cmd":"echo oops >&2"}'
  check "exec: cwd respected"                  expect '.stdout == "/tmp\n"' POST /exec -d '{"cmd":"pwd","cwd":"/tmp"}'
  check "exec: bad cwd -> 400"                 status_is 400 POST /exec -d '{"cmd":"pwd","cwd":"/nope"}'
  echo " long-running work"
  check "exec: timeout kills the command"      expect '.timed_out == true' POST /exec -d '{"cmd":"sleep 30","timeout":1}'
  check "/health answers during a long exec"   health_during_long_exec
  check "background: pid, keeps running, logs" background_job
  echo " files"
  check "PUT creates parent folders"           expect '.ok == true' PUT "/files?path=/root/agent-test/sub/test.txt" --data-binary hello
  check "GET returns the contents"             file_is hello /root/agent-test/sub/test.txt
  check "GET missing file -> 404"              status_is 404 GET "/files?path=/root/agent-test/nope"
  echo " network (set up at boot, no ssh)"
  check "guest default route via 172.16.0.1"   expect '.stdout | contains("via 172.16.0.1")' POST /exec -d '{"cmd":"ip route show default"}'
  check "guest reaches the internet"           expect '.stdout | test("HTTP/[0-9.]+ 200")' POST /exec -d '{"cmd":"curl -sI https://github.com | head -1"}'

  api POST /exec -d '{"cmd":"rm -rf /root/agent-test"}' >/dev/null 2>&1 || true
  summary
}

# ---------------------------------------------------------------- main

usage() { sed -n '2,/^set -/p' "$0" | sed '$d; s/^# \{0,1\}//'; }

case ${1:-install} in
  install)         cmd_install ;;
  verify)          cmd_verify ;;
  test)            cmd_test "${2:-}" ;;
  help|-h|--help)  usage ;;
  *)               die "unknown command '$1' (try: $0 help)" ;;
esac
