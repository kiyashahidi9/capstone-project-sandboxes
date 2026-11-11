#!/usr/bin/env bash
# setup_host.sh - prepare a Linux machine with KVM to run sandboxes (Milestone 1a + the rest of setup)
#
# Run once from the repo root:   sudo scripts/setup_host.sh
# Safe to rerun: every step skips work that's already done.
# Rebuild the base image from scratch:   sudo scripts/setup_host.sh --rebuild-rootfs
#
# Steps:
#   1. Check the machine (root, Linux, apt)
#   2. Host packages
#   3. KVM works (kvm-ok)
#   4. cgroups v2
#   5. Firecracker -> /usr/local/bin/firecracker (pinned: everyone uses the same version)
#   6. Kernel -> ~/fc/vmlinux                                              (step 1b)
#      Base image -> ~/fc/rootfs.ext4: Ubuntu 24.04 + python3, git, curl,
#      nodejs, npm (4 GB), SSH key -> ~/fc/vm_key                           (step 1b)
#   7. Guest agent baked into the image   (scripts/install_guest_agent.sh, step 1e)
#   8. Python venv -> .venv
#   9. Checklist
#
# Settings (environment variables):
#   FC_VERSION      Firecracker release (default v1.18.0; "latest" picks the newest)
#   CI_VERSION      Firecracker image folder, e.g. v1.11 (default: from the Firecracker version)
#   KERNEL_SERIES   kernel version prefix (default 5.10)
#   ROOTFS_SIZE     base image size (default 4G, room for npm install)
set -Eeuo pipefail

FC_VERSION=${FC_VERSION:-v1.18.0}
ARCH=$(uname -m)
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REAL_USER=${SUDO_USER:-$(id -un)}
export FC_DIR=${FC_DIR:-$(getent passwd "$REAL_USER" | cut -d: -f6)/fc}
KERNEL_SERIES=${KERNEL_SERIES:-5.10}
ROOTFS_SIZE=${ROOTFS_SIZE:-4G}
S3=https://s3.amazonaws.com/spec.ccfc.min
REBUILD_ROOTFS=false; [[ ${1:-} == --rebuild-rootfs ]] && REBUILD_ROOTFS=true

TMP=""
cleanup() {
  [[ -n $TMP ]] || return 0
  mountpoint -q "$TMP/root/proc" && umount "$TMP/root/proc"
  rm -rf "$TMP"
}
trap cleanup EXIT

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

log "1. checking the machine"
(( EUID == 0 ))                 || die "run as root: sudo $0"
[[ $(uname -s) == Linux ]]      || die "needs Linux"
command -v apt-get >/dev/null   || die "needs Debian/Ubuntu (apt-get)"
[[ -f $REPO/scripts/netns.sh ]] || die "run this from the cloned repo"
sed -i 's/\r$//' "$REPO"/scripts/*.sh            # in case of Windows line endings
chmod +x "$REPO"/scripts/*.sh

log "2. host packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  curl wget jq git squashfs-tools e2fsprogs iproute2 iptables \
  python3 python3-venv python3-pip cpu-checker

log "3. KVM"
kvm-ok >/dev/null || die "KVM not available. On GCP, recreate the VM with --enable-nested-virtualization
       on an Intel machine type such as N2."

log "4. cgroups v2"
[[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs ]] || die "/sys/fs/cgroup is not cgroups v2 (use Ubuntu 22.04 or newer)"

log "5. firecracker $FC_VERSION"
if [[ $FC_VERSION == latest ]]; then
  FC_VERSION=$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
    https://github.com/firecracker-microvm/firecracker/releases/latest)")
fi
if firecracker --version 2>/dev/null | grep -q "Firecracker $FC_VERSION"; then
  echo "    already installed"
else
  tmp=$(mktemp -d)
  curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VERSION/firecracker-$FC_VERSION-$ARCH.tgz" \
    | tar -xz -C "$tmp"
  install -m 0755 "$tmp/release-$FC_VERSION-$ARCH/firecracker-$FC_VERSION-$ARCH" /usr/local/bin/firecracker
  rm -rf "$tmp"
fi

CI_VERSION=${CI_VERSION:-v1.11}       
CI=$S3/firecracker-ci/$CI_VERSION/$ARCH
mkdir -p "$FC_DIR"

log "6. kernel -> $FC_DIR/vmlinux"
if [[ -s $FC_DIR/vmlinux ]]; then
  echo "    already there"
else
  key=$(curl -fsSL "$S3/?prefix=firecracker-ci/$CI_VERSION/$ARCH/vmlinux-$KERNEL_SERIES&list-type=2" \
        | grep -oP "(?<=<Key>)firecracker-ci/$CI_VERSION/$ARCH/vmlinux-${KERNEL_SERIES//./[.]}[.][0-9]{1,3}(?=</Key>)" \
        | sort -V | tail -1 || true)
  [[ -n $key ]] || die "no vmlinux-$KERNEL_SERIES in $CI_VERSION/$ARCH. Check docs/getting-started.md for your
       Firecracker version and rerun with CI_VERSION=<folder> (e.g. v1.11) or KERNEL_SERIES=<version>."
  curl -fsSL -o "$FC_DIR/vmlinux" "$S3/$key"
fi

log "6. base image -> $FC_DIR/rootfs.ext4"
if [[ -s $FC_DIR/rootfs.ext4 ]] && ! $REBUILD_ROOTFS; then
  echo "    already there (rebuild with --rebuild-rootfs)"
else
  TMP=$(mktemp -d)
  echo "    downloading Ubuntu 24.04"
  curl -fsSL -o "$TMP/ubuntu.squashfs" "$CI/ubuntu-24.04.squashfs" \
    || die "no ubuntu-24.04.squashfs in $CI_VERSION/$ARCH (rerun with CI_VERSION=<folder>, see docs/getting-started.md)"
  unsquashfs -q -d "$TMP/root" "$TMP/ubuntu.squashfs"

  echo "    installing python3, git, curl, nodejs, npm"
  rm -f "$TMP/root/etc/resolv.conf"
  echo "nameserver 8.8.8.8" > "$TMP/root/etc/resolv.conf"     # DNS for the VM, and for apt below
  mount -t proc /proc "$TMP/root/proc"
  chroot "$TMP/root" /bin/bash -c \
    "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 git curl nodejs npm"
  umount "$TMP/root/proc"

  echo "    adding SSH key $FC_DIR/vm_key"
  [[ -s $FC_DIR/vm_key ]] || ssh-keygen -q -f "$FC_DIR/vm_key" -N ""
  mkdir -p "$TMP/root/root/.ssh"
  cp "$FC_DIR/vm_key.pub" "$TMP/root/root/.ssh/authorized_keys"
  chown "$REAL_USER": "$FC_DIR/vm_key" "$FC_DIR/vm_key.pub"    # so you can ssh without sudo

  echo "    packing into $ROOTFS_SIZE image"
  chown -R root:root "$TMP/root"
  rm -f "$FC_DIR/rootfs.ext4"
  truncate -s "$ROOTFS_SIZE" "$FC_DIR/rootfs.ext4"
  mkfs.ext4 -q -d "$TMP/root" -F "$FC_DIR/rootfs.ext4"
  cleanup; TMP=""
fi

log "7. guest agent"
"$REPO/scripts/install_guest_agent.sh" install

log "8. python venv"
[[ -d $REPO/.venv ]] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
chown -R "$REAL_USER": "$REPO/.venv"             # so you can pip install without sudo

log "9. checklist"
FAILS=0
check() { local d=$1; shift
  if "$@" &>/dev/null; then echo "  [x] $d"; else echo "  [ ] $d"; FAILS=$((FAILS + 1)); fi; }
check "kvm-ok passes"                          kvm-ok
check "cgroups v2"                             test "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs
check "firecracker $FC_VERSION on PATH"        bash -c "firecracker --version | grep -q 'Firecracker $FC_VERSION'"
check "kernel at $FC_DIR/vmlinux"              test -s "$FC_DIR/vmlinux"
check "base image at $FC_DIR/rootfs.ext4"      test -s "$FC_DIR/rootfs.ext4"
check "SSH key at $FC_DIR/vm_key"              test -s "$FC_DIR/vm_key"
check "guest agent in the base image"          "$REPO/scripts/install_guest_agent.sh" verify
check "python venv with dependencies"          "$REPO/.venv/bin/python" -c "import fastapi, httpx, pydantic"
(( FAILS == 0 )) || die "$FAILS check(s) failed"

echo
echo "Host ready. Start the API with:"
echo "  sudo .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000"
