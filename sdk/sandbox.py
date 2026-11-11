"""sdk/sandbox.py - thin Python wrapper over the sandbox API (Milestone 1h).

Each method is one HTTP call to the API; there is no other logic here.

    from sdk.sandbox import Sandbox

    sbx = Sandbox.create()                      # POST   /sandboxes
    sbx.exec("echo hi")                         # POST   /sandboxes/{id}/exec
    sbx.write_file("/root/x.txt", "hello")      # PUT    /sandboxes/{id}/files?path=
    sbx.read_file("/root/x.txt")                # GET    /sandboxes/{id}/files?path=
    sbx.destroy()                               # DELETE /sandboxes/{id}

The API address defaults to $SANDBOX_API, else http://localhost:8000
(the mock API locally, the real API on the host later: no code changes).
"""
import os
import httpx
from dotenv import load_dotenv

load_dotenv()   # read .env from the repo root (shell variables still win)

DEFAULT_API = os.environ.get("SANDBOX_API", "http://localhost:8000")

# Creating waits for the VM to boot; exec can run up to the guest agent's 600 s default.
CREATE_TIMEOUT = 120
EXEC_TIMEOUT = 660
DEFAULT_TIMEOUT = 30


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


class Sandbox:
    """A sandbox: an isolated Linux VM managed by the sandbox API.

    Start one with `Sandbox.create()` and call `destroy()` when you are done.

    Every method raises `SandboxError` when the API responds with an error:

    - 400: Bad request, e.g. a relative path or a `cwd` that does not exist.
    - 404: No sandbox exists with this id.
    - 409: The sandbox has already been destroyed.
    - 502: The API could not reach the guest agent inside the VM.

    If the API itself cannot be reached, httpx exceptions such as
    `httpx.ConnectError` and `httpx.TimeoutException` are raised unchanged.

    Attributes:
        id: The sandbox id, e.g. "sbx1".
        api: The base URL of the sandbox API.
    """

    def __init__(self, id: str, api: str = DEFAULT_API):
        """Attach to an existing sandbox by id.

        This makes no API call, so it does not check that the sandbox exists.
        To start a new sandbox, use `Sandbox.create()` instead.

        Args:
            id: The id of an existing sandbox, e.g. "sbx1".
            api: The base URL of the sandbox API. Defaults to `$SANDBOX_API`.
        """
        self.id = id
        self.api = api.rstrip("/")
        self._url = f"{self.api}/sandboxes/{id}"

    def __repr__(self) -> str:
        return f"Sandbox(id={self.id!r}, api={self.api!r})"

    @classmethod
    def create(cls, api: str = DEFAULT_API) -> "Sandbox":
        """Start a new sandbox.

        Blocks until the sandbox's VM has booted, for up to `CREATE_TIMEOUT` seconds.

        Args:
            api: The base URL of the sandbox API. Defaults to `$SANDBOX_API`.

        Returns:
            The new, running sandbox.

        Raises:
            SandboxError: 503 if no sandboxes are available.
        """

        url = f"{api.rstrip('/')}/sandboxes"
        res = _call_api("POST", url, timeout=CREATE_TIMEOUT)
        sandbox_id = res.json()["id"]

        return cls(sandbox_id, api)

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
        res = _call_api("POST", url, timeout=EXEC_TIMEOUT, json=body)

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
        res = _call_api("GET", url, params=params)

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

        _call_api("PUT", url, params=params, content=data)

    def destroy(self) -> None:
        """Stop the sandbox and delete everything in it.

        All of its files and running processes, including background ones, are
        removed. The sandbox cannot be used afterwards.

        Raises:
            SandboxError: 409 if the sandbox has already been destroyed.
        """

        _call_api("DELETE", self._url)
