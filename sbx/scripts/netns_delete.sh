#!/usr/bin/env bash
# Delete the network for sandbox slot N (Build Plan 1d). Called by node_agent.delete_network().
#
#   sudo scripts/netns_delete.sh N
#
# Safe to run when some or all of it is already gone. Leaves the shared host
# rules (ip_forward, MASQUERADE) in place, since other sandboxes use them.
# Afterwards nothing for slot N is left, so the next sandbox can reuse it.
set -uo pipefail

N=${1:?usage: netns_delete.sh N}
[[ "$N" =~ ^[0-9]+$ ]] && (( N >= 1 && N <= 250 )) || { echo "slot must be 1-250, got: $N" >&2; exit 2; }

ip route del 10.201.0.$N 2>/dev/null || true
ip link del veth${N}h 2>/dev/null || true   # deleting one end deletes both
ip netns del sbx$N 2>/dev/null || true      # also removes tap0 and the namespace's NAT rules

# Forget the host's tracked connections for this address, so the next
# sandbox in slot N starts with none of the old sandbox's connections.
# (conntrack comes from the "conntrack" apt package; skipped if not installed.)
if command -v conntrack >/dev/null; then
  conntrack -D -s 10.201.0.$N >/dev/null 2>&1 || true
  conntrack -D -d 10.201.0.$N >/dev/null 2>&1 || true
fi

# Fail loudly if anything is still there, so destroy_sandbox keeps the slot.
if ip netns list | grep -qw "sbx$N" || ip link show veth${N}h >/dev/null 2>&1 \
   || ip route show 10.201.0.$N | grep -q .; then
  echo "slot $N network still present after delete" >&2
  exit 1
fi
