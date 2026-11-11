#!/usr/bin/env bash
# setup_host.sh - prepare an existing Linux machine with KVM to run sandboxes
#
# Run once from the repo root after cloning:   sudo scripts/setup_host.sh
# Safe to rerun: every step skips work that is already done.
#
# Steps:
#   0. Check the machine: root, Linux, KVM, apt
#   1. Host packages (ip, iptables, curl, jq, python3, image tools)
#   2. Firecracker -> /usr/local/bin/firecracker
#   3. Kernel      -> ~/fc/vmlinux
#   4. Base image  -> ~/fc/rootfs.ext4, plus SSH key ~/fc/vm_key
#   5. Guest agent baked into the base image (scripts/install_guest_agent.sh)
#   6. Python venv -> .venv with requirements.txt
#   7. Final checklist
#
# It does NOT create the machine, clone the repo, or start the API.
#
# Settings (environment variables):
#   FC_VERSION    Firecracker release (default v1.18.0)
#   ROOTFS_SIZE   base image size (default 2G; room for npm installs)
#   FC_DIR        where kernel/image/key go (default the real user's ~/fc)
set -Eeuo pipefail

FC_VERSION=${FC_VERSION:-v1.18.0}
CI_VERSION=${CI_VERSION:-${FC_VERSION%.*}}          # v1.18.0 -> v1.18 (Firecracker's CI image folder)
ROOTFS_SIZE=${ROOTFS_SIZE:-2G}
ARCH=$(uname -m)                                    # x86_64 or aarch64
S3=https://s3.amazonaws.com/spec.ccfc.min

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REAL_USER=${SUDO_USER:-$(id -un)}
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
FC_DIR=${FC_DIR:-$REAL_HOME/fc}

die()  { echo "error: $*" >&2; exit 1; }
log()  { echo "==> $*"; }
skip() { echo "    already done: $*"; }

# ---------------------------------------------------------------- 0. checks

step0_check_machine() {
  log "0. checking the machine"
  (( EUID == 0 ))                 || die "run as root: sudo $0"
  [[ $(uname -s) == Linux ]]      || die "needs Linux"
  [[ $ARCH == x86_64 || $ARCH == aarch64 ]] || die "unsupported CPU architecture: $ARCH"
  command -v apt-get >/dev/null   || die "needs a Debian/Ubuntu system (apt-get)"
  [[ -e /dev/kvm ]] || die "/dev/kvm missing: this machine can't run VMs.
       On a cloud VM, enable nested virtualization (GCP) or use a bare-metal instance (AWS)."
  [[ -r /dev/kvm && -w /dev/kvm ]] || die "/dev/kvm exists but isn't usable"
  [[ -f $REPO/scripts/netns.sh ]] || die "run this from the cloned repo (scripts/netns.sh not found)"
}

# ---------------------------------------------------------------- 1. packages

step1_packages() {
  log "1. host packages"
  local pkgs=(iproute2 iptables curl jq python3 python3-venv squashfs-tools e2fsprogs openssh-client)
  local missing=()
  for p in "${pkgs[@]}"; do dpkg -s "$p" &>/dev/null || missing+=("$p"); done
  if (( ${#missing[@]} == 0 )); then skip "all packages installed"; return; fi
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
}

# ---------------------------------------------------------------- 2. firecracker

step2_firecracker() {
  log "2. firecracker $FC_VERSION"
  if firecracker --version 2>/dev/null | grep -q "${FC_VERSION#v}"; then skip "$(firecracker --version | head -1)"; return; fi
  local tmp; tmp=$(mktemp -d)
  curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VERSION/firecracker-$FC_VERSION-$ARCH.tgz" \
    | tar -xz -C "$tmp"
  install -m 0755 "$tmp/release-$FC_VERSION-$ARCH/firecracker-$FC_VERSION-$ARCH" /usr/local/bin/firecracker
  rm -rf "$tmp"
}

# ---------------------------------------------------------------- 3-4. kernel and base image

# Newest object in Firecracker's CI bucket whose name matches a pattern.
latest_ci_key() {
  curl -fsSL "$S3/?prefix=firecracker-ci/$CI_VERSION/$ARCH/$1&list-type=2" \
    | grep -oP "(?<=<Key>)firecracker-ci/$CI_VERSION/$ARCH/$2(?=</Key>)" | sort -V | tail -1
}

step3_kernel() {
  log "3. kernel -> $FC_DIR/vmlinux"
  mkdir -p "$FC_DIR"
  if [[ -s $FC_DIR/vmlinux ]]; then skip "vmlinux exists"; return; fi
  local key; key=$(latest_ci_key vmlinux- 'vmlinux-[0-9]+\.[0-9]+\.[0-9]{1,3}')
  [[ -n $key ]] || die "no kernel found for $CI_VERSION/$ARCH (set CI_VERSION, e.g. CI_VERSION=v1.13)"
  curl -fsSL -o "$FC_DIR/vmlinux" "$S3/$key"
}

step4_rootfs() {
  log "4. base image -> $FC_DIR/rootfs.ext4 ($ROOTFS_SIZE), SSH key -> $FC_DIR/vm_key"
  if [[ -s $FC_DIR/rootfs.ext4 && -s $FC_DIR/vm_key ]]; then skip "rootfs.ext4 and vm_key exist"; return; fi
  local key tmp
  key=$(latest_ci_key ubuntu- 'ubuntu-[0-9]+\.[0-9]+\.squashfs')
  [[ -n $key ]] || die "no Ubuntu image found for $CI_VERSION/$ARCH (set CI_VERSION, e.g. CI_VERSION=v1.13)"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/image.squashfs" "$S3/$key"
  unsquashfs -q -d "$tmp/root" "$tmp/image.squashfs"

  rm -f "$FC_DIR/vm_key" "$FC_DIR/vm_key.pub"
  ssh-keygen -q -t ed25519 -N "" -f "$FC_DIR/vm_key"
  mkdir -p "$tmp/root/root/.ssh"
  cp "$FC_DIR/vm_key.pub" "$tmp/root/root/.ssh/authorized_keys"
  chown -R root:root "$tmp/root"

  rm -f "$FC_DIR/rootfs.ext4"
  truncate -s "$ROOTFS_SIZE" "$FC_DIR/rootfs.ext4"
  mkfs.ext4 -q -d "$tmp/root" -F "$FC_DIR/rootfs.ext4"
  rm -rf "$tmp"
  chown "$REAL_USER": "$FC_DIR/vm_key" "$FC_DIR/vm_key.pub"   # so the user can ssh without sudo
}

# ---------------------------------------------------------------- 5. guest agent

step5_guest_agent() {
  log "5. guest agent -> base image"
  sed -i 's/\r$//' "$REPO"/scripts/*.sh          # in case the repo was checked out with Windows line endings
  chmod +x "$REPO"/scripts/*.sh
  FC_DIR=$FC_DIR "$REPO/scripts/install_guest_agent.sh" install
}

# ---------------------------------------------------------------- 6. python

step6_venv() {
  log "6. python venv -> $REPO/.venv"
  [[ -d $REPO/.venv ]] || python3 -m venv "$REPO/.venv"
  "$REPO/.venv/bin/pip" install -q --upgrade pip
  "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
  chown -R "$REAL_USER": "$REPO/.venv"           # so the user can pip install without sudo
}

# ---------------------------------------------------------------- 7. checklist

FAILS=0
check() { local d=$1; shift
  if "$@" &>/dev/null; then echo "  [x] $d"; else echo "  [ ] $d"; FAILS=$((FAILS + 1)); fi; }

step7_checklist() {
  log "7. checklist"
  check "KVM usable"                              test -w /dev/kvm
  check "firecracker on root's PATH"              command -v firecracker
  check "kernel at $FC_DIR/vmlinux"               test -s "$FC_DIR/vmlinux"
  check "base image at $FC_DIR/rootfs.ext4"       test -s "$FC_DIR/rootfs.ext4"
  check "SSH key at $FC_DIR/vm_key"               test -s "$FC_DIR/vm_key"
  check "guest agent in the base image"           env FC_DIR="$FC_DIR" "$REPO/scripts/install_guest_agent.sh" verify
  check "python venv with dependencies"           "$REPO/.venv/bin/python" -c "import fastapi, httpx, pydantic"
  if (( FAILS )); then die "$FAILS check(s) failed"; fi
  echo
  echo "Host ready. Start the API with:"
  echo "  sudo .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000"
}

step0_check_machine
step1_packages
step2_firecracker
step3_kernel
step4_rootfs
step5_guest_agent
step6_venv
step7_checklist
