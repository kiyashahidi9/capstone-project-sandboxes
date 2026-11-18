import os
import signal
import subprocess
import tempfile
import threading

from agent_types import BackgroundResult, ExecResult

# exit code reported when a command is killed for running too long
TIMEOUT_EXIT_CODE = 124


## COMMAND RUNNERS ##

def run_foreground(cmd: str, cwd: str, timeout: float) -> ExecResult:
    """
    Runs a shell command
    waits for it to finish
    kills if timed out
    returns everything it printed
    used by /exec request
    """
    timed_out = False

    process = subprocess.Popen(
        cmd, shell=True,
        cwd=cwd, text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        out, err = process.communicate(timeout=timeout)

    except subprocess.TimeoutExpired:
        timed_out = True

        os.killpg(process.pid, signal.SIGKILL)

        out, err = process.communicate()

    return {
        "exit_code": TIMEOUT_EXIT_CODE if timed_out else process.returncode,
        "stdout": out,
        "stderr": err,
        "timed_out": timed_out,
    }


def run_background(cmd: str, cwd: str) -> BackgroundResult:
    """
    Starts a shell command
    does NOT wait for it to finish
    returns its pid right away
    output goes to /tmp/bg-<pid>.log
    used by /exec request with "background": true
    """

    fd, tmp = tempfile.mkstemp(prefix="bg-", suffix=".log", dir="/tmp")

    with os.fdopen(fd, "w") as log:
        process = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    os.rename(tmp, f"/tmp/bg-{process.pid}.log")

    cleanup = threading.Thread(target=process.wait, daemon=True)
    cleanup.start()

    return {"pid": process.pid}


## FILE HELPERS ##

def write_file_atomically(path: str, data: bytes) -> None:
    """
    Writes `data` to the file at `path`
    creates parent folders if needed
    readers never see a half-written file
    used by PUT /files
    """
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)

    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)

    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        os.chmod(tmp, 0o644)

        os.replace(tmp, path)

    except BaseException:
        os.unlink(tmp)
        raise
