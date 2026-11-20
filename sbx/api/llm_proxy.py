"""
The key proxy: forwards an agent's model calls to its provider, adding the client's key on the way
the agent sends its turn token where its SDK would send a key; the token only works from this host,
only for model calls, and only until the turn ends, so a leaked or printed "key" is worth nothing
the sandbox can't reach this route: it listens on the host's 127.0.0.1, outside every sandbox's network
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

import agent_secrets
from api.auth import sandbox_for_turn_token
from llm_providers import Provider, find_provider


HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_BAD_GATEWAY = 502

# headers the agent's SDK may pass on; everything else, including whatever it sent as a key, is dropped
FORWARDED_HEADERS = ("content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta")
DEFAULT_CONTENT_TYPE = "application/json"
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)   # model answers can be slow
UPDATE_HINT = "add it to secrets.env in your agent folder, then run install.sh --update-secrets"


# ---------------------------------------------------------------------------
# Checks that stop a call early
# ---------------------------------------------------------------------------

def require_provider(name: str) -> Provider:
    """Finds the provider named in the path, or answers 404."""
    provider = find_provider(name)
    if provider is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail=f"Unknown model provider: {name}")
    return provider


def require_allowed_path(provider: Provider, path: str) -> None:
    """Answers 403 unless the call is one of the provider's model calls."""
    if path not in provider.allowed_paths:
        raise HTTPException(status_code=HTTP_FORBIDDEN, detail=f"Only model calls go through the proxy, not {path}")


def token_from(request: Request) -> str:
    """Takes the agent's token from where its SDK put the key: 'Authorization: Bearer' or 'x-api-key'."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    return request.headers.get("x-api-key", "")


def require_turn(request: Request) -> None:
    """Answers 401 unless the call carries the token of a turn that is still running."""
    if sandbox_for_turn_token(token_from(request)) is None:
        raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail="Only a running turn's agent may call models")


def require_key(provider: Provider) -> str:
    """Gives the client's key for this provider, or answers 401 saying where to put it."""
    key = agent_secrets.read_secret(provider.key_name)
    if key is None:
        raise HTTPException(status_code=HTTP_UNAUTHORIZED, detail=f"No {provider.key_name} for this agent: {UPDATE_HINT}")
    return key


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------

def upstream_url(provider: Provider, path: str, query: str) -> str:
    """Gives the provider's real address for this call, keeping any query string."""
    url = f"{provider.upstream}/{path}"
    return f"{url}?{query}" if query else url


def upstream_headers(request: Request, provider: Provider, key: str) -> dict[str, str]:
    """Keeps the safe headers from the agent's call and adds the client's key the way the provider expects."""
    headers = {name: request.headers[name] for name in FORWARDED_HEADERS if name in request.headers}
    headers[provider.key_header] = f"{provider.key_prefix}{key}"
    return headers


def upstream_client() -> httpx.AsyncClient:
    """Makes the HTTP client that talks to providers (tests swap in a fake one)."""
    return httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)


async def send_upstream(client: httpx.AsyncClient, upstream: httpx.Request, provider: Provider) -> httpx.Response:
    """Sends the call to the provider, or answers 502 if the provider can't be reached."""
    try:
        return await client.send(upstream, stream=True)
    except httpx.RequestError as error:
        await client.aclose()
        raise HTTPException(status_code=HTTP_BAD_GATEWAY, detail=f"Couldn't reach {provider.name}") from error


async def close_upstream(response: httpx.Response, client: httpx.AsyncClient) -> None:
    """Closes the provider's answer and the client once the agent has the whole answer."""
    await response.aclose()
    await client.aclose()


async def forward(request: Request, provider: Provider, path: str, key: str) -> StreamingResponse:
    """Sends the agent's call to the provider with the key and streams the answer back, errors included."""
    client = upstream_client()
    upstream = client.build_request(
        "POST",
        upstream_url(provider, path, request.url.query),
        headers=upstream_headers(request, provider, key),
        content=await request.body(),
    )
    response = await send_upstream(client, upstream, provider)
    return StreamingResponse(
        response.aiter_bytes(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type", DEFAULT_CONTENT_TYPE),
        background=BackgroundTask(close_upstream, response, client),
    )
