# Project Plan - MVP

# Persistent Sandboxes: Build Plan

This README is our build order. Follow it top to bottom. **Do not skip ahead.** Each step depends on the one before it working.

**The MVP is Milestone 1:** we start a sandbox on Firecracker, and a client connects to it with their agent and does real work inside it.

**Where the project ends up (Milestones 2–6):** when the agent is idle, we pause the sandbox (saving memory + disk) and resume it later with all its work intact.

| Milestone | What we have at the end |
| -- | -- |
| **1. Sandbox on Firecracker (MVP)** | We start a sandbox on Firecracker, and a client connects to it with their agent and does real work, driven from a terminal client app |
| **2. Idle detection** | For every sandbox, we know whether the agent is working, waiting or idle |
| **3. Pause & resume** | We can pause a sandbox and resume it with memory + disk intact |
| **4. Auto pause & resume** | Idle sandboxes pause on their own and wake up on the next message |
| **5. Reconnect** | Clients that disconnected catch up on exactly what they missed |
| **6. Durable snapshots** | Snapshots survive losing the machine |

### How to read each step

* **Do:** the exact commands or code to write.
* **You should see:** what success looks like.
* **If it fails:** the most common cause.
* Write everything you learn (errors, fixes, surprises) in `NOTES.md`. It becomes part of our write-up.

---

## The big picture

Our architecture diagram has three colors. They are three layers:

| Color | What it is | Analogy | Who writes it |
| -- | -- | -- | -- |
| **Grey** | Firecracker itself | The engine | Nobody. We install it and use it. |
| **Teal** | The node agent (code that tells Firecracker to start, pause, snapshot and resume a VM) and the guest agent (a small server inside each VM) | The pedals | Us, a smaller version of E2B's orchestrator and `envd` |
| **Blue** | Activity sampler, signal intake, classifier, pause decider, event broker | The driver | Us, from scratch |

Words we use in this README:

| Word | Means |
| -- | -- |
| **Host** | The Linux machine (our GCP VM) that runs Firecracker |
| **Guest** / **VM** / **sandbox** | One Firecracker microVM running on the host |
| **Guest agent** | Our small HTTP server running *inside* each VM |
| **Node agent** | Our Python code on the host that controls Firecracker |
| **Agent** / **harness** | The coding agent (LLM loop). Runs *outside* the VM and uses the sandbox through our API |
| **Client app** | The terminal app a human uses to give the agent tasks |

> **Team rule:** no blue-box code gets merged until Milestone 1's acceptance test passes.

### Tools we use

* **Python 3.11+** on the host.
* `httpx` to talk to Firecracker. Firecracker is controlled by plain HTTP over a Unix socket, so we don't need an SDK:

  ```
  import httpxfc = httpx.Client(transport=httpx.HTTPTransport(uds="/tmp/sbx1.sock"), base_url="http://localhost")fc.put("/actions", json={"action_type": "InstanceStart"}).raise_for_status()
  
  ```
* **FastAPI + uvicorn** for our API server.
* `subprocess.run([...], check=True)` for Linux setup commands (network, cgroups, copying files).
* `asyncio` for background loops (Milestones 2 and 4).
* **Reference code to read** (for the steps, not to copy): E2B's orchestrator and `envd` (Go, the model project), and **BandSox** (Python, Firecracker with snapshot/restore).

> **Firecracker versions matter.** API fields and download paths change between versions. Everyone uses the same version, and when a command here doesn't match, the official docs for *our* version win. Key docs in the Firecracker repo: `docs/getting-started.md`, `docs/network-setup.md`, `docs/snapshotting/snapshot-support.md`, `docs/snapshotting/network-for-clones.md`.

---

# Milestone 1: Sandbox on Firecracker (MVP)

**Goal:** our system starts a sandbox on Firecracker, and a client connects to it with their agent and does real work inside it.

Work through 1a → 1j in order.

## 1a. Set up the host machine

Firecracker needs **KVM** (hardware virtualization). A normal cloud VM doesn't expose KVM, so on GCP we must turn on **nested virtualization**.

**Do: create the GCP VM** (use an Intel machine type such as N2; nested virtualization is best supported there):

```bash
gcloud compute instances create sbx-node-1 \
  --zone=us-east1-b \
  --machine-type=n2-standard-8 \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=200GB \
  --enable-nested-virtualization

gcloud compute ssh sbx-node-1 --zone=us-east1-b
```

**Do: check KVM works** (on the host):

```bash
ls -l /dev/kvm
sudo apt-get update && sudo apt-get install -y cpu-checker && kvm-ok
```

**You should see:** `/dev/kvm` exists, and `kvm-ok` prints `KVM acceleration can be used`. **If it fails:** the VM was created without `--enable-nested-virtualization`, or on a machine type that doesn't support it. Recreate it.

**Do: install host packages:**

```bash
sudo apt-get install -y curl wget jq git squashfs-tools e2fsprogs iproute2 iptables \
  python3 python3-venv python3-pip
```

**Do: check cgroups v2** (Milestone 2's sampler reads CPU usage from it):

```bash
stat -fc %T /sys/fs/cgroup
```

**You should see:** `cgroup2fs`.

**Do: download Firecracker:**

```bash
mkdir -p ~/fc && cd ~/fc
ARCH="$(uname -m)"
release_url="https://github.com/firecracker-microvm/firecracker/releases"
latest=$(basename $(curl -fsSLI -o /dev/null -w %{url_effective} ${release_url}/latest))
curl -L ${release_url}/download/${latest}/firecracker-${latest}-${ARCH}.tgz | tar -xz
sudo mv release-${latest}-${ARCH}/firecracker-${latest}-${ARCH} /usr/local/bin/firecracker
firecracker --version
```

**You should see:** a version like `Firecracker v1.x.y`. **Write it in** `NOTES.md`; everyone must use the same one. (After the first person installs it, everyone else downloads that exact version instead of `latest`.)

### Done when

- [X] kvm-ok passes and firecracker --version works on the host.
- [X] These commands are saved in scripts/setup_host.sh so anyone can rebuild the host.

## 1b. Get a kernel and build a root filesystem

A VM needs two files:

* `vmlinux`: the Linux kernel the VM boots.
* `rootfs.ext4`: the VM’s hard drive (an Ubuntu filesystem in one file).

Firecracker publishes test images we can use. **Check** `docs/getting-started.md` for our version: the folder in the URL (`v1.11` below) changes with each release.

**Do: download the kernel and an Ubuntu image:**

```bash
cd ~/fc
ARCH="$(uname -m)"
CI_VERSION="v1.11"   # match our Firecracker version; see getting-started.md

# Kernel
latest_kernel=$(wget "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/${CI_VERSION}/${ARCH}/vmlinux-5.10&list-type=2" -O - 2>/dev/null \
  | grep -oP "(?<=<Key>)(firecracker-ci/${CI_VERSION}/${ARCH}/vmlinux-5\.10\.[0-9]{1,3})(?=</Key>)")
wget -O vmlinux "https://s3.amazonaws.com/spec.ccfc.min/${latest_kernel}"

# Ubuntu filesystem (compressed)
wget -O ubuntu.squashfs "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/${CI_VERSION}/${ARCH}/ubuntu-24.04.squashfs"
sudo rm -rf rootfs-dir
sudo unsquashfs -d rootfs-dir ubuntu.squashfs
```

**To verify, run the below command and you should only see Green flags:**

```bash
cd ~/fc || exit 1

echo "=== Firecracker Milestone 1b Verification ==="

# 1. Verify kernel
if [ -s vmlinux ] && file vmlinux | grep -q "ELF 64-bit"; then
  echo "✅ Guest kernel downloaded and valid ELF"
else
  echo "❌ Guest kernel missing or invalid"
fi

# 2. Verify kernel architecture
echo "Host architecture: $(uname -m)"
file vmlinux

# 3. Verify SquashFS
if [ -s ubuntu.squashfs ] && unsquashfs -s ubuntu.squashfs >/dev/null 2>&1; then
  echo "✅ Ubuntu SquashFS image valid"
else
  echo "❌ Ubuntu SquashFS missing or invalid"
fi

# 4. Verify extracted Ubuntu
if [ -f rootfs-dir/etc/os-release ] &&
   grep -q '^VERSION_ID="24.04"' rootfs-dir/etc/os-release; then
  echo "✅ Ubuntu 24.04 filesystem extracted"
else
  echo "❌ Ubuntu filesystem missing or incorrect version"
fi

# 5. Verify directory contents
echo
echo "=== Downloaded Files ==="
ls -lh vmlinux ubuntu.squashfs

echo
echo "=== Ubuntu Version ==="
cat rootfs-dir/etc/os-release
```


**Do: prepare the image for** `apt-get`. The CI image is stripped down, so it needs DNS, a writable `/tmp`, apt’s cache folders, and the host’s `/proc`, `/dev` and `/sys`:

```bash
sudo rm -f rootfs-dir/etc/resolv.conf
echo "nameserver 8.8.8.8" | sudo tee rootfs-dir/etc/resolv.conf

sudo mkdir -p rootfs-dir/tmp rootfs-dir/var/cache/apt/archives/partial rootfs-dir/var/log/apt
sudo chmod 1777 rootfs-dir/tmp

sudo mount -t proc /proc rootfs-dir/proc
sudo mount --bind /dev rootfs-dir/dev
sudo mount --bind /dev/pts rootfs-dir/dev/pts
sudo mount --bind /sys rootfs-dir/sys
```


**To verify, run the command below:**

```
cd ~/fc || exit 1

echo "=== Guest Filesystem Verification ==="

# 1. DNS configuration
grep -q "nameserver 8.8.8.8" rootfs-dir/etc/resolv.conf \
  && echo "✅ DNS configured" \
  || echo "❌ DNS configuration missing"

# 2. /tmp permissions
[ "$(stat -c '%a' rootfs-dir/tmp)" = "1777" ] \
  && echo "✅ /tmp permissions correct (1777)" \
  || echo "❌ /tmp permissions incorrect"

# 3. APT directories
for dir in \
  rootfs-dir/var/cache/apt/archives/partial \
  rootfs-dir/var/log/apt
do
  [ -d "$dir" ] \
    && echo "✅ $dir exists" \
    || echo "❌ $dir missing"
done

# 4. Verify mounts
for dir in proc dev dev/pts sys
do
  if mountpoint -q "rootfs-dir/$dir"; then
    echo "✅ /$dir mounted"
  else
    echo "❌ /$dir NOT mounted"
  fi
done

# 5. Verify /dev/null
if sudo chroot rootfs-dir /bin/bash -c 'echo test > /dev/null'; then
  echo "✅ /dev/null accessible inside chroot"
else
  echo "❌ /dev/null inaccessible"
fi
```

**Do: install what the agent will need** (Python for our guest agent; git and Node for the coding tasks; CA certificates for HTTPS). Run `update` and `install` separately so a failed update can’t silently skip the install:

```bash
sudo chroot rootfs-dir /bin/bash -c "apt-get update"
sudo chroot rootfs-dir /bin/bash -c "apt-get install -y --no-install-recommends ca-certificates python3 git curl nodejs npm"
sudo chroot rootfs-dir /bin/bash -c "apt-get clean"
```


**To verify, the installation of the git, Node for coding tasks run the command below:**

```
cd ~/fc || exit 1

echo "=== Guest Development Tools Verification ==="

for tool in python3 git node npm curl; do
  if sudo chroot rootfs-dir /bin/bash -c "command -v $tool" >/dev/null 2>&1; then
    version=$(sudo chroot rootfs-dir /bin/bash -c "$tool --version" 2>&1 | head -1)
    echo "✅ $tool installed: $version"
  else
    echo "❌ $tool NOT installed"
  fi
done

# Verify CA certificates
if sudo test -s rootfs-dir/etc/ssl/certs/ca-certificates.crt; then
  echo "✅ CA certificates installed"
else
  echo "❌ CA certificates missing"
fi
```

**Do: check the install, then unmount:**

```bash
sudo chroot rootfs-dir /bin/bash -c "python3 --version && git --version && node --version && npm --version && curl -sI https://github.com | head -1"

sudo umount rootfs-dir/sys rootfs-dir/dev/pts rootfs-dir/dev rootfs-dir/proc
mount | grep rootfs-dir
```

**You should see:** four version numbers, `HTTP/2 200`, and nothing from `mount | grep`.

> ⚠️ Always unmount before `rm -rf rootfs-dir` or `mkfs.ext4`. While mounted, `rootfs-dir/dev` is the host’s real `/dev`.

**Do: add an SSH key** (so we can log in to the VM while debugging). `-N ""` means no passphrase:

```bash
[ -f vm_key ] || ssh-keygen -f vm_key -N ""
sudo mkdir -p rootfs-dir/root/.ssh
sudo cp vm_key.pub rootfs-dir/root/.ssh/authorized_keys
sudo chmod 700 rootfs-dir/root/.ssh
sudo chmod 600 rootfs-dir/root/.ssh/authorized_keys
```

**To verify the above command for adding** `ssh`  key, run command below:

```
cd ~/fc || exit 1

echo "=== Guest SSH Configuration Verification ==="

if [ -s vm_key ] && [ -s vm_key.pub ]; then
  echo "✅ SSH private and public keys exist"
else
  echo "❌ SSH key pair missing"
fi

if sudo cmp -s vm_key.pub rootfs-dir/root/.ssh/authorized_keys; then
  echo "✅ Public key installed in guest filesystem"
else
  echo "❌ Guest authorized_keys missing or incorrect"
fi

if [ "$(sudo stat -c '%a' rootfs-dir/root/.ssh)" = "700" ]; then
  echo "✅ SSH directory permissions correct (700)"
else
  echo "❌ Incorrect SSH directory permissions"
fi

if [ "$(sudo stat -c '%a' rootfs-dir/root/.ssh/authorized_keys)" = "600" ]; then
  echo "✅ authorized_keys permissions correct (600)"
else
  echo "❌ Incorrect authorized_keys permissions"
fi
```

**Do: pack it into a 4 GB disk image** (big enough for `npm install`):

```bash
sudo chown -R root:root rootfs-dir
rm -f rootfs.ext4
truncate -s 4G rootfs.ext4
sudo mkfs.ext4 -d rootfs-dir -F rootfs.ext4
```


**To verify the final step above:**

```bash
cd ~/fc || exit 1

echo "=== Firecracker RootFS Verification ==="

if [ -s vmlinux ]; then
  echo "✅ Guest kernel exists"
else
  echo "❌ Guest kernel missing"
fi

if [ -f rootfs.ext4 ]; then
  echo "✅ Root filesystem image exists"
else
  echo "❌ Root filesystem image missing"
fi

if [ "$(stat -c %s rootfs.ext4)" -eq 4294967296 ]; then
  echo "✅ RootFS size is 4 GiB"
else
  echo "❌ RootFS size incorrect"
fi

if file rootfs.ext4 | grep -q "ext4 filesystem"; then
  echo "✅ Valid ext4 filesystem detected"
else
  echo "❌ Invalid ext4 filesystem"
fi

echo
ls -lh vmlinux rootfs.ext4
```

**You should see:** `ls -lh ~/fc/vmlinux ~/fc/rootfs.ext4` lists both files (`rootfs.ext4` is 4.0G). This `rootfs.ext4` is our **base image**; every sandbox gets its own copy of it.

#### Done when

- [ ] `scripts/build_rootfs.sh` rebuilds `vmlinux` and `rootfs.ext4` from scratch (start it with `set -euo pipefail`).

## 1c. Boot your first VM by hand

**Why by hand first:** so we understand every call before we write Python for it.

### Step 1: create a network device for the VM (on the host)

The VM gets a virtual network card. On the host side it shows up as a **tap device** (`tap0`). The host is `172.16.0.1`, the VM will be `172.16.0.2`.

```bash
sudo ip tuntap add dev tap0 mode tap
sudo ip addr add 172.16.0.1/30 dev tap0
sudo ip link set dev tap0 up

# Let the VM reach the internet through the host
sudo sh -c "echo 1 > /proc/sys/net/ipv4/ip_forward"
sudo iptables -P FORWARD ACCEPT
HOST_IFACE=$(ip -j route list default | jq -r '.[0].dev')
sudo iptables -t nat -A POSTROUTING -o "$HOST_IFACE" -j MASQUERADE
```

### Step 2: start Firecracker (terminal 1)

```bash
cd ~/fc
sudo rm -f /tmp/sbx1.sock
sudo firecracker --api-sock /tmp/sbx1.sock
```

Leave this running. **This terminal becomes the VM's console** once it boots.

### Step 3: configure and boot the VM (terminal 2)

Each `curl` sends one setting to Firecracker. **Each should return nothing (HTTP 204).** If one prints an error, read it; it usually says which field is wrong.

```bash
cd ~/fc
S=/tmp/sbx1.sock

# Which kernel to boot, and kernel options
sudo curl --unix-socket $S -X PUT http://localhost/boot-source \
  -H 'Content-Type: application/json' \
  -d "{\"kernel_image_path\":\"$(pwd)/vmlinux\",\"boot_args\":\"console=ttyS0 reboot=k panic=1 pci=off\"}"

# The VM's hard drive
sudo curl --unix-socket $S -X PUT http://localhost/drives/rootfs \
  -H 'Content-Type: application/json' \
  -d "{\"drive_id\":\"rootfs\",\"path_on_host\":\"$(pwd)/rootfs.ext4\",\"is_root_device\":true,\"is_read_only\":false}"

# CPUs and memory
sudo curl --unix-socket $S -X PUT http://localhost/machine-config \
  -H 'Content-Type: application/json' \
  -d '{"vcpu_count":2,"mem_size_mib":1024}'

# Network card, connected to tap0. This MAC makes the test image use IP 172.16.0.2.
sudo curl --unix-socket $S -X PUT http://localhost/network-interfaces/eth0 \
  -H 'Content-Type: application/json' \
  -d '{"iface_id":"eth0","guest_mac":"06:00:AC:10:00:02","host_dev_name":"tap0"}'

# Boot
sudo curl --unix-socket $S -X PUT http://localhost/actions \
  -H 'Content-Type: application/json' \
  -d '{"action_type":"InstanceStart"}'
```

**You should see:** Linux boot messages scroll in terminal 1, ending in a login prompt.

### Step 4: log in and check the network

```bash
ssh -i ~/fc/vm_key root@172.16.0.2
```

Inside the VM:

```bash
ip route add default via 172.16.0.1 dev eth0   # send internet traffic through the host
ping -c 2 8.8.8.8
curl -sI https://github.com | head -1
python3 --version && git --version && node --version
```

**You should see:** ping replies, `HTTP/2 200`, and three version numbers. **If ssh hangs:** the VM didn't get IP `172.16.0.2`. Check the `guest_mac` and that `tap0` is up (`ip addr show tap0`). **If the VM can't reach the internet:** check `ip_forward` is `1` and the MASQUERADE rule exists (`sudo iptables -t nat -L POSTROUTING`).

### Step 5: shut it down

`reboot` inside the VM (this stops Firecracker), or `Ctrl+C` in terminal 1. Then `sudo ip link del tap0`.

### Done when

- [ ] scripts/start_vm_by_hand.sh boots a VM and you can SSH in.

## 1d. Give each VM its own network

**The problem:** if two VMs both use `tap0` and `172.16.0.2`, they clash. Later (Milestone 3), a snapshot also **remembers** the tap name and IP, so every restored VM will insist on `tap0` and `172.16.0.2`.

**The fix** (from Firecracker's `network-for-clones.md`): put each VM in its own **network namespace**: a private copy of the network stack. Inside every namespace, the VM sees the exact same `tap0` and `172.16.0.2`. From the host, each VM is reached at its own unique address.

For sandbox number **N** (1, 2, 3, …):

| Thing | Value |
| -- | -- |
| Namespace name | `sbx<N>` |
| Inside the namespace: `tap0` | `172.16.0.1/30` (same for every VM) |
| Guest IP | `172.16.0.2` (same for every VM) |
| veth pair (a virtual cable between host and namespace) | host side `veth<N>h` = `10.200.<N>.1/30`, namespace side `veth<N>n` = `10.200.<N>.2/30` |
| **Unique address the host uses to reach this VM** | `10.201.0.<N>` |

**Do: write** `scripts/netns_create.sh N`:

```bash
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
```

**Do: write** `scripts/netns_delete.sh N`:

```bash
#!/usr/bin/env bash
N=$1
ip route del 10.201.0.$N 2>/dev/null || true
ip link del veth${N}h 2>/dev/null || true   # deleting one end deletes both
ip netns del sbx$N 2>/dev/null || true
```

**Do: test with two VMs at once.**

1. `sudo scripts/netns_create.sh 1` and `sudo scripts/netns_create.sh 2`.
2. Make two disk copies: `cp --sparse=always rootfs.ext4 vm1.ext4` and the same for `vm2.ext4`.
3. Start each Firecracker **inside its namespace**: `sudo ip netns exec sbx1 firecracker --api-sock /tmp/sbx1.sock` (and `sbx2`, `/tmp/sbx2.sock`).
4. Send the same 1c config calls to each socket, pointing each at its own disk. **Both use** `host_dev_name: tap0` and the same MAC.
5. From the host: `ssh -i vm_key root@10.201.0.1` and `ssh -i vm_key root@10.201.0.2`.

**You should see:** two separate VMs, both thinking they are `172.16.0.2`, reachable from the host at `10.201.0.1` and `10.201.0.2`, both able to reach the internet (they still need `ip route add default via 172.16.0.1` inside; 1e automates that).

### Done when

- [ ] Two VMs run at the same time with no network conflict.
- [ ] netns_delete.sh leaves nothing behind (ip netns list and ip link are clean).

## 1e. The guest agent (runs inside every VM)

**What it is:** a small HTTP server inside the VM. It's how everything outside the VM runs commands and reads/writes files inside it. (E2B's version is called `envd` and listens on port 49983; we use the same port.)

**Do: write** `guest_agent/guest_agent.py`. Use **only the Python standard library** (`http.server`, `subprocess`, `json`) so nothing needs installing inside the VM. Endpoints:

| Endpoint | Request | Response |
| -- | -- | -- |
| `GET /health` | — | `{"ok": true}` |
| `POST /exec` | `{"cmd": "npm install", "cwd": "/root/app", "timeout": 600}` | `{"exit_code": 0, "stdout": "...", "stderr": "..."}` |
| `POST /exec` (background) | `{"cmd": "npm run dev", "cwd": "/root/app", "background": true}` | `{"pid": 1234}` immediately. Output goes to `/tmp/bg-<pid>.log` |
| `GET /files?path=/root/app/x.js` | — | the file's contents, or 404 |
| `PUT /files?path=/root/app/x.js` | body = new contents | `{"ok": true}`. Creates parent folders if missing |

Rules:

* Run commands with `subprocess.run(cmd, shell=True, cwd=..., capture_output=True, text=True, timeout=...)`.
* For `background: true`, use `subprocess.Popen(..., start_new_session=True)` so the process keeps running after the request ends.
* Use `ThreadingHTTPServer` so a long `exec` doesn't block `/health`.
* Listen on `0.0.0.0:49983`.

**Do: bake it into the base image so it starts at boot,** and set up the VM's network at boot too:

```
cd ~/fc
sudo mkdir -p /mnt/rootfs
sudo mount -o loop rootfs.ext4 /mnt/rootfs

sudo mkdir -p /mnt/rootfs/opt/guest_agent
sudo cp ~/<repo>/guest_agent/guest_agent.py /mnt/rootfs/opt/guest_agent/

sudo tee /mnt/rootfs/etc/systemd/system/guest-agent.service > /dev/null <<'EOF'
[Unit]
Description=Sandbox guest agent
After=network.target

[Service]
# Network setup (the leading "-" means: ignore the error if it's already set)
ExecStartPre=-/sbin/ip addr add 172.16.0.2/30 dev eth0
ExecStartPre=-/sbin/ip link set eth0 up
ExecStartPre=-/sbin/ip route add default via 172.16.0.1 dev eth0
ExecStart=/usr/bin/python3 /opt/guest_agent/guest_agent.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo ln -sf /etc/systemd/system/guest-agent.service \
  /mnt/rootfs/etc/systemd/system/multi-user.target.wants/guest-agent.service

sudo umount /mnt/rootfs
```

Put this in `scripts/install_guest_agent.sh`. **Rerun it every time** `guest_agent.py` changes.

**Do: test it.** Boot a VM in namespace 1 (as in 1d), then from the host:

```
curl http://10.201.0.1:49983/health
curl -X POST http://10.201.0.1:49983/exec -d '{"cmd":"echo hi"}'
curl -X PUT "http://10.201.0.1:49983/files?path=/root/test.txt" --data 'hello'
curl "http://10.201.0.1:49983/files?path=/root/test.txt"
curl -X POST http://10.201.0.1:49983/exec -d '{"cmd":"curl -sI https://github.com | head -1"}'
```

**You should see:** `{"ok": true}`, `hi`, `hello`, and `HTTP/2 200` (so the VM has internet without anyone SSHing in). **If** `/health` doesn't answer: SSH in and run `systemctl status guest-agent` and `journalctl -u guest-agent`.

### Done when

- [ ] All five curls above work on a freshly booted VM, with no manual steps inside it.

## 1f. The node agent: control sandboxes from Python

**What it is:** one Python module, `node_agent.py`, that does everything from 1c–1e in code. No web server yet.

**Do: write these functions.**

### `create_sandbox() -> Sandbox`

 1. Pick the lowest free number **N** (1–250). The sandbox ID is `sbx<N>`.
 2. Make its folder: `/var/sbx/sbx<N>/`.
 3. Copy the base image: `cp --sparse=always ~/fc/rootfs.ext4 /var/sbx/sbx<N>/rootfs.ext4`.
 4. Run `scripts/netns_create.sh N`.
 5. Create its cgroup: `mkdir /sys/fs/cgroup/sandboxes/sbx<N>` (create the parent `/sys/fs/cgroup/sandboxes` once at startup).
 6. Start Firecracker in the namespace:

    ```
    proc = subprocess.Popen(    ["ip", "netns", "exec", f"sbx{N}", "firecracker", "--api-sock", f"/var/sbx/sbx{N}/fc.sock"],    stdout=open(f"/var/sbx/sbx{N}/console.log", "w"), stderr=subprocess.STDOUT,)
    
    ```
 7. Move it into the cgroup: write `proc.pid` to `/sys/fs/cgroup/sandboxes/sbx<N>/cgroup.procs`.
 8. Wait until the socket file exists (check every 50 ms, give up after 5 s).
 9. Send the 1c calls with `httpx` over the socket: `boot-source`, `drives/rootfs` (pointing at `/var/sbx/sbx<N>/rootfs.ext4`), `machine-config`, `network-interfaces/eth0` (`tap0` + the same MAC), `actions InstanceStart`. Call `.raise_for_status()` on each.
10. Poll `http://10.201.0.<N>:49983/health` every 200 ms until it answers (give up after 30 s). **Record how long boot took.**
11. Return `Sandbox(id="sbx<N>", n=N, ip="10.201.0.<N>", pid=proc.pid)`.

### `destroy_sandbox(id)`

1. Kill the Firecracker process (`proc.kill()` or `os.kill(pid, SIGKILL)`), wait for it to exit.
2. `rmdir /sys/fs/cgroup/sandboxes/<id>` (only works once the process is gone).
3. Run `scripts/netns_delete.sh N`.
4. Delete `/var/sbx/<id>/`.

### `exec(id, cmd, cwd="/root", background=False, timeout=600)`

POST to the guest agent's `/exec`; return its JSON.

### `read_file(id, path)` / `write_file(id, path, content)`

GET / PUT the guest agent's `/files`.

**If something fails halfway through** `create_sandbox`, clean up whatever was already created (call `destroy_sandbox`), then raise the error. Otherwise you'll leak namespaces and processes.

**Do: write** `tests/test_node_agent.py`:

```
a = create_sandbox()
b = create_sandbox()
assert exec(a.id, "echo hi")["stdout"].strip() == "hi"
assert exec(b.id, "echo hi")["stdout"].strip() == "hi"
write_file(a.id, "/root/x.txt", "from a")
assert read_file(a.id, "/root/x.txt") == "from a"
assert exec(b.id, "cat /root/x.txt")["exit_code"] != 0   # b can't see a's files
destroy_sandbox(a.id)
destroy_sandbox(b.id)
```

### Done when

- [ ] The test passes (run with sudo).
- [ ] After it runs, ip netns list, ls /var/sbx and ls /sys/fs/cgroup/sandboxes are empty and no firecracker process is left (pgrep firecracker).

## 1g. The API server

**What it is:** a FastAPI app (`sbx/api/`) that wraps the node agent. This is what agents and clients talk to. It runs as root on the host.

| File | Job |
| -- | -- |
| `api/main.py` | The routes, and the startup hook |
| `api/helpers.py` | Lookups that answer 404/409 early, and turning node agent and guest agent errors into HTTP errors |
| `api/db.py` | Reads and writes to the `sandboxes` table |
| `api/models.py` | Request and response shapes (Pydantic) |
| `db/schema.sql` | The tables. Safe to run again |

**Do: create these endpoints** (`api/main.py`):

| Method & path | Calls | Returns |
| -- | -- | -- |
| `POST /sandboxes` | `create_sandbox(id)` | **201** `{"id": "sbx_3f9c…", "state": "running", "ip": "10.201.0.1", "created_at": …, "updated_at": …, "boot_seconds": 1.8}` |
| `GET /sandboxes` | — | `{"sandboxes": [ … ]}`: every sandbox, destroyed ones included, oldest first |
| `GET /sandboxes/{id}` | — | `{"id": …, "state": …, "ip": …, "created_at": …, "updated_at": …}` |
| `POST /sandboxes/{id}/exec` | `exec()` | `{"exit_code", "stdout", "stderr", "timed_out"}`, or `{"pid"}` if `background` is true |
| `GET /sandboxes/{id}/files?path=` | `read_file()` | the file's contents as plain text |
| `PUT /sandboxes/{id}/files?path=` | `write_file()` | `{"ok": true}`. The request body is the file's text |
| `DELETE /sandboxes/{id}` | `destroy_sandbox()` | `{"ok": true}`. Deleting twice is harmless |

The `exec` body is `{"cmd": "...", "cwd": "/root", "background": false, "timeout": 600}`. Only `cmd` is required. `timeout` must be 1–3600 seconds.

**Errors:**

| Code | When |
| -- | -- |
| **404** | Unknown sandbox ID, or the file doesn't exist |
| **409** | The sandbox isn't `running` (exec and files only) |
| **400 / 422** | Bad request: missing `path` or `cmd`, wrong types, or a file body that isn't text. The guest agent's own 4xx errors (e.g. a `cwd` that doesn't exist) are passed through |
| **500** | The VM failed to boot. The row is marked `destroyed` |
| **502** | The guest agent didn't answer, or answered with a 5xx |

**Do: keep state in SQLite** (`/var/sbx/state.db`, the same file as the node agent's `slots` table; `SBX_DB` overrides the path):

```
CREATE TABLE IF NOT EXISTS sandboxes (
  id          TEXT PRIMARY KEY,   -- "sbx_3f9c…" (uuid4), same as slots.sandbox_id
  state       TEXT NOT NULL CHECK (state IN ('starting', 'running', 'destroyed')),   -- Milestone 3 adds pausing | paused | resuming
  created_at  INTEGER NOT NULL,   -- unix seconds
  updated_at  INTEGER NOT NULL    -- unix seconds, changes with state
);
```

* No `n`, `ip` or `pid` columns. The slot comes from a join with `slots` (see 1f), the IP is worked out from the slot (`10.201.0.<n>`), and the PID is kept in `/var/sbx/<id>/firecracker.pid`. Each fact is stored in exactly one place.
* Rows are never deleted. A destroyed sandbox stays in the list with `state = 'destroyed'`.
* Pick the ID first (`new_sandbox_id()`), insert the row with `state = 'starting'`, then call `create_sandbox(id)` and set `running` when it returns. If the boot fails, set `destroyed` and answer 500.
* **On startup** (FastAPI `lifespan` in `main.py`):
  1. `db.create_tables()`. Without this step, a fresh server answers every request with 500 (`no such table`).
  2. For every row from `db.list_live_sandboxes()`: if `node_agent.is_sandbox_running(id)` is false, call `destroy_sandbox(id)` to clean up what's left and mark it `destroyed`. This also catches rows stuck in `starting` after a crash.
  3. For every ID from `node_agent.list_host_sandboxes()` that has no live row, call `destroy_sandbox(id)`. These are leftovers the API no longer tracks.

**Do: write** `tests/test_api.py` using FastAPI's `TestClient`, with the node agent functions swapped out by `monkeypatch` so it runs on any laptop (no root, no KVM). Point `SBX_DB` at a temp file. Include one test that does **not** call `create_tables()` itself, so the startup hook is tested too.

```
cd sbx && uv run pytest -q
```

**Do: run it** (from `sbx/`, so `node_agent` and `api` can be imported):

```
sudo .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
```

**Do: test with curl:**

```
ID=$(curl -s -X POST localhost:8000/sandboxes | jq -r .id); echo $ID
curl -s localhost:8000/sandboxes | jq
curl -s -X POST localhost:8000/sandboxes/$ID/exec -H 'Content-Type: application/json' -d '{"cmd":"uname -a"}' | jq
curl -s -X PUT "localhost:8000/sandboxes/$ID/files?path=/root/a.txt" --data-binary 'hello'
curl -s "localhost:8000/sandboxes/$ID/files?path=/root/a.txt"
curl -s -X DELETE localhost:8000/sandboxes/$ID
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/sandboxes/$ID/exec -H 'Content-Type: application/json' -d '{"cmd":"ls"}'
```

**You should see:** an `sbx_` ID with `"state": "running"`, `uname` output from the VM's kernel, `hello`, `{"ok":true}`, and then `409`, because the sandbox is destroyed.

**If it fails:**
* Every request returns 500: the `sandboxes` table is missing. Check that `lifespan` calls `db.create_tables()`, or run `sudo sqlite3 /var/sbx/state.db ".read db/schema.sql"` once.
* `sudo uv` or `sudo uvicorn` gives "command not found": root's PATH doesn't include them. Use `.venv/bin/uvicorn` or `"$(which uv)"`.
* 502 on exec or files: the guest agent isn't answering. Check `/var/sbx/<id>/console.log`.

### Done when

- [ ] `uv run pytest -q` passes. The VM tests from 1f also pass with sudo.
- [ ] Every endpoint works with curl and returns the codes in the table above.
- [ ] Starting the server on an empty `/var/sbx` works with no manual steps (tables are created on startup).
- [ ] Stop and restart the server: running sandboxes are still listed and usable.
- [ ] Kill a sandbox's Firecracker process by hand (`sudo kill -9 $(cat /var/sbx/<id>/firecracker.pid)`), then restart the server: that sandbox shows `destroyed`, and its namespace, folder and cgroup are gone.
- [ ] After `DELETE`, `ip netns list`, `ls /var/sbx` and `ls /sys/fs/cgroup/sandboxes` show nothing left from that sandbox.

## 1h. The SDK and an example agent

**First, confirm one decision as a team: the agent runs *outside* the VM.** The agent loop (calling the LLM, deciding what to do next) runs on the client's side. It uses the sandbox only to run commands and edit files through our API. Our diagram assumes this, and it matters later: when a sandbox is paused, the agent is still alive, so it can tell us "I'm waiting on a human" and wake the sandbox when a new message arrives.

**Do: write** `sdk/sandbox.py`, a thin wrapper over our API:

```
class Sandbox:
    @classmethod
    def create(cls, api="http://<host>:8000") -> "Sandbox": ...   # POST /sandboxes
    def exec(self, cmd, cwd="/root", background=False) -> dict: ...
    def read_file(self, path) -> str: ...
    def write_file(self, path, content) -> None: ...
    def destroy(self) -> None: ...
```

**Do: write** `example_agent/agent.py`, a minimal coding agent. It stands in for a client's agent; it's our test workload, not the product. Use whichever LLM API the team has a key for, with tool calling.

Three tools, each a thin call to the SDK:

| Tool | Input | Calls |
| -- | -- | -- |
| `run_command` | `cmd`, optional `background` | `sbx.exec(...)` |
| `read_file` | `path` | `sbx.read_file(path)` |
| `write_file` | `path`, `content` | `sbx.write_file(path, content)` |

The loop:

```
messages = [system prompt, user task]
loop:
    response = llm(messages, tools)
    if response has no tool calls: return response text     # the agent is done
    for each tool call:
        result = run the tool
        report the step (the client app prints it, see 1i)
        add the result to messages
```

System prompt (start with this): *"You are a coding agent working in a Linux sandbox. Use the tools to do the task. Start long-running servers with background=true."*

Cap it at 50 steps so a confused agent can't loop forever.

### Done when

- [ ] python example_agent/agent.py "create hello.py that prints hello, and run it" works end to end.

## 1i. The client app (version 1: a terminal chat)

**What it is:** the "client app / human" box in our diagram. A human types tasks; the agent does them in the sandbox. It starts tiny here and **grows in Milestones 2, 4 and 5.** It's a terminal app on purpose: a web UI is extra work that doesn't help prove anything.

**Do: write** `client/client.py`:

1. On start, call `Sandbox.create()` and print `sandbox sbx1 ready`.
2. Show a `>` prompt. When the user types a task, run the example agent on it with this sandbox.
3. Print each step as it happens: `[run] npm install`, `[read] package.json`, `[write] src/index.js`, and the first few lines of command output.
4. When the agent finishes, print its final message and `done`, then show the prompt again. **Keep the conversation history**, so a follow-up task knows what came before.
5. `/quit` destroys the sandbox and exits.

```
$ python client/client.py
sandbox sbx1 ready
> clone https://github.com/<example-express-app> and start the dev server
[run] git clone https://github.com/<example-express-app> app
[run] cd app && npm install
[run] cd app && npm run dev   (background)
The dev server is running on port 3000.
done
>
```

### Done when

- [ ] You can give it two tasks in a row, and the second builds on the first.

## 1j. Milestone 1 acceptance test

**Do: write** `tests/acceptance_agent.md` (a checklist a person runs) and follow it:

1. Start the API server on the host.
2. Start `client.py` on your laptop or the host. It prints a sandbox ID.
3. Type: *"Clone* `<our test repo>`, install dependencies, start the dev server in the background on port 3000, and add a `/health` route that returns ok."
4. Watch the client print each step until `done`.
5. From the host: `curl http://10.201.0.<N>:3000/health`. **It returns ok.**
6. Type a follow-up: *"Change* `/health` to also return the current time." `curl` again; it shows the time.
7. Type `/quit`. Check that `ip netns list`, `ls /var/sbx` and `pgrep firecracker` are empty.

**Also record** in `NOTES.md`: how long `create_sandbox` takes (Firecracker boot + guest agent ready).

### Milestone 1 is done when

- [ ] Someone who didn't write the code runs the checklist on a fresh host and every step passes.

---

# After the MVP

Milestones 2–6 build on the MVP, in this order.

---

# Milestone 2: Idle detection

**Goal:** for every running sandbox, know whether the agent is `working`, `waiting` or `idle`. **This milestone only watches. Nothing gets paused yet.** We log the labels and check them against what the agent was really doing.

## 2a. Activity sampler (reads from the host only)

**What it is:** a loop that, every 5 seconds, measures how busy each VM is. It needs no code inside the VM, because each VM's Firecracker process is in its own cgroup (CPU) and its own namespace (network).

**Do: write** `control/sampler.py`, an `asyncio` task started with the API server.

Every 5 seconds, for each sandbox in state `running`:

1. **CPU.** Read `/sys/fs/cgroup/sandboxes/<id>/cpu.stat` and take the `usage_usec` line (total CPU time used, in microseconds).

   ```
   cpu_pct = (usage_usec_now - usage_usec_last) / (elapsed_microseconds * vcpu_count) * 100
   
   ```
2. **Network.** Read the byte counters of the VM's network device:

   ```
   ip netns exec sbx<N> cat /sys/class/net/tap0/statistics/rx_bytesip netns exec sbx<N> cat /sys/class/net/tap0/statistics/tx_bytes
   
   ```

   `net_bytes = (rx + tx now) - (rx + tx last time)`
3. Send a reading to signal intake (2c):

   ```
   { "sandbox": "sbx1", "kind": "sample", "cpu_pct": 85.0, "net_bytes": 1200, "at": "2026-11-06T10:12:05Z" }
   
   ```

**Do: test it.**

| Run inside the VM (via `exec`) | You should see |
| -- | -- |
| nothing | `cpu_pct` near 0, `net_bytes` near 0 |
| `yes > /dev/null` (background) | `cpu_pct` near 50 (one of two vCPUs busy) |
| `curl -L -o /dev/null <the Firecracker release .tgz URL from 1a>` (any large download works) | large `net_bytes` |

### Done when

- [ ] All three rows in the table behave as described.

## 2b. Hooks and leases (the agent tells us what it's doing)

**What they are:**

* **Hooks:** the agent reports events from its loop ("I started a turn", "I finished", "I'm waiting for a person").
* **Lease:** the agent says "keep me awake for 60 seconds" and must renew it. If the agent crashes, it stops renewing, the lease expires, and the sandbox can't be kept awake forever by a dead agent.

**Do: add these endpoints:**

| Method & path | Body | Meaning |
| -- | -- | -- |
| `POST /sandboxes/{id}/signals` | `{"type": "turn_started"}` | Agent started working on a task |
|  | `{"type": "turn_finished"}` | Agent finished and is waiting for the next task |
|  | `{"type": "waiting_on_human"}` | Agent asked a question and is blocked on the answer |
| `POST /sandboxes/{id}/lease` | `{"ttl_seconds": 60}` | Keep awake for 60 s from now |

**Do: add to the SDK:** `sbx.signal(type)` and `sbx.lease(ttl_seconds)`.

**Do: call them from the example agent:**

| When | Call |
| -- | -- |
| A new task arrives | `signal("turn_started")` |
| The agent returns its final answer | `signal("turn_finished")` |
| The agent calls `ask_human` (new tool, below) | `signal("waiting_on_human")` |
| The human answers | `signal("turn_started")` |
| While any command is running | `lease(60)`, renewed every 30 s by a background thread |

**Do: add a fourth tool,** `ask_human(question)`. It returns the human's answer. The agent uses it when it needs a decision (e.g. "Which port should I use?"). Add to the system prompt: *"If you need a decision from the user, call ask_human."*

**Do: Client app, version 2: answer the agent's questions.**

- [ ] When the agent calls ask_human, client.py prints the question with a \[waiting on you\] marker.
- [ ] Whatever you type next is returned to the agent as the answer.
- [ ] Add a /status command that calls GET /sandboxes/{id} and prints the current label (once 2d exists).

### Done when

- [ ] Give a task that needs a decision (e.g. "Set up a web server; ask me which framework to use"). The client shows the question, waits as long as you like, and the agent carries on after you answer.

## 2c. Signal intake

**What it is:** one place that collects everything: sampler readings, hooks and lease heartbeats. **It makes no decisions.**

**Do: write** `control/signals.py`:

* Keep, per sandbox, a list of signals from the last 10 minutes (in memory). Drop older ones.
* Each signal: `{"sandbox", "kind": "sample" | "hook" | "lease", ..., "at"}`.
* Track per sandbox: the last hook and when, and the lease expiry time.
* Add `GET /sandboxes/{id}/signals`, which returns the list.
* Add a `--record` option that also appends every signal to `fixtures/<sandbox>-<date>.jsonl`.

### Done when

- [ ] Run the client through a task with a question in it. GET /sandboxes/{id}/signals shows hooks, leases and samples in time order.
- [ ] At least 5 recorded timelines are saved in fixtures/ (mix of: long builds, waiting on a question, finished and left alone).

## 2d. Activity classifier

**What it is:** a **pure function** (no I/O, no clock reads; "now" is passed in). One sandbox's recent signals in, one label out.

```
def classify(signals: list[dict], now: datetime) -> Literal["working", "waiting", "idle"]:
```

**Do: implement these starting rules, top to bottom; the first match wins:**

| \# | If | Label | Why |
| -- | -- | -- | -- |
| 1 | A lease hasn't expired yet | `working` | The agent said it's busy |
| 2 | Last hook is `turn_started` | `working` | Mid-task |
| 3 | Any sample in the last 60 s has `cpu_pct > 5` or `net_bytes > 10000` | `working` | Catches background jobs the hooks miss (e.g. a build the agent started) |
| 4 | Last hook is `waiting_on_human` | `waiting` | Blocked on a person |
| 5 | Last hook is `turn_finished` | `waiting` | Waiting for the next task |
| 6 | No signal of any kind for 5 minutes | `idle` | Nothing is happening |
| 7 | Otherwise | `working` | When unsure, stay awake |

**Do: run it** every 5 s for every running sandbox, store the label, show it in `GET /sandboxes/{id}`, and log every time it changes: `sbx1 working -> waiting (rule 4)`.

**Do: write unit tests** in `tests/test_classifier.py`: one test per rule, plus each recorded fixture with the labels you expect at specific times.

### Milestone 2 is done when

- [ ] All unit tests pass.
- [ ] Shadow test: run the client through a real 30-minute session while a teammate writes down, every minute, what the agent was actually doing. Compare with the logged labels. Write every mismatch in NOTES.md, fix the rules, and repeat until they match.

---

# Milestone 3: Pause & resume (by hand)

**Goal:** pause a sandbox and resume it later with memory + disk intact. Triggered manually through the API; Milestone 4 automates it.

## 3a. Learn how snapshots work (by hand)

Use a sandbox created by the API (say `sbx1`), so it's already in its namespace.

**What a snapshot is:** two files Firecracker writes while the VM is paused:

* `vmstate`: CPU registers and device state (small).
* `mem`: the VM's entire RAM (as big as `mem_size_mib`).

It does **not** include the disk. But once we pause the VM and kill Firecracker, the disk file `/var/sbx/sbx1/rootfs.ext4` stops changing, so it already matches the memory snapshot. **Never let anything write to it between pause and resume.**

**Do: pause and snapshot:**

```
S=/var/sbx/sbx1/fc.sock
mkdir -p /var/sbx/sbx1/snap

# 1. Flush the VM's pending file writes to disk (via the guest agent)
curl -X POST http://10.201.0.1:49983/exec -d '{"cmd":"sync"}'

# 2. Freeze the VM's CPUs
sudo curl --unix-socket $S -X PATCH http://localhost/vm \
  -H 'Content-Type: application/json' -d '{"state":"Paused"}'

# 3. Write the snapshot (time this!)
time sudo curl --unix-socket $S -X PUT http://localhost/snapshot/create \
  -H 'Content-Type: application/json' \
  -d '{"snapshot_type":"Full","snapshot_path":"/var/sbx/sbx1/snap/vmstate","mem_file_path":"/var/sbx/sbx1/snap/mem"}'

# 4. Kill Firecracker. This is what frees the RAM.
sudo pkill -f "api-sock /var/sbx/sbx1/fc.sock"
```

**Do: restore into a new Firecracker process, in the same namespace:**

```
sudo rm -f /var/sbx/sbx1/fc.sock
sudo ip netns exec sbx1 firecracker --api-sock /var/sbx/sbx1/fc.sock &

# Load the snapshot and start running immediately (time this!)
time sudo curl --unix-socket /var/sbx/sbx1/fc.sock -X PUT http://localhost/snapshot/load \
  -H 'Content-Type: application/json' \
  -d '{"snapshot_path":"/var/sbx/sbx1/snap/vmstate","mem_backend":{"backend_type":"File","backend_path":"/var/sbx/sbx1/snap/mem"},"resume_vm":true}'
```

Note that we **don't** send boot-source, drives, machine-config or network again: the snapshot remembers them, including the disk path and `tap0`. That's why 1d kept them identical.

**Do: these experiments, and write every result in** `NOTES.md`:

| \# | Experiment | How | What to write down |
| -- | -- | -- | -- |
| 1 | Memory survives a kill | Before pausing, start a counter: `exec` `{"cmd":"i=0; while true; do echo $i >> /root/count; i=$((i+1)); sleep 1; done", "background": true}`. After restore, `tail /root/count` | Does it continue from where it stopped? (It should, with no restart.) |
| 2 | RAM is freed | `free -m` on the host before step 4 and after | How many MB came back |
| 3 | Dev server survives | Start `python3 -m http.server 8080` in the background, pause, restore, `curl http://10.201.0.1:8080` from the host | Does it answer without restarting? |
| 4 | Disk must match memory | Restore once with an *older* copy of `rootfs.ext4` swapped in | What breaks (file errors, crashes) |
| 5 | Clock drift | Pause for 3 minutes, restore, compare `date` in the VM with `date` on the host | How far behind the VM is |
| 6 | Network after restore | Right after restore, does the guest agent answer `/health`? Does an open SSH session survive? | What reconnects and what doesn't |
| 7 | Sizes and times | `ls -lh /var/sbx/sbx1/snap/` plus the `time` outputs | Fill in the table in 3d |

## 3b. Add pause/resume to the guest agent and node agent

**Do: add to the guest agent:**

| Endpoint | Does |
| -- | -- |
| `POST /sync` | runs `sync` and returns when it finishes |
| `POST /clock` | body `{"unix_time": 1730900000.123}`; sets the VM clock (`date -s @<unix_time>`). Fixes the drift from experiment 5 |

Rerun `scripts/install_guest_agent.sh` (existing sandboxes keep the old version; create new ones to test).

**Do: add to** `node_agent.py`:

### `pause_sandbox(id)`

1. `POST /sync` to the guest agent.
2. `PATCH /vm {"state": "Paused"}`.
3. `PUT /snapshot/create` into `/var/sbx/<id>/snap/`.
4. Kill the Firecracker process and wait for it to exit.
5. Return the time each step took.

### `resume_sandbox(id)`

1. Check `/var/sbx/<id>/snap/vmstate`, `/var/sbx/<id>/snap/mem` and `/var/sbx/<id>/rootfs.ext4` all exist.
2. Delete the old socket file. Start a new Firecracker in the **same** namespace (same `Popen` as `create_sandbox` step 6) and put it in the **same** cgroup (step 7).
3. `PUT /snapshot/load` with `resume_vm: true`.
4. Wait for the guest agent's `/health` (it should answer almost immediately).
5. `POST /clock` with the host's current time.
6. Save the new PID. Return the time each step took.

## 3c. Add to the API

**Do: new endpoints:**

| Method & path | Calls |
| -- | -- |
| `POST /sandboxes/{id}/pause` | `pause_sandbox()` |
| `POST /sandboxes/{id}/resume` | `resume_sandbox()` |

**Do: add to the** `sandboxes` table: `snapshot_dir TEXT`, `paused_at INTEGER`.

**Do: enforce these lifecycle rules** (the "Lifecycle state machine" box). Any other change returns **409**:

```
running  → pausing  → paused
paused   → resuming → running
any      → destroyed
```

* **One change at a time per sandbox.** Keep an `asyncio.Lock` per sandbox and hold it during pause and resume. If a resume arrives during a pause, it waits for the lock, then resumes.
* **Resume on next request:** if `exec` or a file request arrives while the sandbox is `paused`, resume it first, then handle the request. The caller just sees a slower response.
* Add `sbx.pause()` and `sbx.resume()` to the SDK.

## 3d. Acceptance test

**Do: write** `tests/acceptance_pause.md` and follow it:

1. Run the Milestone 1 acceptance test through step 5 (dev server running, `/health` answers).
2. Start the counter from 3a experiment 1.
3. `curl -X POST localhost:8000/sandboxes/sbx1/pause`. Check `pgrep -f sbx1/fc.sock` returns nothing.
4. Wait 60 seconds.
5. In the client, type a follow-up task. **It resumes automatically** (resume on next request).
6. `curl http://10.201.0.1:3000/health`. **It answers without the dev server being restarted.**
7. `tail /root/count` (via `exec`): the counter continued from where it stopped.
8. The agent finishes the follow-up task as if nothing happened.
9. Repeat steps 3–7 with **two** sandboxes paused and resumed at the same time.

**Do: fill in this table** (create sandboxes with each memory size; it's needed for Milestone 4's grace period):

| Guest RAM | Pause time (sync + pause + snapshot + kill) | Resume time (start + load + health) | `mem` size | `vmstate` size |
| -- | -- | -- | -- | -- |
| 512 MiB |  |  |  |  |
| 1 GiB |  |  |  |  |
| 2 GiB |  |  |  |  |

### Milestone 3 is done when

- [ ] Every step in the checklist passes.
- [ ] The table is filled in.

---

# Milestone 4: Auto pause & resume

**Goal:** connect idle detection (Milestone 2) to pause/resume (Milestone 3).

## 4a. Baseline: the E2B-style timeout rule (build this first)

**Why:** our claim is that pausing based on agent state beats pausing on a timer. We need the timer version to compare against.

**Do: write** `control/decider_timeout.py`:

```
def decide_timeout(last_api_request_at, now, timeout=timedelta(minutes=5)) -> Literal["pause", "none"]:
    return "pause" if now - last_api_request_at > timeout else "none"
```

Track `last_api_request_at` per sandbox (update it on every API call for that sandbox).

## 4b. Our pause decider

**What it is:** a **pure function** that turns the classifier's label into a command.

```
def decide(label, label_since, lease_expires_at, awake_since, pending_message, now, cfg) -> Literal["pause", "resume", "none"]:
```

**Do: implement these rules, top to bottom; the first match wins:**

| \# | If | Command |
| -- | -- | -- |
| 1 | Sandbox is paused and a new task or message is pending | `resume` |
| 2 | Sandbox is running and has been awake longer than `cfg.max_awake` (start: 4 h) | `pause` |
| 3 | A lease is active | `none` |
| 4 | Label is `waiting` or `idle`, and has been for longer than `cfg.grace` (start: 60 s) | `pause` |
| 5 | Otherwise | `none` |

**Do: run it** every 5 s (in the same loop as the classifier) and carry out the command with `pause_sandbox` / `resume_sandbox`. Log every automatic action with the rule that caused it: `auto-pause sbx1 (rule 4: waiting for 63s)`.

**Choosing the grace period:** use the 3d table. If a pause + resume costs \~10 s of waiting for a 1 GiB sandbox, pausing after only 5 s of idleness isn't worth it. Write down the reasoning for the value you pick.

**Do: add a config switch** so the server can run either `decide_timeout` (baseline) or `decide` (ours).

## 4c. Compare

**Do: write** `tools/replay.py`: feed each recorded timeline in `fixtures/` through the classifier and **both** deciders, minute by minute, without real VMs. Count:

| Metric | Means | Baseline | Ours |
| -- | -- | -- | -- |
| Mid-work pauses | Paused while the agent was really working (bad) |  |  |
| Idle minutes kept awake | Minutes spent running while waiting/idle (wasted compute) |  |  |
| Number of pauses | Each one costs pause + resume time |  |  |

## 4d. Client app, version 3: see pauses and resumes

- [ ] Every 2 s, client.py checks GET /sandboxes/{id} and prints a line when the state changes: \[sandbox paused: waiting on you\], \[sandbox resuming…\], \[sandbox running, resumed in 1.8s\].
- [ ] Typing a reply while the sandbox is paused just works: the reply goes to the agent, the agent's next sandbox call resumes it, and the client shows the resume.

### Milestone 4 is done when

- [ ] In client.py: the agent asks a question, you wait. After the grace period the client shows \[sandbox paused\]. You reply, it shows the resume, and the agent carries on.
- [ ] A sandbox running a long npm install (no hooks sent) is not paused.
- [ ] The 4c table is filled in and our decider has fewer mid-work pauses than the baseline.

---

# Milestone 5: Reconnect (event broker)

**Goal:** a client that disconnected (or whose sandbox was paused) reconnects and gets exactly what it missed.

## 5a. The event log

**Do: add a table:**

```
CREATE TABLE events (
  sandbox_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,    -- 1, 2, 3, ... per sandbox
  type        TEXT NOT NULL,       -- agent_step | agent_message | question | paused | resumed | ...
  data        TEXT NOT NULL,       -- JSON
  at          INTEGER NOT NULL,
  PRIMARY KEY (sandbox_id, seq)
);
```

It lives on the **host**, outside the VM, so it survives pauses.

**Do: add endpoints:**

| Method & path | Does |
| -- | -- |
| `POST /sandboxes/{id}/events` | Append an event. The server assigns the next `seq`. The agent calls this for every step it reports |
| `GET /sandboxes/{id}/events?after=1041` | Server-Sent Events stream: first every event with `seq > 1041`, then new events live as they arrive |

* The server itself appends `pausing`, `paused`, `resuming` and `resumed` events.
* **Retention:** delete events older than 7 days (once an hour).

## 5b. Client app, version 4: reconnect and catch up

- [ ] client.py gets everything it prints (agent steps, questions, pause/resume lines) from the event stream, instead of directly from the agent and state polling.
- [ ] After printing each event, it saves its seq to \~/.sbx_client/<sandbox_id>.
- [ ] python client/client.py --attach sbx1 reads the saved seq, connects with ?after=<seq>, prints everything it missed, then continues live.

### Done when

- [ ] Start a long task, close client.py mid-task, let the agent finish and the sandbox get paused, then --attach. The client prints exactly the missed events (check: no seq skipped or repeated), and you can carry on chatting.

---

# Milestone 6: Durable snapshots in GCS

**Goal:** a snapshot survives losing the machine.

## 6a. Upload after pause

**Do:**

1. Create a bucket in the same region as the host: `gcloud storage buckets create gs://<our-bucket> --location=us-east1`.
2. Give the host's service account write access to it.
3. After `pause_sandbox` finishes, start a **background** task that uploads `mem`, `vmstate` and `rootfs.ext4` to `gs://<our-bucket>/<sandbox_id>/<snapshot_id>/`. The pause call **does not wait** for it.
4. Add `upload_status` (`pending | uploading | done | failed`) to the `sandboxes` table. Retry failures up to 3 times.
5. **Don't delete or change the local files during the upload.** If a resume starts mid-upload, the resume uses the local files as normal; the upload keeps reading the snapshot files, which don't change.

## 6b. Download on resume when needed

**Do:** in `resume_sandbox`, if the local snapshot files are missing, download them from GCS to `/var/sbx/<id>/` first, then resume as normal. Record how long the download took.

### Done when

- [ ] Pause a sandbox, wait for upload_status = done, delete /var/sbx/<id>/snap/ and rootfs.ext4, resume. It works (slower). Write the download time in the 3d table as a new column.