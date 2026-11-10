"""Mock of the 1g API, for building the 1h SDK and agent without Firecracker.

Same endpoints, JSON and status codes as the API contract. Every "sandbox" forwards
exec/files to ONE local guest agent, so all sandboxes share one filesystem here.
That's fine for SDK and agent work; isolation is tested in 1f, not here.

Run:
  pip install fastapi uvicorn httpx
  docker run --rm -p 49983:49983 -v "$PWD/guest_agent:/app" python:3.12 python /app/guest_agent.py
  uvicorn dev.mock_api:app --port 8000

Swap to the real API later by pointing the SDK at it (SANDBOX_API=http://<host>:8000).
"""
import time

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

GUEST = "http://127.0.0.1:49983"   # the local guest agent (Docker)
MAX_SANDBOXES = 250

app = FastAPI(title="mock sandbox API")
sandboxes: dict[str, dict] = {}    # id -> sandbox object, as in the contract


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
    """Forward to the guest agent, mapping its errors like the real API does."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, GUEST + path, **kwargs)
    except httpx.ConnectError:
        raise HTTPException(502, "guest agent not running on :49983 (start it with docker, see top of file)")
    if r.status_code >= 400 and r.status_code != 404:
        detail = r.json().get("error", r.text) if r.headers.get("content-type") == "application/json" else r.text
        raise HTTPException(400 if r.status_code < 500 else 502, detail)
    return r


@app.post("/sandboxes", status_code=201)
def create_sandbox():
    in_use = {sb["n"] for sb in sandboxes.values() if sb["state"] != "destroyed"}
    free = [n for n in range(1, MAX_SANDBOXES + 1) if n not in in_use]
    if not free:
        raise HTTPException(503, "no free sandbox numbers")
    n, now = free[0], int(time.time())
    sb = {"id": f"sbx{n}", "n": n, "state": "running", "ip": f"10.201.0.{n}",
          "created_at": now, "updated_at": now}
    sandboxes[sb["id"]] = sb   # replaces a destroyed row with the same id
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


def public(sb: dict) -> dict:
    return {k: sb[k] for k in ("id", "state", "ip", "created_at", "updated_at")}
