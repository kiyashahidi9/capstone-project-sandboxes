#!/usr/bin/env python3
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from helpers import run_background, run_foreground, write_file_atomically

## CONFIG ##

# where the agent runs and defaults
HOST = os.environ.get("AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("AGENT_PORT", "49983"))

# fallbacks for /exec requests
DEFAULT_CWD = "/root"     # when no "cwd" is given
DEFAULT_TIMEOUT = 600     # seconds; when a foreground run has no "timeout"

# sets HOME if it isn't already set; tools like npm and git need it.
os.environ.setdefault("HOME", "/root")

class BadRequest(Exception):
    """Client error: becomes a 400 with {"error": ...}."""

## REQUEST HANDLER ##

class Handler(BaseHTTPRequestHandler):
    server_version = "guest-agent/1"

    # endpoints

    def health(self) -> None:
        """
        GET /health
        answers {"ok": true} if the agent is up
        """
        self.send(200, {"ok": True})

    def exec_(self) -> None:
        """
        POST /exec {"cmd", "cwd"?, "timeout"?, "background"?}
        runs a shell command in the VM
        background: true -> returns the pid right away
        otherwise -> waits and returns its output
        """
        request = self.json_body()

        # the command to run (required)
        cmd = request.get("cmd")
        if not isinstance(cmd, str) or not cmd:
            raise BadRequest('"cmd" must be a non-empty string')

        # the folder to run it in (optional)
        cwd = request.get("cwd", DEFAULT_CWD)
        if not os.path.isdir(cwd):
            raise BadRequest(f"cwd does not exist: {cwd}")

        # background: start it and return the pid
        if request.get("background"):
            return self.send(200, run_background(cmd, cwd))

        # foreground: wait for it and return its output
        timeout = request.get("timeout", DEFAULT_TIMEOUT)
        self.send(200, run_foreground(cmd, cwd, timeout))

    def read_file(self) -> None:
        """
        GET /files?path=/abs/path
        returns the file's raw contents
        missing file -> 404
        """
        path = self.path_param()

        # no such file
        if not os.path.isfile(path):
            return self.send(404, {"error": f"no such file: {path}"})

        # send the bytes as-is
        with open(path, "rb") as f:
            self.send(200, f.read(), "application/octet-stream")

    def write_file(self) -> None:
        """
        PUT /files?path=/abs/path  (body = file contents)
        writes the file, creating parent folders
        """
        path = self.path_param()

        # write the request body to the file
        write_file_atomically(path, self.body())

        self.send(200, {"ok": True})

    # ------------------------------------------------------------ routing

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


## ROUTES ##

ROUTES = {
    ("GET", "/health"): Handler.health,
    ("POST", "/exec"): Handler.exec_,
    ("GET", "/files"): Handler.read_file,
    ("PUT", "/files"): Handler.write_file,
}

## RUN ##

if __name__ == "__main__":

    # listen on HOST:PORT; each request gets its own Handler on its own thread
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    # a long /exec never blocks stopping the agent
    server.daemon_threads = True

    # flush=True: show up in the systemd logs right away
    print(f"guest agent listening on {HOST}:{PORT}", flush=True)

    server.serve_forever()
