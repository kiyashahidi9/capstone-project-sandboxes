# Setup

Sandboxes are Firecracker microVMs, which need a Linux host with KVM.
These steps create the host on Google Cloud and set it up.

## 1. Install the Google Cloud CLI (on your computer)

Install it from https://cloud.google.com/sdk/docs/install, then log in and choose your project:

```bash
gcloud init
```

## 2. Create the host VM

A normal cloud VM doesn't expose KVM, so turn on nested virtualization. Use an Intel machine type
such as N2, where it's best supported.

```bash
gcloud compute instances create sbx-node-1 \
  --zone=us-east1-b \
  --machine-type=n2-standard-8 \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=200GB \
  --enable-nested-virtualization
```

## 3. Connect to it

```bash
gcloud compute ssh sbx-node-1 --zone=us-east1-b
```

For VS Code Remote-SSH, run `gcloud compute config-ssh` (again after each restart; the IP changes).

## 4. Set up the host (on the VM)

```bash
git clone https://github.com/kiyashahidi9/capstone-project-sandboxes.git -b 1-setup_host
cd capstone-project-sandboxes/sbx
chmod +x scripts/*
sudo scripts/setup_host.sh
```

This takes a few minutes. It:

1. installs host packages and checks KVM (`kvm-ok`) and cgroups v2,
2. installs Firecracker (pinned version, so everyone matches),
3. downloads the kernel and builds the base image in `~/fc`: Ubuntu 24.04 with Python, git and Node,
4. bakes the guest agent into the image (`scripts/install_guest_agent.sh`),
5. creates the Python environment `.venv`,

and ends with a checklist. Every line should show `[x]`. It's safe to rerun. To rebuild the base
image from scratch, run `sudo scripts/setup_host.sh --rebuild-rootfs`.

## Stopping and starting the VM

```bash
gcloud compute instances stop sbx-node-1 --zone=us-east1-b
gcloud compute instances start sbx-node-1 --zone=us-east1-b
```

Stopping ends every running sandbox. After starting, run step 5 again.