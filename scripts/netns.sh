#!/usr/bin/env bash
# netns.sh - one network namespace per Firecracker sandbox (Milestone 1d)
#
# Why: every VM (and later every restored snapshot) insists on tap0 + 172.16.0.2.
# Each VM gets its own network namespace, so all of them can keep that identity,
# and the host reaches VM N at its own unique "clone address" 10.201.0.N.
# Pattern from Firecracker's docs/snapshotting/network-for-clones.md.
#
# Layout for sandbox N (1..254):
#   namespace          sbxN
#   tap0 (in ns)       172.16.0.1/30                     same in every ns
#   guest              172.16.0.2                        same in every VM
#   veth pair          vethNh 10.200.N.1/30 (host)  <->  vethNn 10.200.N.2/30 (ns)
#   clone address      10.201.0.N                        how the host reaches VM N
#
# Packet flow:
#   host -> 10.201.0.N -> vethNh => vethNn [DNAT -> 172.16.0.2] -> tap0 -> guest
#   guest -> tap0 [SNAT 172.16.0.2 -> 10.201.0.N] -> vethNn => vethNh -> MASQUERADE -> internet
#      (the guest-agent service in the image sets "default via 172.16.0.1" at boot)
#
# Create steps (one function each below):
#   1. namespace sbxN, loopback up
#   2. tap0 inside it with 172.16.0.1/30
#   3. veth pair host <-> ns, ns default route via the host end
#   4. NAT inside the ns: guest IP <-> clone address
#   5. host: route clone address into the ns, forwarding + MASQUERADE to the internet
#
# Usage (as root):
#   netns.sh start N            create sbxN if needed, boot a VM in it, set its route
#   netns.sh stop N             stop the VM in sbxN (keeps network and disk)
#   netns.sh create N           build sandbox N's network only (rolls back on failure)
#   netns.sh delete N           stop the VM and remove everything for N (disk kept)
#   netns.sh verify N           checklist that every piece of N exists (+ live checks)
#   netns.sh verify-deleted N   checklist that nothing of N is left behind
#   netns.sh list               show sandbox namespaces, clone IPs, pids
#   netns.sh host-teardown      remove shared host NAT/FORWARD rules (no sandboxes left)
#
# Files (FC_DIR, default ~/fc): vmlinux, rootfs.ext4, vm_key; per VM: vmN.ext4, console-sbxN.log
#
# 1D TEST HERE:
#   chmod +x scripts/netns.sh                                          # enables execution (once)
#   sudo scripts/netns.sh start 1 && sudo scripts/netns.sh start 2     # two VMs at once
#   sudo scripts/netns.sh verify 1 && sudo scripts/netns.sh verify 2   # checklist incl. live checks
#   ssh -i ~/fc/vm_key root@10.201.0.1                                 # terminal 1
#   ssh -i ~/fc/vm_key root@10.201.0.2                                 # terminal 2; then you can exit
#   sudo scripts/netns.sh delete 1 && sudo scripts/netns.sh delete 2   # tear down
#   sudo scripts/netns.sh verify-deleted 1 && sudo scripts/netns.sh verify-deleted 2
#   sudo scripts/netns.sh host-teardown                                # optional: shared host rules
set -Eeuo pipefail

# Fixed identity every VM sees (and every snapshot will remember)
readonly TAP=tap0
readonly TAP_IP=172.16.0.1
readonly GUEST_IP=172.16.0.2
readonly CLONE_NET=10.201.0.0/16
readonly GUEST_MAC=06:00:AC:10:00:02   # the test image derives 172.16.0.2 from this MAC

# Kernel, rootfs and key from 1a/1b. Under sudo, $HOME is /root, so use the real user's home.
REAL_HOME=$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)
FC_DIR=${FC_DIR:-$REAL_HOME/fc}
SSH_OPTS=(-i "$FC_DIR/vm_key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o ConnectTimeout=3 -o BatchMode=yes -o LogLevel=ERROR)

die()       { echo "error: $*" >&2; exit 1; }
log()       { echo "==> $*"; }
need_root() { (( EUID == 0 )) || die "run as root (sudo)"; }

# Sets N, NS, VETH_H, VETH_N, VETH_H_IP, VETH_N_IP, CLONE_IP, SOCK for sandbox $1
vars() {
  [[ ${1:-} =~ ^[0-9]+$ ]] || die "N must be a number (got '${1:-}')"
  N=$((10#$1))
  (( N >= 1 && N <= 254 )) || die "N must be 1..254 (got $N)"
  NS=sbx$N
  VETH_H=veth${N}h
  VETH_N=veth${N}n
  VETH_H_IP=10.200.$N.1
  VETH_N_IP=10.200.$N.2
  CLONE_IP=10.201.0.$N
  SOCK=/tmp/$NS.sock
}

in_ns()     { ip netns exec "$NS" "$@"; }
guest_ssh() { ssh "${SSH_OPTS[@]}" "root@$CLONE_IP" "$@"; }         # the way the host normally reaches the VM

host_iface() {
  local dev
  dev=$(ip route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i+1); exit}}')
  [[ -n $dev ]] || die "host has no default route"
  echo "$dev"
}

# Host iptables rules are shared by all sandboxes: add once, remove all copies.
ipt_ensure() { local t=$1 c=$2; shift 2; iptables -t "$t" -C "$c" "$@" 2>/dev/null || iptables -t "$t" -I "$c" "$@"; }
ipt_remove() { local t=$1 c=$2; shift 2; while iptables -t "$t" -D "$c" "$@" 2>/dev/null; do :; done; }

# ---------------------------------------------------------------- create steps

step1_namespace() {
  log "1. namespace $NS"
  ip netns add "$NS"
  in_ns ip link set lo up
}

step2_tap() {
  log "2. $TAP in $NS with $TAP_IP/30"
  in_ns ip tuntap add dev "$TAP" mode tap
  in_ns ip addr add "$TAP_IP/30" dev "$TAP"
  in_ns ip link set "$TAP" up
}

step3_veth() {
  log "3. veth $VETH_H ($VETH_H_IP) <-> $VETH_N ($VETH_N_IP)"
  ip link add "$VETH_H" type veth peer name "$VETH_N"
  ip link set "$VETH_N" netns "$NS"
  ip addr add "$VETH_H_IP/30" dev "$VETH_H"
  ip link set "$VETH_H" up
  in_ns ip addr add "$VETH_N_IP/30" dev "$VETH_N"
  in_ns ip link set "$VETH_N" up
  in_ns ip route add default via "$VETH_H_IP"
}

step4_ns_nat() {
  log "4. NAT in $NS: $GUEST_IP <-> $CLONE_IP"
  in_ns sysctl -qw net.ipv4.ip_forward=1
  in_ns iptables -t nat -A POSTROUTING -o "$VETH_N" -s "$GUEST_IP" -j SNAT --to-source "$CLONE_IP"
  in_ns iptables -t nat -A PREROUTING  -i "$VETH_N" -d "$CLONE_IP" -j DNAT --to-destination "$GUEST_IP"
}

step5_host() {
  local wan
  wan=$(host_iface)
  log "5. host: route $CLONE_IP into $NS, NAT $CLONE_NET out $wan"
  ip route add "$CLONE_IP" via "$VETH_N_IP"
  sysctl -qw net.ipv4.ip_forward=1
  ipt_ensure nat    POSTROUTING -s "$CLONE_NET" -o "$wan" -j MASQUERADE
  # Narrow FORWARD rules instead of "iptables -P FORWARD ACCEPT" (which opens the whole host)
  ipt_ensure filter FORWARD -s "$CLONE_NET" -j ACCEPT
  ipt_ensure filter FORWARD -d "$CLONE_NET" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
}

cmd_create() {
  vars "$1"
  if [[ -e /run/netns/$NS ]]; then die "$NS already exists (run: $0 delete $N)"; fi
  if ip link show "$VETH_H" &>/dev/null; then die "$VETH_H already exists (run: $0 delete $N)"; fi

  trap 'trap - ERR; echo "!! create $NS failed, rolling back" >&2; cmd_delete "$N" >/dev/null' ERR
  step1_namespace
  step2_tap
  step3_veth
  step4_ns_nat
  step5_host
  trap - ERR

  log "created $NS (VM will be reachable at $CLONE_IP)"
}

# ---------------------------------------------------------------- VM start/stop

fc_put() {
  curl -sS --fail-with-body --unix-socket "$SOCK" -X PUT "http://localhost$1" \
    -H 'Content-Type: application/json' -d "$2" >/dev/null
}

cmd_start() {
  vars "$1"
  local disk=$FC_DIR/vm$N.ext4 console=$FC_DIR/console-$NS.log f i
  for f in vmlinux rootfs.ext4 vm_key; do
    [[ -s $FC_DIR/$f ]] || die "missing $FC_DIR/$f"
  done
  command -v firecracker >/dev/null || die "firecracker not on PATH"
  [[ -e /run/netns/$NS ]] || cmd_create "$N"
  [[ -z $(ip netns pids "$NS" 2>/dev/null) ]] || die "a VM is already running in $NS (run: $0 stop $N)"

  if [[ ! -e $disk ]]; then
    log "copying rootfs -> $disk"
    cp --sparse=always "$FC_DIR/rootfs.ext4" "$disk"
  fi

  log "starting firecracker in $NS (console -> $console)"
  rm -f "$SOCK"
  setsid ip netns exec "$NS" firecracker --api-sock "$SOCK" >"$console" 2>&1 </dev/null &
  for i in $(seq 1 100); do [[ -S $SOCK ]] && break; sleep 0.05; done
  [[ -S $SOCK ]] || die "socket never appeared, see $console"

  log "configuring and booting"
  fc_put /boot-source "{\"kernel_image_path\":\"$FC_DIR/vmlinux\",\"boot_args\":\"console=ttyS0 reboot=k panic=1 pci=off\"}"
  fc_put /drives/rootfs "{\"drive_id\":\"rootfs\",\"path_on_host\":\"$disk\",\"is_root_device\":true,\"is_read_only\":false}"
  fc_put /machine-config '{"vcpu_count":2,"mem_size_mib":1024}'
  fc_put /network-interfaces/eth0 "{\"iface_id\":\"eth0\",\"guest_mac\":\"$GUEST_MAC\",\"host_dev_name\":\"$TAP\"}"
  fc_put /actions '{"action_type":"InstanceStart"}'

    log "waiting for the guest agent on $CLONE_IP:49983"
    for i in $(seq 1 60); do curl -sf --max-time 1 "http://$CLONE_IP:49983/health" >/dev/null && break; sleep 1; done
    curl -sf --max-time 2 "http://$CLONE_IP:49983/health" >/dev/null \
      || die "guest agent never answered (disk older than install_guest_agent.sh? remove $disk). See $console"

  log "VM $N is up:  ssh -i $FC_DIR/vm_key root@$CLONE_IP"
}

cmd_stop() {
  vars "$1"
  local pids
  pids=$(ip netns pids "$NS" 2>/dev/null | xargs || true)
  if [[ -n $pids ]]; then
    kill $pids 2>/dev/null || true
    sleep 0.5
    kill -9 $pids 2>/dev/null || true
  fi
  rm -f "$SOCK"
  log "stopped VM $N (network and $FC_DIR/vm$N.ext4 kept)"
}

# ---------------------------------------------------------------- delete

cmd_delete() {
  cmd_stop "$1" >/dev/null                       # kills anything in the ns and removes the socket
  ip route del "$CLONE_IP" 2>/dev/null || true
  ip link del "$VETH_H"    2>/dev/null || true   # deleting one end deletes both
  ip netns del "$NS"       2>/dev/null || true   # also drops tap0 and the ns's iptables rules
  log "deleted $NS (disk kept; shared host rules kept, see host-teardown)"
}

# ---------------------------------------------------------------- checks

FAILS=0
check() {   # check "description" command args...
  local desc=$1; shift
  if "$@" &>/dev/null; then printf '  [x] %s\n' "$desc"
  else printf '  [ ] %s\n' "$desc"; FAILS=$((FAILS + 1)); fi
}
not()         { ! "$@"; }
out_has()     { local want=$1 out; shift; out=$("$@" 2>/dev/null) || return 1; [[ $out == *"$want"* ]]; }
no_output()   { [[ -z $("$@" 2>/dev/null) ]]; }
ns_exists()   { [[ -e /run/netns/$NS ]]; }
host_fwd_on() { [[ $(sysctl -n net.ipv4.ip_forward) == 1 ]]; }
ns_fwd_on()   { [[ $(in_ns sysctl -n net.ipv4.ip_forward 2>/dev/null) == 1 ]]; }

summary() {
  if (( FAILS )); then echo "FAIL: $FAILS check(s) failed"; return 1; fi
  echo "OK"
}

cmd_verify() {
  vars "$1"
  local wan
  wan=$(host_iface)
  FAILS=0
  echo "$NS  (VM at $CLONE_IP)"

  echo " 1. namespace"
  check "namespace $NS exists"               ns_exists
  check "lo is up"                           out_has ",UP" in_ns ip -o link show dev lo
  echo " 2. tap"
  check "$TAP has $TAP_IP/30"                out_has " $TAP_IP/30 " in_ns ip -o addr show dev "$TAP"
  check "$TAP is up"                         out_has ",UP" in_ns ip -o link show dev "$TAP"
  echo " 3. veth"
  check "$VETH_H (host) has $VETH_H_IP/30"   out_has " $VETH_H_IP/30 " ip -o addr show dev "$VETH_H"
  check "$VETH_H is up"                      out_has ",UP" ip -o link show dev "$VETH_H"
  check "$VETH_N (ns) has $VETH_N_IP/30"     out_has " $VETH_N_IP/30 " in_ns ip -o addr show dev "$VETH_N"
  check "$VETH_N is up"                      out_has ",UP" in_ns ip -o link show dev "$VETH_N"
  check "ns default route via $VETH_H_IP"    out_has "via $VETH_H_IP " in_ns ip route show default
  echo " 4. NAT in namespace"
  check "ip_forward=1 in $NS"                ns_fwd_on
  check "SNAT $GUEST_IP -> $CLONE_IP"        in_ns iptables -t nat -C POSTROUTING -o "$VETH_N" -s "$GUEST_IP" -j SNAT --to-source "$CLONE_IP"
  check "DNAT $CLONE_IP -> $GUEST_IP"        in_ns iptables -t nat -C PREROUTING -i "$VETH_N" -d "$CLONE_IP" -j DNAT --to-destination "$GUEST_IP"
  echo " 5. host"
  check "route $CLONE_IP via $VETH_N_IP"     out_has "via $VETH_N_IP " ip route show "$CLONE_IP"
  check "ip_forward=1 on host"               host_fwd_on
  check "MASQUERADE $CLONE_NET out $wan"     iptables -t nat -C POSTROUTING -s "$CLONE_NET" -o "$wan" -j MASQUERADE
  check "FORWARD allows $CLONE_NET out"      iptables -C FORWARD -s "$CLONE_NET" -j ACCEPT
  check "FORWARD allows replies back"        iptables -C FORWARD -d "$CLONE_NET" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

  echo " live (VM running)"
  if [[ -n $(ip netns pids "$NS" 2>/dev/null) ]]; then
    check "guest agent /health"              out_has '"ok": true' curl -s --max-time 2 "http://$CLONE_IP:49983/health"
    check "host can ping $CLONE_IP"          ping -c1 -W2 "$CLONE_IP"
    check "host can ssh to $CLONE_IP"        guest_ssh true
    check "guest has default via $TAP_IP"    out_has "via $TAP_IP " guest_ssh ip route show default
    check "guest can reach the internet"     guest_ssh ping -c1 -W3 1.1.1.1
  else
    echo "  [-] skipped: nothing running in $NS"
  fi
  summary
}

cmd_verify_deleted() {
  vars "$1"
  FAILS=0
  echo "$NS cleanup"
  check "namespace $NS is gone"              not ns_exists
  check "$VETH_H is gone"                    not ip link show dev "$VETH_H"
  check "no host route for $CLONE_IP"        no_output ip route show "$CLONE_IP"
  check "no firecracker using $SOCK"         not pgrep -f -- "$SOCK"
  check "$SOCK removed"                      not test -e "$SOCK"
  summary
}

# ---------------------------------------------------------------- misc

cmd_list() {
  local f ns
  for f in /run/netns/sbx*; do
    [[ -e $f ]] || { echo "(no sandboxes)"; return; }
    ns=${f##*/}
    printf '%-7s %-13s pids: %s\n' "$ns" "10.201.0.${ns#sbx}" "$(ip netns pids "$ns" 2>/dev/null | xargs)"
  done
}

cmd_host_teardown() {
  if compgen -G "/run/netns/sbx*" >/dev/null; then die "sandbox namespaces still exist (see: $0 list)"; fi
  local wan
  wan=$(host_iface)
  ipt_remove nat    POSTROUTING -s "$CLONE_NET" -o "$wan" -j MASQUERADE
  ipt_remove filter FORWARD -s "$CLONE_NET" -j ACCEPT
  ipt_remove filter FORWARD -d "$CLONE_NET" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  log "removed shared host rules (net.ipv4.ip_forward left as is)"
}

usage() { sed -n '2,/^set -/p' "$0" | sed '$d; s/^# \{0,1\}//'; }

main() {
  local cmd=${1:-help}
  shift || true
  case $cmd in
    start|stop|create|delete|verify|verify-deleted)
      [[ $# -eq 1 ]] || die "usage: $0 $cmd N"
      need_root ;;
    list|host-teardown) need_root ;;
  esac
  case $cmd in
    start)           cmd_start "$1" ;;
    stop)            cmd_stop "$1" ;;
    create)          cmd_create "$1" ;;
    delete)          cmd_delete "$1" ;;
    verify)          cmd_verify "$1" ;;
    verify-deleted)  cmd_verify_deleted "$1" ;;
    list)            cmd_list ;;
    host-teardown)   cmd_host_teardown ;;
    help|-h|--help)  usage ;;
    *)               die "unknown command '$cmd' (try: $0 help)" ;;
  esac
}

main "$@"
