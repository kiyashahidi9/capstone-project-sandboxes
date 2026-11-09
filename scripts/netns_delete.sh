#!/usr/bin/env bash
N=$1
ip route del 10.201.0.$N 2>/dev/null || true
ip link del veth${N}h 2>/dev/null || true   # deleting one end deletes both
ip netns del sbx$N 2>/dev/null || true
