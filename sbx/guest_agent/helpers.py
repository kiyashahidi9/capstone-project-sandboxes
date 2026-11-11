"""helpers.py - the real work behind the guest agent's endpoints.

guest_agent.py handles HTTP; these functions run commands and write files.
Standard library only, like the agent itself.
"""
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

    # Start the command in the shell, inside the folder `cwd`.
    process = subprocess.Popen(
        cmd, shell=True,
        cwd=cwd, text=True,
        stdin=subprocess.DEVNULL,       # no input, so it can't get stuck typing
        stdout=subprocess.PIPE,         # output captured
        stderr=subprocess.PIPE,         # error captured
        start_new_session=True,         # gets its own process group for timeout cleanup
    )

    try:
        # wait for it to finish and collect its output (gives up after `timeout`)
        out, err = process.communicate(timeout=timeout)

    except subprocess.TimeoutExpired:
        timed_out = True

        # too slow: kill the shell and everything it started,
        os.killpg(process.pid, signal.SIGKILL)

        # collect the output and error
        out, err = process.communicate()

    # return process output
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

    # log file for the output; random name for now, since the pid doesn't exist yet
    fd, tmp = tempfile.mkstemp(prefix="bg-", suffix=".log", dir="/tmp")

    # start the command in the shell, inside `cwd`
    with os.fdopen(fd, "w") as log:
        process = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,       # no input, so it can't get stuck typing
            stdout=log,                     # output goes to the log file
            stderr=subprocess.STDOUT,       # errors go to the same log file
            start_new_session=True,         # own process group, so it can be killed as a whole
        )

    # now we know the pid: rename the log to /tmp/bg-<pid>.log (the command keeps writing to it)
    os.rename(tmp, f"/tmp/bg-{process.pid}.log")

    # clean up when it exits, so finished jobs don't pile up as zombies
    cleanup = threading.Thread(target=process.wait, daemon=True)
    cleanup.start()

    # return the pid so the caller can check on it or stop it
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

    # Make a temp file in the same folder (renaming is only all-or-nothing within one folder's filesystem).
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)

    try:
        # write everything to the temp file first
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        # mkstemp makes the file private (0600); give it normal permissions
        os.chmod(tmp, 0o644)

        # swap it into place in one step
        os.replace(tmp, path)

    except BaseException:
        # something failed: don't leave a stray temp file behind
        os.unlink(tmp)
        raise
