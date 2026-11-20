"""The key proxy: an agent's model calls reach the provider with the client's key, which the agent never holds."""

import httpx
import pytest
from fastapi.testclient import TestClient

import agent_secrets
import llm_providers
from api import auth, db, llm_proxy
from api.main import app


ADMIN_KEY = "test-admin-key"
SANDBOX_ID = "sbx_" + "a" * 32
REAL_OPENAI_KEY = "sk-real-openai"
REAL_ANTHROPIC_KEY = "sk-ant-real"
CHAT = "/llm/openai/v1/chat/completions"
MESSAGES = "/llm/anthropic/v1/messages"
CHAT_BODY = b'{"model": "gpt-4.1", "messages": []}'


## SETUP ##

class FakeProvider:
    """Stands in for the provider's servers: remembers each call and gives a set answer."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.body = b'{"ok": true}'
        self.content_type = "application/json"
        self.unreachable = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Records the call and answers it, or fails to connect when told to."""
        self.calls.append(request)
        if self.unreachable:
            raise httpx.ConnectError("no route", request=request)
        return httpx.Response(self.status, content=self.body, headers={"content-type": self.content_type})


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    """Points the proxy at a fake provider instead of the internet."""
    fake = FakeProvider()
    monkeypatch.setattr(llm_proxy, "upstream_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    return fake


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    """Writes the client's secrets.env with a key for each provider."""
    path = tmp_path / "secrets.env"
    path.write_text(f"OPENAI_API_KEY={REAL_OPENAI_KEY}\nANTHROPIC_API_KEY='{REAL_ANTHROPIC_KEY}'\n")
    monkeypatch.setenv(agent_secrets.SECRETS_FILE_ENV, str(path))
    return path


@pytest.fixture
def client(tmp_path, monkeypatch, provider, secrets_file):
    """Calls the API with no key of its own, as an agent does."""
    monkeypatch.setenv("SBX_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", ADMIN_KEY)
    db.create_tables()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def token():
    """Gives a running turn's token, and ends the turn after the test."""
    turn_token = auth.new_turn_token(SANDBOX_ID)
    yield turn_token
    auth.drop_turn_token(turn_token)


def bearer(key: str) -> dict[str, str]:
    """Gives the header the OpenAI SDK sends its key in."""
    return {"Authorization": f"Bearer {key}"}


## FORWARDING ##

def test_an_openai_call_reaches_openai_with_the_real_key_not_the_token(client, provider, token):
    response = client.post(CHAT, content=CHAT_BODY, headers={**bearer(token), "content-type": "application/json"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    (call,) = provider.calls
    assert str(call.url) == "https://api.openai.com/v1/chat/completions"
    assert call.headers["authorization"] == f"Bearer {REAL_OPENAI_KEY}"
    assert call.content == CHAT_BODY
    assert token not in str(call.headers)


def test_an_anthropic_call_uses_x_api_key_both_ways(client, provider, token):
    headers = {"x-api-key": token, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    response = client.post(MESSAGES, content=b"{}", headers=headers)

    assert response.status_code == 200
    (call,) = provider.calls
    assert str(call.url) == "https://api.anthropic.com/v1/messages"
    assert call.headers["x-api-key"] == REAL_ANTHROPIC_KEY
    assert call.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in call.headers


def test_only_safe_headers_are_passed_on(client, provider, token):
    client.post(CHAT, content=CHAT_BODY, headers={**bearer(token), "cookie": "session=1", "x-forwarded-for": "10.0.0.1"})

    (call,) = provider.calls
    assert "cookie" not in call.headers
    assert "x-forwarded-for" not in call.headers


def test_a_query_string_is_kept(client, provider, token):
    client.post(MESSAGES + "?beta=true", content=b"{}", headers={"x-api-key": token})

    (call,) = provider.calls
    assert str(call.url) == "https://api.anthropic.com/v1/messages?beta=true"


def test_a_streamed_answer_comes_back_as_a_stream(client, provider, token):
    provider.content_type = "text/event-stream"
    provider.body = b"data: one\n\ndata: two\n\n"

    response = client.post(CHAT, content=CHAT_BODY, headers=bearer(token))

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b"data: one\n\ndata: two\n\n"


def test_the_providers_own_error_reaches_the_agent_unchanged(client, provider, token):
    provider.status = 401
    provider.body = b'{"error": {"message": "Incorrect API key provided"}}'

    response = client.post(CHAT, content=CHAT_BODY, headers=bearer(token))

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Incorrect API key provided"


def test_a_replaced_key_is_used_on_the_next_call(client, provider, token, secrets_file):
    secrets_file.write_text("OPENAI_API_KEY=sk-new-key\n")

    client.post(CHAT, content=CHAT_BODY, headers=bearer(token))

    assert provider.calls[-1].headers["authorization"] == "Bearer sk-new-key"


def test_an_unreachable_provider_answers_502(client, provider, token):
    provider.unreachable = True

    response = client.post(CHAT, content=CHAT_BODY, headers=bearer(token))

    assert response.status_code == 502


## REFUSALS ##

@pytest.mark.parametrize("headers", [{}, bearer(ADMIN_KEY), bearer("made-up"), {"x-api-key": "made-up"}])
def test_only_a_running_turns_token_may_call_models(client, provider, headers):
    response = client.post(CHAT, content=CHAT_BODY, headers=headers)

    assert response.status_code == 401
    assert provider.calls == []


def test_a_token_stops_working_when_its_turn_ends(client, provider):
    ended = auth.new_turn_token(SANDBOX_ID)
    auth.drop_turn_token(ended)

    response = client.post(CHAT, content=CHAT_BODY, headers=bearer(ended))

    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/llm/openai/v1/files", "/llm/openai/v1/organization/api_keys", "/llm/anthropic/v1/models"])
def test_calls_other_than_model_calls_are_refused(client, provider, token, path):
    response = client.post(path, content=b"{}", headers={**bearer(token), "x-api-key": token})

    assert response.status_code == 403
    assert provider.calls == []


def test_an_unknown_provider_is_404(client, provider, token):
    response = client.post("/llm/nobody/v1/chat", content=b"{}", headers=bearer(token))

    assert response.status_code == 404


def test_a_missing_key_says_where_to_put_it(client, provider, token, secrets_file):
    secrets_file.write_text("ANTHROPIC_API_KEY=sk-ant-only\n")

    response = client.post(CHAT, content=CHAT_BODY, headers=bearer(token))

    assert response.status_code == 401
    assert "OPENAI_API_KEY" in response.json()["detail"]
    assert "secrets.env" in response.json()["detail"]
    assert provider.calls == []


## THE AGENT'S SIDE ##

def test_the_agent_gets_proxy_addresses_and_its_token_in_place_of_every_key():
    environment = llm_providers.agent_environment("http://127.0.0.1:8000", "turn-token")

    assert environment == {
        "OPENAI_API_KEY": "turn-token",
        "OPENAI_BASE_URL": "http://127.0.0.1:8000/llm/openai/v1",
        "ANTHROPIC_API_KEY": "turn-token",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000/llm/anthropic",
    }


## FINDING SECRETS.ENV ##

def test_secrets_env_is_in_the_clients_agent_folder(monkeypatch):
    monkeypatch.delenv(agent_secrets.SECRETS_FILE_ENV, raising=False)
    monkeypatch.setenv("AGENT_DIR", "/opt/agents/my-bot")

    assert agent_secrets.secrets_path() == "/opt/agents/my-bot/secrets.env"


def test_secrets_env_is_the_example_agents_without_a_client_agent(monkeypatch):
    monkeypatch.delenv(agent_secrets.SECRETS_FILE_ENV, raising=False)
    monkeypatch.setenv("AGENT_DIR", "")

    assert agent_secrets.secrets_path() == "/opt/agents/secrets.env"


def test_a_missing_secrets_file_has_no_keys(tmp_path, monkeypatch):
    monkeypatch.setenv(agent_secrets.SECRETS_FILE_ENV, str(tmp_path / "nothing-here.env"))

    assert agent_secrets.read_secret("OPENAI_API_KEY") is None


def test_a_blank_key_counts_as_missing(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text("OPENAI_API_KEY=\n")
    monkeypatch.setenv(agent_secrets.SECRETS_FILE_ENV, str(path))

    assert agent_secrets.read_secret("OPENAI_API_KEY") is None
