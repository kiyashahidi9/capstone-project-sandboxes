# Setup

Sandboxes run on a Linux VM with KVM. These steps create one on Google Cloud and set it up.

## 1. Install the Google Cloud CLI (on your computer)

Follow https://cloud.google.com/sdk/docs/install, then log in and pick your project:

```bash
gcloud init
```

## 2. Create the VM

Nested virtualization lets Firecracker run VMs inside it. The free `e2-micro` can't do this, so use an N2 machine.

```bash
gcloud compute instances create sbx-node-1 \
  --zone=us-east1-b --machine-type=n2-standard-4 \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB --enable-nested-virtualization
```

## 3. Connect to it

```bash
gcloud compute ssh sbx-node-1 --zone=us-east1-b
```

For VS Code Remote-SSH, run `gcloud compute config-ssh` (again after every restart, since the IP changes).

## 4. Set up the host (on the VM)

```bash
git clone https://github.com/kiyashahidi9/capstone-project-sandboxes.git
cd capstone-project-sandboxes
sudo scripts/setup_host.sh
```

This installs Firecracker and host packages, downloads the kernel and base image into `~/fc`,
bakes the guest agent into the image, and creates `.venv`. It ends with a checklist; every line
should show `[x]`. It's safe to rerun.

## Stopping and starting the VM

```bash
gcloud compute instances stop sbx-node-1 --zone=us-east1-b    # stops billing for CPU/RAM
gcloud compute instances start sbx-node-1 --zone=us-east1-b
```

Stopping ends every running sandbox. Start the API again (step 5) after starting the VM.