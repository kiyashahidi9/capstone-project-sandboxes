import os
import httpx
from dotenv import load_dotenv

# read .env from the repo root (shell variables still win);
# a locked-down agent can't read it (2f) and gets its settings from the environment instead
try:
    load_dotenv()
except OSError:
    pass

DEFAULT_API = os.environ.get("SANDBOX_API", "http://localhost:8000")

# Creating waits for the VM to boot; exec can run up to the guest agent's 600 s default.
CREATE_TIMEOUT = 120
EXEC_TIMEOUT = 660
DEFAULT_TIMEOUT = 30

# A pause writes the VM's whole memory to disk (~15 s, more if the guest is slow to sync).
# File calls and destroy may wait for a pause under way, then resume, so they get the same allowance.
PAUSE_TIMEOUT = 120


class SandboxError(Exception):
    """Raised when the sandbox API responds with an error status.

    Attributes:
        status: The HTTP status code, e.g. 404.
        detail: The error message returned by the API.
    """

    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def _call_api(
        method: str, 
        url: str, 
        timeout: float = DEFAULT_TIMEOUT, 
        **kwargs
        ) -> httpx.Response:
    """Send one HTTP request to the sandbox API.

    Used internally by every `Sandbox` method.

    Args:
        method: The HTTP method, e.g. "POST".
        url: The full URL to call.
        timeout: Seconds to wait for a response.
        **kwargs: Passed through to `httpx.request` (json, params, content).

    Returns:
        The successful response.

    Raises:
        SandboxError: If the API responds with a 4xx or 5xx status.
    """

    res = httpx.request(method, url, timeout=timeout, **kwargs)
    
    if res.is_error:
        # use the API's {"detail": "..."} message; fall back to the raw text if it isn't JSON
        try:
            detail = res.json().get("detail", res.text)
        except ValueError:
            detail = res.text
        raise SandboxError(res.status_code, str(detail))
    
    return res


def auth_headers(token: str | None) -> dict[str, str]:
    """The Authorization header for a token, or no headers without one."""
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token}"}


class Sandbox:
    """A sandbox: an isolated Linux VM managed by the sandbox API.

    Start one with `Sandbox.create()` and call `destroy()` when you are done.

    A paused sandbox wakes up by itself: `exec()`, `read_file()` and
    `write_file()` resume it first, so those calls are just a little slower.

    Every method raises `SandboxError` when the API responds with an error:

    - 400: Bad request, e.g. a relative path or a `cwd` that does not exist.
    - 404: No sandbox exists with this id.
    - 409: The sandbox is in the wrong state for this call, e.g. it has been
      destroyed, or you paused a sandbox that is already paused.
    - 502: The API could not reach the guest agent inside the VM.

    If the API itself cannot be reached, httpx exceptions such as
    `httpx.ConnectError` and `httpx.TimeoutException` are raised unchanged.

    Attributes:
        id: The sandbox id, e.g. "sbx1".
        api: The base URL of the sandbox API.
    """

    def __init__(self, id: str, api: str = DEFAULT_API, token: str | None = None):
        """Attach to an existing sandbox by id.

        This makes no API call, so it does not check that the sandbox exists.
        To start a new sandbox, use `Sandbox.create()` instead.

        Args:
            id: The id of an existing sandbox, e.g. "sbx1".
            api: The base URL of the sandbox API. Defaults to `$SANDBOX_API`.
            token: Sent as `Authorization: Bearer <token>` on every request,
                if given.
        """
        self.id = id
        self.api = api.rstrip("/")
        self._url = f"{self.api}/sandboxes/{id}"
        self._headers = auth_headers(token)

    @classmethod
    def from_env(cls) -> "Sandbox":
        """Attach to the sandbox an agent was started for.

        Reads `SANDBOX_ID`, `SANDBOX_API` and `SANDBOX_TOKEN`, which the agent
        runner sets for each turn (see docs/agent_contract.md).

        Returns:
            The sandbox, ready to use.

        Raises:
            RuntimeError: If `SANDBOX_ID` is not set.
        """

        sandbox_id = os.environ.get("SANDBOX_ID")
        if not sandbox_id:
            raise RuntimeError("SANDBOX_ID is not set")

        api = os.environ.get("SANDBOX_API", DEFAULT_API)
        token = os.environ.get("SANDBOX_TOKEN") or None   # an empty value means no token

        return cls(sandbox_id, api, token)

    def __repr__(self) -> str:
        return f"Sandbox(id={self.id!r}, api={self.api!r})"

    @classmethod
    def create(cls, api: str = DEFAULT_API, token: str | None = None) -> "Sandbox":
        """Start a new sandbox.

        Blocks until the sandbox's VM has booted, for up to `CREATE_TIMEOUT` seconds.

        Args:
            api: The base URL of the sandbox API. Defaults to `$SANDBOX_API`.
            token: Sent as `Authorization: Bearer <token>`, if given.

        Returns:
            The new, running sandbox.

        Raises:
            SandboxError: 503 if no sandboxes are available.
        """

        url = f"{api.rstrip('/')}/sandboxes"
        res = _call_api("POST", url, timeout=CREATE_TIMEOUT, headers=auth_headers(token))
        sandbox_id = res.json()["id"]

        return cls(sandbox_id, api, token)

    def exec(
            self, 
            cmd: str, 
            cwd: str = "/root", 
            background: bool = False
            ) -> dict:
        """Run a shell command in the sandbox.

        The command runs as root through `sh -c`. By default this waits for the
        command to finish. With `background=True` it returns immediately, which
        is how servers and other long-running processes should be started.

        A command that fails does not raise an exception; check `exit_code`.

        Args:
            cmd: The shell command to run, e.g. "npm install".
            cwd: The directory to run it in. It must already exist.
            background: If True, start the command and return without waiting.
                Its output is written to `/tmp/bg-<pid>.log`, which can be read
                with `read_file()`. It keeps running until it exits or the
                sandbox is destroyed.

        Returns:
            In the foreground, a dict with `exit_code`, `stdout`, `stderr` and
            `timed_out`. Commands are killed after 600 seconds, in which case
            `exit_code` is 124 and `timed_out` is True.

            In the background, a dict with the process's `pid`.

        Raises:
            SandboxError: 400 if `cwd` does not exist.
        """

        url = f"{self._url}/exec"
        body = {"cmd": cmd, "cwd": cwd, "background": background}
        res = _call_api("POST", url, timeout=EXEC_TIMEOUT, json=body, headers=self._headers)

        return res.json()

    def read_file(self, path: str) -> str:
        """Read a text file from the sandbox.

        Intended for text files; binary content will not decode correctly.

        Args:
            path: The absolute path of the file, e.g. "/root/app.py".

        Returns:
            The file's contents.

        Raises:
            SandboxError: 404 if the file does not exist, or 400 if `path` is
                not absolute.
        """

        url = f"{self._url}/files"
        params = {"path": path}
        res = _call_api("GET", url, timeout=PAUSE_TIMEOUT, params=params, headers=self._headers)

        return res.text

    def write_file(self, path: str, content: str) -> None:
        """Write a text file to the sandbox.

        Creates any missing parent directories and overwrites the file if it
        already exists. The write is atomic: readers never see a partially
        written file.

        Args:
            path: The absolute path of the file, e.g. "/root/app.py".
            content: The full text to write.

        Raises:
            SandboxError: 400 if `path` is not absolute.
        """

        url = f"{self._url}/files"
        params = {"path": path}
        data = content.encode()

        _call_api("PUT", url, timeout=PAUSE_TIMEOUT, params=params, content=data, headers=self._headers)

    def send_message(self, text: str) -> int:
        """Send the user's message to the sandbox's agent.

        Returns once the turn has started; the agent works in the background
        on the host. Follow its progress with `events()`. A paused sandbox is
        resumed first, so the call is a little slower.

        Args:
            text: The message, e.g. "create hello.py and run it".

        Returns:
            The message's `seq` in the event log.

        Raises:
            SandboxError: 409 if a turn is already running, or the sandbox is
                still starting or has been destroyed.
        """

        url = f"{self._url}/messages"
        res = _call_api("POST", url, timeout=PAUSE_TIMEOUT, json={"text": text}, headers=self._headers)

        return res.json()["seq"]

    def events(self, after: int = 0) -> list[dict]:
        """Get the sandbox's events with a `seq` greater than `after`.

        Args:
            after: The last `seq` already seen; 0 for everything.

        Returns:
            The new events, oldest first. Each has `seq`, `type`, `data` and
            `at` (unix seconds).
        """

        url = f"{self._url}/events"
        res = _call_api("GET", url, params={"after": after}, headers=self._headers)

        return res.json()["events"]

    def pause(self) -> None:
        """Save the sandbox to disk and free its memory.

        Everything is kept: files, running processes and the agent's
        conversation. An agent turn that is running is cut off. Blocks until
        the pause is done, usually about 15 seconds.

        Raises:
            SandboxError: 409 if the sandbox is not running (for example,
                already paused).
        """

        _call_api("POST", f"{self._url}/pause", timeout=PAUSE_TIMEOUT, headers=self._headers)

    def resume(self) -> None:
        """Bring a paused sandbox back exactly where it left off.

        Usually not needed: `exec()`, `read_file()` and `write_file()` resume a
        paused sandbox by themselves. Takes about 1-2 seconds.

        Raises:
            SandboxError: 409 if the sandbox is not paused.
        """

        _call_api("POST", f"{self._url}/resume", timeout=PAUSE_TIMEOUT, headers=self._headers)

    def destroy(self) -> None:
        """Stop the sandbox and delete everything in it.

        All of its files and running processes, including background ones, are
        removed. The sandbox cannot be used afterwards. If a pause is under
        way, this waits for it to finish first. Calling it twice is harmless.
        """

        _call_api("DELETE", self._url, timeout=PAUSE_TIMEOUT, headers=self._headers)
