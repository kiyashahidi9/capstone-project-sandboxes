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

Smoke test against a running API:  python -m sdk.sandbox
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
    """The API answered with an error (status code + its detail message)."""

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
    """
    Sends one HTTP request to the API
    error response -> raises SandboxError
    success -> returns the response
    used by every Sandbox method
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
    def __init__(self, id: str, api: str = DEFAULT_API):
        self.id = id
        self.api = api.rstrip("/")
        self._url = f"{self.api}/sandboxes/{id}"

    def __repr__(self) -> str:
        return f"Sandbox(id={self.id!r}, api={self.api!r})"

    @classmethod
    def create(cls, api: str = DEFAULT_API) -> "Sandbox":
        """
        Starts a new sandbox
        blocks until its VM has booted
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
        """
        Runs a shell command in the sandbox
        foreground -> {"exit_code", "stdout", "stderr", ...}; check exit_code, it won't raise
        background -> {"pid"}; output in /tmp/bg-<pid>.log
        """

        url = f"{self._url}/exec"
        body = {"cmd": cmd, "cwd": cwd, "background": background}
        res = _call_api("POST", url, timeout=EXEC_TIMEOUT, json=body)

        return res.json()

    def read_file(self, path: str) -> str:
        """
        Reads a file in the sandbox
        missing file -> SandboxError (404)
        """

        url = f"{self._url}/files"
        params = {"path": path}
        res = _call_api("GET", url, params=params)

        return res.text

    def write_file(self, path: str, content: str) -> None:
        """
        Writes a file in the sandbox
        creates parent folders if needed
        """

        url = f"{self._url}/files"
        params = {"path": path}
        data = content.encode()

        _call_api("PUT", url, params=params, content=data)

    def destroy(self) -> None:
        """
        Stops the sandbox
        deletes everything in it
        """

        _call_api("DELETE", self._url)


if __name__ == "__main__":
    sbx = Sandbox.create()
    print("created", sbx)
    try:
        print("exec      ", sbx.exec("echo hi"))
        sbx.write_file("/root/sdk-test/x.txt", "hello")
        print("read_file ", repr(sbx.read_file("/root/sdk-test/x.txt")))
        print("background", sbx.exec("sleep 5", background=True))
    finally:
        sbx.destroy()
        print("destroyed", sbx.id)
