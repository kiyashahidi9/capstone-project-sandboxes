#!/usr/bin/env bash
# Step 1c: boot one Firecracker VM "by hand" (plain curl calls, no Python).
#
# Usage:   sudo scripts/start_vm_by_hand.sh
#
# What it does:
#   1. Creates tap0 on the host (host = 172.16.0.1, VM = 172.16.0.2) and NAT to the internet.
#   2. Starts Firecracker with its API socket at /tmp/sbx1.sock.
#   3. Sends the config calls (boot-source, drive, machine-config, network) and boots.
#   4. Waits until SSH answers, then sets the VM's default route.
#   5. Stays running. Ctrl+C (or `reboot` inside the VM) stops the VM and removes tap0.
#
# Needs (from 1a and 1b): firecracker on PATH, and vmlinux, rootfs.ext4, vm_key in FC_DIR.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

# Under sudo, $HOME is /root, so find the real user's ~/fc.
REAL_HOME=$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)
FC_DIR=${FC_DIR:-$REAL_HOME/fc}

S=/tmp/sbx1.sock
TAP=tap0
HOST_IP=172.16.0.1
GUEST_IP=172.16.0.2
GUEST_MAC=06:00:AC:10:00:02   # this MAC makes the test image use 172.16.0.2
CONSOLE_LOG=$FC_DIR/console.log
SSH_OPTS=(-i "$FC_DIR/vm_key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o ConnectTimeout=2 -o BatchMode=yes -o LogLevel=ERROR)

for f in vmlinux rootfs.ext4 vm_key; do
  [ -s "$FC_DIR/$f" ] || { echo "Missing $FC_DIR/$f (see steps 1b)" >&2; exit 1; }
done
command -v firecracker >/dev/null || { echo "firecracker not on PATH (see step 1a)" >&2; exit 1; }
if ip link show "$TAP" >/dev/null 2>&1; then
  echo "$TAP already exists. Remove it first: sudo ip link del $TAP" >&2
  exit 1
fi

FC_PID=""
cleanup() {
  echo
  echo "Shutting down..."
  [ -n "$FC_PID" ] && kill "$FC_PID" 2>/dev/null && wait "$FC_PID" 2>/dev/null || true
  ip link del "$TAP" 2>/dev/null || true
  rm -f "$S"
  echo "VM stopped, $TAP removed."
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---------- Step 1: network device for the VM ----------
echo "==> Creating $TAP ($HOST_IP/30)"
ip tuntap add dev "$TAP" mode tap
ip addr add "$HOST_IP/30" dev "$TAP"
ip link set dev "$TAP" up

# Let the VM reach the internet through the host
sysctl -qw net.ipv4.ip_forward=1
iptables -P FORWARD ACCEPT
HOST_IFACE=$(ip -j route list default | jq -r '.[0].dev')
iptables -t nat -C POSTROUTING -o "$HOST_IFACE" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -o "$HOST_IFACE" -j MASQUERADE

# ---------- Step 2: start Firecracker ----------
echo "==> Starting Firecracker (console -> $CONSOLE_LOG)"
rm -f "$S"
firecracker --api-sock "$S" >"$CONSOLE_LOG" 2>&1 </dev/null &
FC_PID=$!

for _ in $(seq 1 100); do   # 100 x 50 ms = 5 s
  [ -S "$S" ] && break
  sleep 0.05
done
[ -S "$S" ] || { echo "Firecracker socket never appeared. See $CONSOLE_LOG" >&2; exit 1; }

# ---------- Step 3: configure and boot ----------
# --fail-with-body: on an HTTP error, print Firecracker's message and stop the script.
fc_put() {
  echo "    PUT $1"
  curl -sS --fail-with-body --unix-socket "$S" -X PUT "http://localhost$1" \
    -H 'Content-Type: application/json' -d "$2"
}

echo "==> Configuring VM"
fc_put /boot-source \
  "{\"kernel_image_path\":\"$FC_DIR/vmlinux\",\"boot_args\":\"console=ttyS0 reboot=k panic=1 pci=off\"}"
fc_put /drives/rootfs \
  "{\"drive_id\":\"rootfs\",\"path_on_host\":\"$FC_DIR/rootfs.ext4\",\"is_root_device\":true,\"is_read_only\":false}"
fc_put /machine-config '{"vcpu_count":2,"mem_size_mib":1024}'
fc_put /network-interfaces/eth0 \
  "{\"iface_id\":\"eth0\",\"guest_mac\":\"$GUEST_MAC\",\"host_dev_name\":\"$TAP\"}"

echo "==> Booting"
BOOT_START=$(date +%s.%N)
fc_put /actions '{"action_type":"InstanceStart"}'

# ---------- Step 4: wait for SSH, set the VM's default route ----------
echo "==> Waiting for SSH on $GUEST_IP"
for _ in $(seq 1 60); do
  ssh "${SSH_OPTS[@]}" "root@$GUEST_IP" true 2>/dev/null && break
  kill -0 "$FC_PID" 2>/dev/null || { echo "Firecracker exited. See $CONSOLE_LOG" >&2; exit 1; }
  sleep 1
done
ssh "${SSH_OPTS[@]}" "root@$GUEST_IP" true 2>/dev/null \
  || { echo "SSH never answered. Check guest_mac and 'ip addr show $TAP'. Console: $CONSOLE_LOG" >&2; exit 1; }
BOOT_SECS=$(echo "$(date +%s.%N) - $BOOT_START" | bc 2>/dev/null || echo "?")

ssh "${SSH_OPTS[@]}" "root@$GUEST_IP" "ip route replace default via $HOST_IP dev eth0"

cat <<EOF

VM is up (SSH ready ${BOOT_SECS}s after InstanceStart).

  Log in:       ssh -i $FC_DIR/vm_key root@$GUEST_IP
  Console log:  tail -f $CONSOLE_LOG

  Checks to run inside the VM:
    ping -c 2 8.8.8.8
    curl -sI https://github.com | head -1
    python3 --version && git --version && node --version

Press Ctrl+C here (or run 'reboot' inside the VM) to stop it and remove $TAP.
EOF

wait "$FC_PID" || true
FC_PID=""
