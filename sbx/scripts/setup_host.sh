#!/usr/bin/env bash
# setup_host.sh - prepare a Linux machine with KVM to run sandboxes (Milestones 1a + 1b, plus 1e's install)
#
# Run once from the repo root:   sudo sbx/scripts/setup_host.sh
# Safe to rerun: every step skips work that's already done.
# Rebuild the base image from scratch:   sudo sbx/scripts/setup_host.sh --rebuild-rootfs
#
# Steps:
#   1. Check the machine and the repo
#   2. Host packages
#   3. KVM works (kvm-ok)
#   4. cgroups v2
#   5. Firecracker -> /usr/local/bin/firecracker (pinned: everyone uses the same version)
#   6. Kernel -> ~/fc/vmlinux
#   7. Base image -> ~/fc/rootfs.ext4: Ubuntu 24.04 + python3, git, curl, nodejs, npm;
#      SSH key -> ~/fc/vm_key
#   8. Guest agent baked into the image (install_guest_agent.sh, next to this script)
#   9. Python venv -> .venv in the repo root
#  10. Checklist
#
# Settings (environment variables):
#   FC_VERSION      Firecracker release (default v1.18.0; "latest" picks the newest)
#   CI_VERSION      Firecracker image folder (default v1.11, see docs/getting-started.md)
#   KERNEL_SERIES   kernel version prefix (default 5.10)
#   ROOTFS_SIZE     base image size (default 4G, room for npm install)
set -Eeuo pipefail

FC_VERSION=${FC_VERSION:-v1.18.0}
CI_VERSION=${CI_VERSION:-v1.11}
KERNEL_SERIES=${KERNEL_SERIES:-5.10}
ROOTFS_SIZE=${ROOTFS_SIZE:-4G}
ARCH=$(uname -m)
S3=https://s3.amazonaws.com/spec.ccfc.min
CI=$S3/firecracker-ci/$CI_VERSION/$ARCH
SCRIPTS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)                      # the folder holding this script
REPO=$(git -C "$SCRIPTS" rev-parse --show-toplevel 2>/dev/null || (cd "$SCRIPTS/../.." && pwd))   # repo root
REAL_USER=${SUDO_USER:-$(id -un)}
export FC_DIR=${FC_DIR:-$(getent passwd "$REAL_USER" | cut -d: -f6)/fc}
REBUILD_ROOTFS=false; [[ ${1:-} == --rebuild-rootfs ]] && REBUILD_ROOTFS=true

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

# ---------------------------------------------------------------- temp build folder

# The base image is built in a temp folder with /proc, /sys and /dev mounted into it.
# Cleanup must unmount them BEFORE deleting the folder: deleting a mounted /dev
# would delete the host's real device files.
TMP=""
CHROOT_MOUNTS=(proc sys dev/pts dev)          # unmount order: innermost first

unmount_chroot() {
  local m
  for m in "${CHROOT_MOUNTS[@]}"; do
    mountpoint -q "$TMP/root/$m" && umount -R "$TMP/root/$m"
  done
  return 0
}

cleanup() {
  [[ -n $TMP ]] || return 0
  unmount_chroot || true
  if findmnt -rn -o TARGET | grep -q "^$TMP/"; then
    echo "warning: something is still mounted under $TMP; not deleting it" >&2
    return 0
  fi
  rm -rf "$TMP"
  TMP=""
}
trap cleanup EXIT

# ---------------------------------------------------------------- 1-5: the host

log "1. checking the machine and the repo"
(( EUID == 0 ))               || die "run as root: sudo $0"
[[ $(uname -s) == Linux ]]    || die "needs Linux"
command -v apt-get >/dev/null || die "needs Debian/Ubuntu (apt-get)"
for f in "$SCRIPTS/netns.sh" "$SCRIPTS/install_guest_agent.sh" "$SCRIPTS/../guest_agent/guest_agent.py" "$REPO/requirements.txt"; do
  [[ -f $f ]] || die "missing $(realpath -m "$f"): run this from a complete clone of the repo"
done
# Windows line endings break bash scripts; fix only files that have them.
grep -l $'\r' "$SCRIPTS"/*.sh 2>/dev/null | xargs -r sed -i 's/\r$//' || true
chmod +x "$SCRIPTS"/*.sh

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
  TMP=$(mktemp -d)
  curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VERSION/firecracker-$FC_VERSION-$ARCH.tgz" \
    | tar -xz -C "$TMP"
  install -m 0755 "$TMP/release-$FC_VERSION-$ARCH/firecracker-$FC_VERSION-$ARCH" /usr/local/bin/firecracker
  cleanup
fi

mkdir -p "$FC_DIR"
chown "$REAL_USER": "$FC_DIR"                    # yours, not root's

# ---------------------------------------------------------------- 6-7: kernel and base image
# Both are written to a .tmp file and renamed only when complete, so a failed
# run never leaves a broken file that the next run would skip as "already there".

log "6. kernel -> $FC_DIR/vmlinux"
if [[ -s $FC_DIR/vmlinux ]]; then
  echo "    already there"
else
  key=$(curl -fsSL "$S3/?prefix=firecracker-ci/$CI_VERSION/$ARCH/vmlinux-$KERNEL_SERIES&list-type=2" \
        | grep -oP "(?<=<Key>)firecracker-ci/$CI_VERSION/$ARCH/vmlinux-${KERNEL_SERIES//./[.]}[.][0-9]{1,3}(?=</Key>)" \
        | sort -V | tail -1 || true)
  [[ -n $key ]] || die "no vmlinux-$KERNEL_SERIES in $CI_VERSION/$ARCH. Check docs/getting-started.md and
       rerun with CI_VERSION=<folder> or KERNEL_SERIES=<version>."
  curl -fsSL -o "$FC_DIR/vmlinux.tmp" "$S3/$key"
  mv "$FC_DIR/vmlinux.tmp" "$FC_DIR/vmlinux"
fi

log "7. base image -> $FC_DIR/rootfs.ext4"
if [[ -s $FC_DIR/rootfs.ext4 ]] && ! $REBUILD_ROOTFS; then
  echo "    already there (rebuild with --rebuild-rootfs)"
else
  TMP=$(mktemp -d)
  ROOT=$TMP/root

  echo "    downloading Ubuntu 24.04"
  curl -fsSL -o "$TMP/ubuntu.squashfs" "$CI/ubuntu-24.04.squashfs" \
    || die "no ubuntu-24.04.squashfs in $CI_VERSION/$ARCH (rerun with CI_VERSION=<folder>, see docs/getting-started.md)"
  unsquashfs -q -d "$ROOT" "$TMP/ubuntu.squashfs"

  echo "    installing python3, git, curl, nodejs, npm"
  rm -f "$ROOT/etc/resolv.conf"
  echo "nameserver 8.8.8.8" > "$ROOT/etc/resolv.conf"    # DNS for the VM, and for apt below
  mkdir -p "$ROOT/tmp" && chmod 1777 "$ROOT/tmp"         # apt needs a writable /tmp
  mkdir -p "$ROOT/var/cache/apt/archives/partial" "$ROOT/var/lib/apt/lists/partial"   # apt's download folders
  printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"   # don't start services in the chroot
  chmod +x "$ROOT/usr/sbin/policy-rc.d"
  mkdir -p "$ROOT/proc" "$ROOT/sys" "$ROOT/dev"
  mount -t proc proc "$ROOT/proc"
  mount --rbind /sys "$ROOT/sys" && mount --make-rslave "$ROOT/sys"
  mount --bind /dev "$ROOT/dev"
  mkdir -p "$ROOT/dev/pts" && mount --bind /dev/pts "$ROOT/dev/pts"
  chroot "$ROOT" /bin/bash -c "apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 git curl nodejs npm \
    && apt-get clean && rm -rf /var/lib/apt/lists/*"
  unmount_chroot
  rm -f "$ROOT/usr/sbin/policy-rc.d"

  echo "    adding SSH key $FC_DIR/vm_key"
  [[ -s $FC_DIR/vm_key ]] || ssh-keygen -q -f "$FC_DIR/vm_key" -N ""
  chown "$REAL_USER": "$FC_DIR/vm_key" "$FC_DIR/vm_key.pub"   # so you can ssh without sudo
  mkdir -p "$ROOT/root/.ssh"
  cp "$FC_DIR/vm_key.pub" "$ROOT/root/.ssh/authorized_keys"

  echo "    packing into a $ROOTFS_SIZE image"
  chown -R root:root "$ROOT"
  rm -f "$FC_DIR/rootfs.ext4.tmp"
  truncate -s "$ROOTFS_SIZE" "$FC_DIR/rootfs.ext4.tmp"
  mkfs.ext4 -q -d "$ROOT" -F "$FC_DIR/rootfs.ext4.tmp"
  mv "$FC_DIR/rootfs.ext4.tmp" "$FC_DIR/rootfs.ext4"
  cleanup
fi

# ---------------------------------------------------------------- 8-9: guest agent and python

log "8. guest agent"
"$SCRIPTS/install_guest_agent.sh" install

log "9. python venv"
[[ -d $REPO/.venv ]] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
chown -R "$REAL_USER": "$REPO/.venv"             # so you can pip install without sudo

# ---------------------------------------------------------------- 10: checklist

log "10. checklist"
FAILS=0
check() { local d=$1; shift
  if "$@" &>/dev/null; then echo "  [x] $d"; else echo "  [ ] $d"; FAILS=$((FAILS + 1)); fi; }
check "kvm-ok passes"                          kvm-ok
check "cgroups v2"                             test "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs
check "firecracker $FC_VERSION on PATH"        bash -c "firecracker --version | grep -q 'Firecracker $FC_VERSION'"
check "kernel at $FC_DIR/vmlinux"              test -s "$FC_DIR/vmlinux"
check "base image at $FC_DIR/rootfs.ext4"      test -s "$FC_DIR/rootfs.ext4"
check "SSH key at $FC_DIR/vm_key"              test -s "$FC_DIR/vm_key"
check "guest agent in the base image"          "$SCRIPTS/install_guest_agent.sh" verify
check "python venv with dependencies"          "$REPO/.venv/bin/python" -c "import fastapi, httpx, pydantic"
(( FAILS == 0 )) || die "$FAILS check(s) failed"

echo
echo "Host ready. Start the API with:"
echo "  sudo .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000"
