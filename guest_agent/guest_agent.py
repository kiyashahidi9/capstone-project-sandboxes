#!/usr/bin/env python3
"""guest_agent.py - small HTTP server inside the VM (Milestone 1e).

Everything outside the VM runs commands and reads/writes files through this.
Same port as E2B's envd (49983). Standard library only, so nothing needs
installing inside the VM.

Endpoints:
  GET  /health                       -> {"ok": true}
  POST /exec   {"cmd", "cwd"?, "timeout"?}
                                     -> {"exit_code", "stdout", "stderr", "timed_out"}
  POST /exec   {"cmd", "cwd"?, "background": true}
                                     -> {"pid"}  (output in /tmp/bg-<pid>.log)
  GET  /files?path=/abs/path         -> raw file contents, or 404
  PUT  /files?path=/abs/path  body   -> {"ok": true}  (creates parent folders)

No auth: only the host can reach the VM (10.201.0.N is routed on the host only).
"""
import json
import os
import signal
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("AGENT_PORT", "49983"))
DEFAULT_CWD = "/root"
DEFAULT_TIMEOUT = 600  # seconds

# systemd starts services without HOME; tools like npm and git need it.
os.environ.setdefault("HOME", "/root")


class BadRequest(Exception):
    """Client error: becomes a 400 with {"error": ...}."""


def run_foreground(cmd, cwd, timeout):
    # Own process group, so a timeout kills the shell AND everything it started.
    # (subprocess.run(..., timeout=) only kills the shell; children like npm keep
    # the output pipes open and the request hangs.)
    proc = subprocess.Popen(
        cmd, shell=True, cwd=cwd, text=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return {"exit_code": proc.returncode, "stdout": out, "stderr": err, "timed_out": False}
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return {"exit_code": 124, "stdout": out, "stderr": err, "timed_out": True}


def run_background(cmd, cwd):
    # The pid isn't known until the process starts, so write to a temp file and
    # rename it to /tmp/bg-<pid>.log afterwards (the open file follows the rename).
    fd, tmp = tempfile.mkstemp(prefix="bg-", suffix=".log", dir="/tmp")
    with os.fdopen(fd, "w") as log:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    os.rename(tmp, f"/tmp/bg-{proc.pid}.log")
    # Reap it when it exits, so finished background jobs don't pile up as zombies.
    threading.Thread(target=proc.wait, daemon=True).start()
    return {"pid": proc.pid}


class Handler(BaseHTTPRequestHandler):
    server_version = "guest-agent/1"

    # ------------------------------------------------------------ helpers

    def send(self, code, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def json_body(self):
        try:
            data = json.loads(self.body() or b"{}")
        except json.JSONDecodeError as e:
            raise BadRequest(f"invalid JSON: {e}")
        if not isinstance(data, dict):
            raise BadRequest("body must be a JSON object")
        return data

    def path_param(self):
        path = parse_qs(urlparse(self.path).query).get("path", [""])[0]
        if not path.startswith("/"):
            raise BadRequest("?path= must be an absolute path")
        return path

    def route(self, method):
        handler = ROUTES.get((method, urlparse(self.path).path))
        if handler is None:
            return self.send(404, {"error": "not found"})
        try:
            handler(self)
        except BadRequest as e:
            self.send(400, {"error": str(e)})
        except Exception as e:  # keep the agent alive whatever happens
            self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    # ------------------------------------------------------------ endpoints

    def health(self):
        self.send(200, {"ok": True})

    def exec_(self):
        req = self.json_body()
        cmd = req.get("cmd")
        if not isinstance(cmd, str) or not cmd:
            raise BadRequest('"cmd" must be a non-empty string')
        cwd = req.get("cwd", DEFAULT_CWD)
        if not os.path.isdir(cwd):
            raise BadRequest(f"cwd does not exist: {cwd}")
        if req.get("background"):
            return self.send(200, run_background(cmd, cwd))
        timeout = req.get("timeout", DEFAULT_TIMEOUT)
        self.send(200, run_foreground(cmd, cwd, timeout))

    def read_file(self):
        path = self.path_param()
        if not os.path.isfile(path):
            return self.send(404, {"error": f"no such file: {path}"})
        with open(path, "rb") as f:
            self.send(200, f.read(), "application/octet-stream")

    def write_file(self):
        path = self.path_param()
        data = self.body()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write to a temp file, then rename: readers never see a half-written file.
        tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self.send(200, {"ok": True})


ROUTES = {
    ("GET", "/health"): Handler.health,
    ("POST", "/exec"): Handler.exec_,
    ("GET", "/files"): Handler.read_file,
    ("PUT", "/files"): Handler.write_file,
}


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True  # a long /exec never blocks /health or shutdown
    print(f"guest agent listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()
