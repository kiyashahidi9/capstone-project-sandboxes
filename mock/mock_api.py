"""Mock of the 1g API, for building the 1h SDK and agent without Firecracker.

Same endpoints, JSON and status codes as the API contract. Every "sandbox" forwards
exec/files to ONE guest agent running in Docker, so all sandboxes share one filesystem
here. That's fine for SDK and agent work; isolation is tested in 1f, not here.

Run from the repo root (Git Bash on Windows):
  pip install fastapi uvicorn httpx python-dotenv
  MSYS_NO_PATHCONV=1 docker run --rm -p 49983:49983 -v "$(pwd -W)/guest_agent:/app" -w /app python:3.12 python guest_agent.py
  uvicorn mock.mock_api:app --port 8000

On Linux/macOS, drop MSYS_NO_PATHCONV=1 and use "$PWD" instead of "$(pwd -W)".
The guest agent must run in Docker: it needs Linux (process groups, /tmp, /root).

Switch to the real API later by pointing the SDK at it: SANDBOX_API=http://<host>:8000
"""
import os
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response

load_dotenv()   # read .env from the repo root (shell variables still win)

GUEST = os.environ.get("GUEST_AGENT", "http://127.0.0.1:49983")   # the guest agent in Docker
MAX_SANDBOXES = 250

app = FastAPI(title="mock sandbox API")
sandboxes: dict[str, dict] = {}    # id -> sandbox record


# ---------------------------------------------------------------- endpoints

@app.post("/sandboxes", status_code=201)
def create_sandbox():
    in_use = {sb["n"] for sb in sandboxes.values() if sb["state"] != "destroyed"}
    free = [n for n in range(1, MAX_SANDBOXES + 1) if n not in in_use]
    if not free:
        raise HTTPException(503, "no free sandbox numbers")
    n, now = free[0], int(time.time())
    sb = {"id": f"sbx{n}", "n": n, "state": "running", "ip": f"10.201.0.{n}",
          "created_at": now, "updated_at": now}
    sandboxes[sb["id"]] = sb   # replaces a destroyed record with the same id
    return public(sb)


@app.get("/sandboxes")
def list_sandboxes():
    return [public(sb) for sb in sandboxes.values() if sb["state"] != "destroyed"]


@app.get("/sandboxes/{id}")
def get(id: str):
    return public(get_sandbox(id))


@app.post("/sandboxes/{id}/exec")
async def exec_(id: str, request: Request):
    get_running(id)
    body = await request.json()
    r = await guest("POST", "/exec", timeout=body.get("timeout", 600) + 10, json=body)
    return r.json()


@app.get("/sandboxes/{id}/files")
async def read_file(id: str, path: str):
    get_running(id)
    r = await guest("GET", "/files", params={"path": path})
    if r.status_code == 404:
        raise HTTPException(404, f"no such file: {path}")
    return Response(r.content, media_type="text/plain")


@app.put("/sandboxes/{id}/files")
async def write_file(id: str, path: str, request: Request):
    get_running(id)
    await guest("PUT", "/files", params={"path": path}, content=await request.body())
    return {"ok": True}


@app.delete("/sandboxes/{id}")
def destroy(id: str):
    sb = get_sandbox(id)
    if sb["state"] == "destroyed":
        raise HTTPException(409, f"{id} is already destroyed")
    sb["state"], sb["updated_at"] = "destroyed", int(time.time())
    return {"ok": True}


# ---------------------------------------------------------------- helpers

def public(sb: dict) -> dict:
    """The sandbox object every endpoint returns (see the contract)."""
    return {k: sb[k] for k in ("id", "state", "ip", "created_at", "updated_at")}


def get_sandbox(id: str) -> dict:
    sb = sandboxes.get(id)
    if sb is None:
        raise HTTPException(404, f"no sandbox {id}")
    return sb


def get_running(id: str) -> dict:
    sb = get_sandbox(id)
    if sb["state"] != "running":
        raise HTTPException(409, f"{id} is {sb['state']}")
    return sb


async def guest(method: str, path: str, timeout: float = 30, **kwargs) -> httpx.Response:
    """Forward to the guest agent, mapping its errors the way the real API does."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, GUEST + path, **kwargs)
    except httpx.ConnectError:
        raise HTTPException(502, f"guest agent not reachable at {GUEST} (start it with docker, see top of file)")
    if r.status_code >= 400 and r.status_code != 404:
        try:
            detail = r.json().get("error", r.text)
        except ValueError:
            detail = r.text
        raise HTTPException(400 if r.status_code < 500 else 502, detail)
    return r
