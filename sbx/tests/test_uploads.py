import os
import threading
import time
from types import SimpleNamespace

import pytest

import host_manager
import uploads
from api import db
from api.models import SandboxState, UploadStatus


ID = "sbx_" + "c" * 32
BUCKET = "test-bucket"
FAKE_CHUNK_BYTES = 4              # the fake reads this much at a time, so a test can stop it mid-file
FILE_BYTES = 64
WAIT_SECONDS = 5                  # longest a test waits for the upload thread before failing
FILE_NAMES = ["vmstate", "mem", "rootfs.ext4"]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeBlob:
    """Stands in for a Cloud Storage object, reading a stream in small chunks like the real library."""

    def __init__(self, bucket, name):
        """Remembers which fake bucket the object goes into."""
        self.bucket = bucket
        self.name = name

    def upload_from_file(self, stream, size, retry):
        """Reads the whole stream chunk by chunk, failing or waiting first if the test asked for that."""
        self.bucket.attempts.append(self.name)
        self.bucket.fail_if_asked(self.name)
        data = b""
        while chunk := stream.read(FAKE_CHUNK_BYTES):
            data += chunk
            self.bucket.reads += 1
            self.bucket.wait_at_gate(self.name)
        assert len(data) == size
        self.bucket.objects[self.name] = data

    def upload_from_string(self, data):
        """Stores a small object straight away."""
        self.bucket.objects[self.name] = data

    def delete(self):
        """Removes the stored object."""
        del self.bucket.objects[self.name]

    def download_to_filename(self, filename):
        """Writes the stored object to a local file, failing first if the test asked for that."""
        self.bucket.fail_if_asked(self.name)
        with open(filename, "wb") as file:
            file.write(self.bucket.objects[self.name])


class FakeBucket:
    """Stands in for the client's bucket: keeps what was uploaded, and can fail or pause an upload."""

    def __init__(self):
        """Starts empty, with no failures and no gate."""
        self.objects = {}
        self.attempts = []
        self.reads = 0
        self.failures_left = {}      # object-name ending -> how many more tries should fail
        self.gate_on = None          # object-name ending whose upload waits at the gate after each chunk
        self.at_gate = threading.Event()
        self.gate = threading.Event()

    def blob(self, name, chunk_size=None):
        """Gives a fake object with the given name."""
        return FakeBlob(self, name)

    def list_blobs(self, prefix=""):
        """Lists the stored objects whose names start with the prefix, like the real bucket."""
        return [FakeBlob(self, name) for name in sorted(self.objects) if name.startswith(prefix)]

    def fail_if_asked(self, name):
        """Raises a network-style error while the test still wants this object's tries to fail."""
        for ending, left in self.failures_left.items():
            if name.endswith(ending) and left > 0:
                self.failures_left[ending] = left - 1
                raise ConnectionError("fake network error")

    def wait_at_gate(self, name):
        """Holds the upload after a chunk until the test opens the gate, if this object is gated."""
        if self.gate_on is not None and name.endswith(self.gate_on):
            self.at_gate.set()
            self.gate.wait(WAIT_SECONDS)


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    """Turns uploads on against a fake bucket, with a recorded sandbox and its snapshot files on disk."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv(uploads.BUCKET_ENV, f"gs://{BUCKET}")
    monkeypatch.setattr(uploads, "RETRY_DELAY_SECONDS", 0)
    fake = FakeBucket()
    monkeypatch.setattr(uploads, "storage_bucket", lambda: fake)
    monkeypatch.setattr(uploads, "_jobs", {})
    db.create_tables()
    db.insert_sandbox(ID)
    db.set_state(ID, SandboxState.PAUSED)
    write_snapshot_files()
    return fake


def write_snapshot_files():
    """Puts small stand-ins for vmstate, mem and rootfs.ext4 where a paused sandbox keeps them."""
    os.makedirs(host_manager.snapshot_dir(ID), exist_ok=True)
    for path in uploads.snapshot_files(ID):
        with open(path, "wb") as file:
            file.write(os.path.basename(path).encode().ljust(FILE_BYTES, b"."))


def upload_status():
    """Gives the sandbox's recorded upload status."""
    return db.get_sandbox(ID).upload_status


def wait_for_upload_status(status):
    """Waits until the upload thread records the given status, failing the test if it never does."""
    deadline = time.monotonic() + WAIT_SECONDS
    while upload_status() != status:
        assert time.monotonic() < deadline, f"upload never reached {status}, still {upload_status()}"
        time.sleep(0.01)


def object_endings(fake):
    """Lists the file names stored in the fake bucket, without their folder."""
    return sorted(name.rsplit("/", 1)[-1] for name in fake.objects)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_bucket_name_drops_the_gs_prefix(monkeypatch):
    monkeypatch.setenv(uploads.BUCKET_ENV, " gs://my-bucket/ ")
    assert uploads.bucket_name() == "my-bucket"


def test_snapshot_ids_sort_by_time_and_differ_within_a_second():
    first = uploads.new_snapshot_id()
    second = uploads.new_snapshot_id()
    assert first != second
    assert first[:16] <= second[:16]          # the time part, e.g. 20261118T153012Z
    assert first[8] == "T" and first[15] == "Z"


def test_files_go_into_a_folder_per_sandbox_and_snapshot():
    job = uploads.UploadJob(ID, "20261118T153012Z-a3f9")
    assert uploads.object_name(job, "mem") == f"{ID}/20261118T153012Z-a3f9/mem"


# ---------------------------------------------------------------------------
# Uploading
# ---------------------------------------------------------------------------

def test_no_bucket_means_no_upload(bucket, monkeypatch):
    monkeypatch.delenv(uploads.BUCKET_ENV)
    uploads.start_upload(ID)
    assert upload_status() is None
    assert bucket.objects == {}


def test_upload_copies_every_file_then_marks_it_complete(bucket):
    uploads.start_upload(ID)
    wait_for_upload_status(UploadStatus.DONE)

    assert object_endings(bucket) == sorted([*FILE_NAMES, uploads.COMPLETE_MARKER])
    assert list(bucket.objects)[-1].endswith(f"/{uploads.COMPLETE_MARKER}")   # written last
    assert len({name.rsplit("/", 1)[0] for name in bucket.objects}) == 1        # one folder
    for path in uploads.snapshot_files(ID):
        stored = next(data for name, data in bucket.objects.items() if name.endswith("/" + os.path.basename(path)))
        assert stored == open(path, "rb").read()
    assert uploads._jobs == {}


def test_a_failed_file_is_tried_again_from_the_start(bucket):
    bucket.failures_left["/mem"] = 2
    uploads.start_upload(ID)
    wait_for_upload_status(UploadStatus.DONE)
    assert sum(name.endswith("/mem") for name in bucket.attempts) == 3
    assert uploads.COMPLETE_MARKER in object_endings(bucket)


def test_upload_fails_after_the_last_retry_without_marking_it_complete(bucket):
    bucket.failures_left["/mem"] = uploads.UPLOAD_ATTEMPTS
    uploads.start_upload(ID)
    wait_for_upload_status(UploadStatus.FAILED)
    assert sum(name.endswith("/mem") for name in bucket.attempts) == uploads.UPLOAD_ATTEMPTS
    assert uploads.COMPLETE_MARKER not in object_endings(bucket)
    assert uploads._jobs == {}


# ---------------------------------------------------------------------------
# Stopping (a resume or destroy)
# ---------------------------------------------------------------------------

def test_stop_mid_file_cancels_and_no_file_is_read_afterwards(bucket):
    bucket.gate_on = "/rootfs.ext4"
    uploads.start_upload(ID)
    assert bucket.at_gate.wait(WAIT_SECONDS)       # part-way through the disk

    uploads.stop_upload(ID)
    reads_when_stopped = bucket.reads
    bucket.gate.set()                              # let the thread try to carry on
    time.sleep(0.05)

    assert bucket.reads == reads_when_stopped
    assert upload_status() == UploadStatus.CANCELLED
    assert "rootfs.ext4" not in object_endings(bucket)
    assert uploads.COMPLETE_MARKER not in object_endings(bucket)


def test_stop_waits_for_a_chunk_read_already_in_progress(bucket):
    job = uploads.UploadJob(ID, uploads.new_snapshot_id())
    uploads.register(job)
    stopped = threading.Event()

    job.reading.acquire()                          # a read is in progress
    threading.Thread(target=lambda: (uploads.stop_upload(ID), stopped.set()), daemon=True).start()
    assert not stopped.wait(0.1)                   # stop is waiting for it
    job.reading.release()
    assert stopped.wait(WAIT_SECONDS)


def test_a_stopped_upload_cannot_overwrite_the_next_pauses_status(bucket):
    bucket.gate_on = "/mem"
    uploads.start_upload(ID)
    assert bucket.at_gate.wait(WAIT_SECONDS)
    old_job = uploads._jobs[ID]
    uploads.stop_upload(ID)

    new_job = uploads.UploadJob(ID, uploads.new_snapshot_id())
    uploads.register(new_job)                      # the next pause's upload
    uploads.finish(old_job, UploadStatus.FAILED)   # the old thread ending late

    assert upload_status() == UploadStatus.PENDING
    assert uploads._jobs[ID] is new_job
    bucket.gate.set()


def test_stop_with_no_upload_does_nothing(bucket):
    uploads.stop_upload(ID)
    assert upload_status() is None


def test_complete_marker_is_refused_once_stopped():
    job = uploads.UploadJob(ID, uploads.new_snapshot_id())
    job.stop.set()
    with pytest.raises(uploads.UploadStopped):
        uploads.send_complete_marker(job)


def test_reader_passes_seek_and_tell_through(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"0123456789")
    job = uploads.UploadJob(ID, "snap")
    with open(path, "rb") as file:
        reader = uploads.StoppableReader(file, job)
        assert reader.seek(0, os.SEEK_END) == 10
        reader.seek(2)
        assert (reader.read(3), reader.tell()) == (b"234", 5)


# ---------------------------------------------------------------------------
# Restoring from the bucket (7b)
# ---------------------------------------------------------------------------

OLDER = "20261101T000000Z-aaaa"
NEWER = "20261102T000000Z-bbbb"
NEWEST_UNFINISHED = "20261103T000000Z-cccc"


def put_in_bucket(fake, snapshot_id, complete=True):
    """Stores a snapshot's three files in the fake bucket, each saying which snapshot it came from."""
    for name in FILE_NAMES:
        fake.objects[f"{ID}/{snapshot_id}/{name}"] = f"{snapshot_id}:{name}".encode()
    if complete:
        fake.objects[f"{ID}/{snapshot_id}/{uploads.COMPLETE_MARKER}"] = b""


def local_contents():
    """Gives what each local snapshot file holds, or None for one that's missing."""
    contents = {}
    for path in uploads.snapshot_files(ID):
        contents[os.path.basename(path)] = open(path, "rb").read() if os.path.exists(path) else None
    return contents


def partial_files():
    """Lists any half-downloaded files left in the sandbox's folders."""
    folders = [host_manager.sandbox_dir(ID), host_manager.snapshot_dir(ID)]
    names = [name for folder in folders for name in os.listdir(folder)]
    return [name for name in names if name.endswith(uploads.PARTIAL_SUFFIX)]


def delete_local_snapshot():
    """Removes the sandbox's local snapshot files, as if they were lost."""
    for path in uploads.snapshot_files(ID):
        os.remove(path)


def test_restore_downloads_the_newest_complete_copy(bucket):
    """Checks the newest folder with the complete marker wins, and a newer unfinished one is skipped."""
    put_in_bucket(bucket, OLDER)
    put_in_bucket(bucket, NEWER)
    put_in_bucket(bucket, NEWEST_UNFINISHED, complete=False)
    delete_local_snapshot()

    uploads.restore_from_bucket(ID)

    assert local_contents() == {name: f"{NEWER}:{name}".encode() for name in FILE_NAMES}
    assert partial_files() == []


def test_restore_does_nothing_without_a_complete_copy(bucket):
    """Checks unfinished copies are never used."""
    put_in_bucket(bucket, NEWEST_UNFINISHED, complete=False)
    delete_local_snapshot()

    uploads.restore_from_bucket(ID)

    assert set(local_contents().values()) == {None}


def test_restore_does_nothing_without_a_bucket(bucket, monkeypatch):
    """Checks a dev host with no SNAPSHOT_BUCKET never touches storage."""
    monkeypatch.delenv(uploads.BUCKET_ENV)
    monkeypatch.setattr(uploads, "storage_bucket", lambda: pytest.fail("storage was used"))
    delete_local_snapshot()

    uploads.restore_from_bucket(ID)

    assert not uploads.has_complete_copy(ID)


def test_a_failed_download_changes_no_local_file(bucket):
    """Checks files from two snapshots never mix: nothing is replaced unless all three downloaded."""
    before = local_contents()
    put_in_bucket(bucket, NEWER)
    bucket.failures_left["mem"] = 1

    with pytest.raises(ConnectionError):
        uploads.restore_from_bucket(ID)

    assert local_contents() == before


def test_has_complete_copy_needs_the_complete_marker(bucket):
    """Checks only a whole copy counts as something the bucket can restore."""
    put_in_bucket(bucket, NEWEST_UNFINISHED, complete=False)
    assert not uploads.has_complete_copy(ID)

    put_in_bucket(bucket, OLDER)
    assert uploads.has_complete_copy(ID)


# ---------------------------------------------------------------------------
# Deleting bucket copies (6b): on every delete of a sandbox
# ---------------------------------------------------------------------------

def test_delete_bucket_copies_removes_only_that_sandboxs_folder(bucket):
    """Checks every pause's copy of the sandbox goes, and other sandboxes' copies stay."""
    put_in_bucket(bucket, OLDER)
    put_in_bucket(bucket, NEWER)
    other = f"sbx_{'d' * 32}/{OLDER}/mem"
    bucket.objects[other] = b"keep"

    uploads.delete_bucket_copies(ID)

    assert list(bucket.objects) == [other]


def test_delete_bucket_copies_does_nothing_without_a_bucket(monkeypatch):
    """Checks a dev host with no SNAPSHOT_BUCKET never touches storage."""
    monkeypatch.delenv(uploads.BUCKET_ENV, raising=False)
    monkeypatch.setattr(uploads, "storage_bucket", lambda: pytest.fail("storage was used"))

    uploads.delete_bucket_copies(ID)


def test_unit_tests_never_point_at_a_real_bucket():
    """Checks no test can upload to the host's real bucket, even when the host's .env sets SNAPSHOT_BUCKET."""
    assert uploads.bucket_name() == ""
