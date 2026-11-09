#!/usr/bin/env bash
set -euo pipefail
N=$1
NS=sbx$N
HOST_IFACE=$(ip -j route list default | jq -r '.[0].dev')

# 1. The namespace
ip netns add $NS

# 2. tap0 inside it (identical in every namespace)
ip netns exec $NS ip tuntap add dev tap0 mode tap
ip netns exec $NS ip addr add 172.16.0.1/30 dev tap0
ip netns exec $NS ip link set tap0 up

# 3. Virtual cable between host and namespace
ip link add veth${N}h type veth peer name veth${N}n
ip link set veth${N}n netns $NS
ip addr add 10.200.$N.1/30 dev veth${N}h
ip link set veth${N}h up
ip netns exec $NS ip addr add 10.200.$N.2/30 dev veth${N}n
ip netns exec $NS ip link set veth${N}n up
ip netns exec $NS ip link set lo up
ip netns exec $NS ip route add default via 10.200.$N.1

# 4. Inside the namespace, translate the guest's IP to this VM's unique address (both directions)
ip netns exec $NS sysctl -qw net.ipv4.ip_forward=1
ip netns exec $NS iptables -t nat -A POSTROUTING -o veth${N}n -s 172.16.0.2 -j SNAT --to 10.201.0.$N
ip netns exec $NS iptables -t nat -A PREROUTING  -i veth${N}n -d 10.201.0.$N -j DNAT --to 172.16.0.2

# 5. On the host: route the unique address into the namespace, and let it reach the internet
ip route add 10.201.0.$N via 10.200.$N.2
sysctl -qw net.ipv4.ip_forward=1
iptables -P FORWARD ACCEPT
iptables -t nat -C POSTROUTING -s 10.201.0.0/16 -o $HOST_IFACE -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s 10.201.0.0/16 -o $HOST_IFACE -j MASQUERADE
