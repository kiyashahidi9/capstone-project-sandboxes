"""
The model providers an agent can reach through the API's key proxy (api/llm_proxy.py)
the agent never holds a provider key: its SDK is pointed at the proxy, and its turn token
stands in for the key; the proxy swaps in the real key from the agent's secrets.env
adding a provider is one more entry in PROVIDERS
"""

from __future__ import annotations

from dataclasses import dataclass


PROXY_PREFIX = "/llm"   # the proxy's routes: /llm/<provider>/<path>


@dataclass(frozen=True)
class Provider:
    """One model provider: where it lives, how it reads a key, and which calls the proxy lets through."""

    name: str                       # in the proxy's path, e.g. /llm/openai/...
    upstream: str                   # the provider's real address
    key_name: str                   # the line in secrets.env that holds the key
    key_header: str                 # the header the provider reads the key from
    key_prefix: str                 # written before the key in that header
    allowed_paths: frozenset[str]   # model calls only; account and admin calls are refused
    base_url_env: str               # the variable the provider's SDK reads its address from
    base_url_suffix: str            # what that SDK expects after /llm/<name>


PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        name="openai",
        upstream="https://api.openai.com",
        key_name="OPENAI_API_KEY",
        key_header="authorization",
        key_prefix="Bearer ",
        allowed_paths=frozenset({"v1/chat/completions", "v1/responses", "v1/completions", "v1/embeddings"}),
        base_url_env="OPENAI_BASE_URL",
        base_url_suffix="/v1",
    ),
    "anthropic": Provider(
        name="anthropic",
        upstream="https://api.anthropic.com",
        key_name="ANTHROPIC_API_KEY",
        key_header="x-api-key",
        key_prefix="",
        allowed_paths=frozenset({"v1/messages", "v1/messages/count_tokens"}),
        base_url_env="ANTHROPIC_BASE_URL",
        base_url_suffix="",
    ),
}


def find_provider(name: str) -> Provider | None:
    """Looks up a provider by the name used in the proxy's path, or gives nothing for an unknown one."""
    return PROVIDERS.get(name)


def proxy_base_url(api: str, provider: Provider) -> str:
    """Gives the address an agent's SDK should use for this provider, e.g. http://127.0.0.1:8000/llm/openai/v1."""
    return f"{api}{PROXY_PREFIX}/{provider.name}{provider.base_url_suffix}"


def agent_environment(api: str, token: str) -> dict[str, str]:
    """Gives the variables that point every provider's SDK at the proxy, with the turn token in place of a key."""
    environment: dict[str, str] = {}
    for provider in PROVIDERS.values():
        environment[provider.key_name] = token
        environment[provider.base_url_env] = proxy_base_url(api, provider)
    return environment
