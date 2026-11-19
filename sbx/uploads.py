"""Copy each paused sandbox's snapshot to the client's bucket, so it survives losing the VM (Build Plan 7a).

After a pause, start_upload() copies mem, vmstate and rootfs.ext4 to
gs://<SNAPSHOT_BUCKET>/<sandbox_id>/<snapshot_id>/ on a background thread, then writes an
empty `complete` file last. A restore (7b) only trusts a folder that has `complete`.

Before a resume starts the VM, stop_upload() stops the copy and waits until no file is being
read. The resumed VM writes to rootfs.ext4, so a copy that kept reading would save a disk that
no longer matches mem. A stopped copy never gets `complete`.

Both are called with the sandbox's lock held (api/helpers.py), so a resume can never slip in
between a pause and the start of its upload. With SNAPSHOT_BUCKET unset (a dev host), both do nothing.

When a paused sandbox's local snapshot is gone, restore_from_bucket() downloads the newest folder
that has `complete` (7b). If the last pause never finished uploading, that's an older pause.
"""
from __future__ import annotations

import functools
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import BinaryIO

from google.cloud import storage

import host_manager
from api import db
from api.models import UploadStatus


logger = logging.getLogger(__name__)

BUCKET_ENV = "SNAPSHOT_BUCKET"    # in .env, e.g. gs://my-project-sbx-snapshots; unset turns uploads off
GCS_PREFIX = "gs://"
COMPLETE_MARKER = "complete"      # written last; its presence means every file in the folder is whole
UPLOAD_ATTEMPTS = 4               # the first try plus 3 retries (7a); every try starts the file from zero
RETRY_DELAY_SECONDS = 5.0         # pause between tries, cut short if the upload is stopped
CHUNK_BYTES = 8 * 1024 * 1024     # bytes read and sent per request; a stop waits for at most one read
SNAPSHOT_ID_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
SNAPSHOT_ID_RANDOM_BYTES = 2      # random hex after the time, so two pauses in one second get different folders
PARTIAL_SUFFIX = ".part"          # a download's name until all three files are whole


class UploadStopped(Exception):
    """Raised inside an upload once a resume or destroy has asked it to stop."""


@dataclass
class UploadJob:
    """One snapshot copy under way: which sandbox, which bucket folder, and the switches that stop it."""

    sandbox_id: str
    snapshot_id: str
    stop: threading.Event = field(default_factory=threading.Event)
    reading: threading.Lock = field(default_factory=threading.Lock)   # held while a chunk of a file is read


# The upload under way for each sandbox. Only the job in here may record upload_status, so a
# stopped thread still finishing a network request can't overwrite what a later pause recorded.
_jobs: dict[str, UploadJob] = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Called by the API (api/helpers.py), with the sandbox's lock held
# ---------------------------------------------------------------------------

def start_upload(sandbox_id: str) -> None:
    """Starts copying a just-paused sandbox's snapshot to the bucket in the background; does nothing if uploads are off."""
    if not bucket_name():
        return
    job = UploadJob(sandbox_id, new_snapshot_id())
    register(job)
    thread = threading.Thread(target=run_upload, args=(job,), name=f"sbx-upload-{sandbox_id}", daemon=True)
    thread.start()


def stop_upload(sandbox_id: str) -> None:
    """Stops the sandbox's upload, if one is under way, and waits until none of its files is being read."""
    job = cancel_current_job(sandbox_id)
    if job is None:
        return
    with job.reading:
        pass   # a chunk read already in progress finishes here; every later read raises UploadStopped
    logger.info("stopped the upload of %s snapshot %s", sandbox_id, job.snapshot_id)


# ---------------------------------------------------------------------------
# Which job is current
# ---------------------------------------------------------------------------

def register(job: UploadJob) -> None:
    """Makes a job the sandbox's current upload and records it as waiting to start."""
    with _jobs_lock:
        _jobs[job.sandbox_id] = job
        db.set_upload_status(job.sandbox_id, UploadStatus.PENDING)


def cancel_current_job(sandbox_id: str) -> UploadJob | None:
    """Tells the sandbox's current upload to stop and records it as cancelled; gives back the job, if there was one."""
    with _jobs_lock:
        job = _jobs.pop(sandbox_id, None)
        if job is None:
            return None
        job.stop.set()
        db.set_upload_status(sandbox_id, UploadStatus.CANCELLED)
    return job


def is_current(job: UploadJob) -> bool:
    """Checks the job is still its sandbox's upload, not one a resume or a later pause replaced; caller holds _jobs_lock."""
    return _jobs.get(job.sandbox_id) is job


def record_status(job: UploadJob, status: UploadStatus) -> None:
    """Records how far the upload has got, unless it has been cancelled."""
    with _jobs_lock:
        if is_current(job):
            db.set_upload_status(job.sandbox_id, status)


def finish(job: UploadJob, status: UploadStatus) -> None:
    """Records how the upload ended and lets go of it, unless it has been cancelled."""
    with _jobs_lock:
        if is_current(job):
            db.set_upload_status(job.sandbox_id, status)
            del _jobs[job.sandbox_id]


# ---------------------------------------------------------------------------
# The upload thread
# ---------------------------------------------------------------------------

def run_upload(job: UploadJob) -> None:
    """Copies one snapshot to the bucket and records how it ended; runs on its own thread."""
    record_status(job, UploadStatus.UPLOADING)
    try:
        upload_snapshot(job)
    except UploadStopped:
        return   # stop_upload() already recorded it as cancelled
    except Exception:
        logger.exception("upload of %s snapshot %s failed", job.sandbox_id, job.snapshot_id)
        finish(job, UploadStatus.FAILED)
        return
    finish(job, UploadStatus.DONE)
    logger.info("uploaded %s snapshot %s", job.sandbox_id, job.snapshot_id)


def upload_snapshot(job: UploadJob) -> None:
    """Copies every file a resume needs to the bucket, then the marker that says the copy is whole."""
    for path in snapshot_files(job.sandbox_id):
        upload_with_retries(job, path)
    send_complete_marker(job)


def upload_with_retries(job: UploadJob, path: str) -> None:
    """Copies one file to the bucket, starting it again from zero after a failure, a few times at most."""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            send_file(job, path)
            return
        except UploadStopped:
            raise
        except Exception:
            if attempt == UPLOAD_ATTEMPTS:
                raise
            logger.warning("upload of %s failed (try %d of %d); starting it again", path, attempt, UPLOAD_ATTEMPTS)
        wait_before_retry(job)


def wait_before_retry(job: UploadJob) -> None:
    """Waits a little before the next try, giving up at once if the upload is stopped meanwhile."""
    if job.stop.wait(RETRY_DELAY_SECONDS):
        raise UploadStopped


# ---------------------------------------------------------------------------
# Names and paths
# ---------------------------------------------------------------------------

def bucket_name() -> str:
    """Gives the client's snapshot bucket from .env, or an empty string when uploads are off."""
    name = os.environ.get(BUCKET_ENV, "").strip()
    return name.removeprefix(GCS_PREFIX).rstrip("/")


def new_snapshot_id() -> str:
    """Makes a name for one pause's folder in the bucket; names sort oldest to newest."""
    stamp = datetime.now(timezone.utc).strftime(SNAPSHOT_ID_TIME_FORMAT)
    return f"{stamp}-{secrets.token_hex(SNAPSHOT_ID_RANDOM_BYTES)}"


def snapshot_files(sandbox_id: str) -> list[str]:
    """Lists the host files a resume needs: the VM's memory, its CPU state and its disk."""
    return [
        host_manager.vmstate_path(sandbox_id),
        host_manager.mem_path(sandbox_id),
        host_manager.disk_path(sandbox_id),
    ]


def object_name(job: UploadJob, file_name: str) -> str:
    """Gives where one file of this snapshot goes in the bucket."""
    return object_path(job.sandbox_id, job.snapshot_id, file_name)


def object_path(sandbox_id: str, snapshot_id: str, file_name: str) -> str:
    """Gives a file's name in the bucket: one folder per sandbox, one inside it per pause."""
    return f"{sandbox_id}/{snapshot_id}/{file_name}"


# ---------------------------------------------------------------------------
# Talking to Cloud Storage
# ---------------------------------------------------------------------------

@functools.cache
def storage_bucket() -> storage.Bucket:
    """Connects to the client's snapshot bucket as the VM's service account; made once, on first use."""
    return storage.Client().bucket(bucket_name())


def send_file(job: UploadJob, path: str) -> None:
    """Sends one file to the bucket, reading it through a reader that refuses once the upload is stopped."""
    blob = storage_bucket().blob(object_name(job, os.path.basename(path)), chunk_size=CHUNK_BYTES)
    with open(path, "rb") as file:
        # retry=None: a failed file starts again from zero in upload_with_retries (7a)
        blob.upload_from_file(StoppableReader(file, job), size=os.path.getsize(path), retry=None)


def send_complete_marker(job: UploadJob) -> None:
    """Writes the empty file that tells a restore this copy is whole; refuses if the upload was stopped."""
    if job.stop.is_set():
        raise UploadStopped
    storage_bucket().blob(object_name(job, COMPLETE_MARKER)).upload_from_string(b"")


class StoppableReader:
    """Reads a snapshot file for the storage library, refusing once the upload has been told to stop."""

    def __init__(self, file: BinaryIO, job: UploadJob) -> None:
        """Wraps an open file for one upload job."""
        self.file = file
        self.job = job

    def read(self, size: int = -1) -> bytes:
        """Gives the next bytes of the file, or raises UploadStopped once a stop was asked for."""
        with self.job.reading:
            if self.job.stop.is_set():
                raise UploadStopped
            return self.file.read(size)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Moves to a place in the file; the library does this to measure the file and to resend a chunk."""
        return self.file.seek(offset, whence)

    def tell(self) -> int:
        """Gives the current place in the file."""
        return self.file.tell()


# ---------------------------------------------------------------------------
# Restoring from the bucket (7b), called by the API with the sandbox's lock held
# ---------------------------------------------------------------------------

def restore_from_bucket(sandbox_id: str) -> None:
    """Downloads the sandbox's newest whole snapshot from the bucket; does nothing if there's none."""
    snapshot_id = newest_complete_snapshot(sandbox_id)
    if snapshot_id is None:
        return
    started = time.monotonic()
    download_snapshot(sandbox_id, snapshot_id)
    logger.warning("restored %s from bucket snapshot %s in %.1f s",
                   sandbox_id, snapshot_id, time.monotonic() - started)


def download_snapshot(sandbox_id: str, snapshot_id: str) -> None:
    """Downloads all three files of one bucket snapshot to where a paused sandbox keeps them."""
    os.makedirs(host_manager.snapshot_dir(sandbox_id), exist_ok=True)
    paths = snapshot_files(sandbox_id)
    for path in paths:
        download_file(sandbox_id, snapshot_id, path)
    move_downloads_into_place(paths)


def has_complete_copy(sandbox_id: str) -> bool:
    """Checks whether the bucket holds a whole snapshot of the sandbox, so it can still be restored."""
    return newest_complete_snapshot(sandbox_id) is not None


def newest_complete_snapshot(sandbox_id: str) -> str | None:
    """Gives the newest of the sandbox's whole bucket copies, or None (always None without a bucket)."""
    if not bucket_name():
        return None
    snapshot_ids = complete_snapshot_ids(sandbox_id)
    return max(snapshot_ids) if snapshot_ids else None   # names start with the time, so the highest is newest


def complete_snapshot_ids(sandbox_id: str) -> list[str]:
    """Lists the sandbox's bucket folders that hold the complete marker, so every file in them is whole."""
    prefix = f"{sandbox_id}/"
    ending = f"/{COMPLETE_MARKER}"
    return [
        blob.name.removeprefix(prefix).removesuffix(ending)
        for blob in storage_bucket().list_blobs(prefix=prefix)
        if blob.name.endswith(ending)
    ]


def download_file(sandbox_id: str, snapshot_id: str, path: str) -> None:
    """Downloads one snapshot file next to where it belongs, under a temporary name."""
    name = object_path(sandbox_id, snapshot_id, os.path.basename(path))
    storage_bucket().blob(name).download_to_filename(path + PARTIAL_SUFFIX)


def move_downloads_into_place(paths: list[str]) -> None:
    """Gives the downloaded files their real names, only once all of them arrived, so two snapshots never mix."""
    for path in paths:
        os.replace(path + PARTIAL_SUFFIX, path)


# ---------------------------------------------------------------------------
# Deleting bucket copies (6b), when a sandbox is deleted for good
# ---------------------------------------------------------------------------

def delete_bucket_copies(sandbox_id: str) -> None:
    """Deletes every bucket copy of a sandbox's snapshots, so it stops costing storage; does nothing without a bucket."""
    if not bucket_name():
        return
    for blob in storage_bucket().list_blobs(prefix=f"{sandbox_id}/"):
        blob.delete()
