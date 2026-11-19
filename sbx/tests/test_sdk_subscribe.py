"""Sandbox.subscribe follows one sandbox's event stream."""

import httpx
import pytest

from sdk.sandbox import Sandbox, SandboxError


SANDBOX_ID = "sbx_" + "a" * 32


class FakeStream:
    """A streaming response the SDK can enter and read line by line."""

    def __init__(self, lines: list[str], status: int = 200, body: str = "") -> None:
        self._lines = lines
        self.status_code = status
        self._body = body
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        self.closed = True
        return False

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def read(self) -> bytes:
        return self._body.encode()

    def json(self) -> dict:
        return httpx.Response(self.status_code, text=self._body).json()

    @property
    def text(self) -> str:
        return self._body

    def iter_lines(self):
        yield from self._lines


def test_subscribe_yields_each_event_and_sends_the_last_seq(monkeypatch) -> None:
    stream = FakeStream([
        "",
        '{"seq": 4, "type": "turn_started", "data": {}, "at": 10}',
        "data: {\"seq\": 5, \"type\": \"turn_finished\", \"data\": {\"exit_code\": 0}, \"at\": 11}",
        ": keep-alive",
    ])
    seen = {}

    def fake_stream(method, url, params, headers, timeout):
        seen["method"] = method
        seen["url"] = url
        seen["params"] = params
        seen["headers"] = headers
        seen["timeout"] = timeout
        return stream

    monkeypatch.setattr(httpx, "stream", fake_stream)
    sandbox = Sandbox(SANDBOX_ID, api="http://api.test", token="admin-key")

    assert list(sandbox.subscribe(after=3)) == [
        {"seq": 4, "type": "turn_started", "data": {}, "at": 10},
        {"seq": 5, "type": "turn_finished", "data": {"exit_code": 0}, "at": 11},
    ]
    assert seen == {
        "method": "GET",
        "url": f"http://api.test/sandboxes/{SANDBOX_ID}/events/stream",
        "params": {"after": 3},
        "headers": {"Authorization": "Bearer admin-key"},
        "timeout": seen["timeout"],
    }
    assert seen["timeout"].read is None
    assert stream.closed


def test_subscribe_raises_the_api_error(monkeypatch) -> None:
    stream = FakeStream([], status=404, body='{"detail": "Sandbox not found"}')
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: stream)
    sandbox = Sandbox(SANDBOX_ID, api="http://api.test")

    with pytest.raises(SandboxError) as raised:
        list(sandbox.subscribe())

    assert raised.value.status == 404
    assert raised.value.detail == "Sandbox not found"


def test_closing_subscribe_closes_the_stream(monkeypatch) -> None:
    stream = FakeStream([
        '{"seq": 1, "type": "user_message", "data": {"text": "hi"}, "at": 1}',
        '{"seq": 2, "type": "turn_finished", "data": {"exit_code": 0}, "at": 2}',
    ])
    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: stream)
    sandbox = Sandbox(SANDBOX_ID, api="http://api.test")

    subscription = sandbox.subscribe()
    assert next(subscription)["seq"] == 1
    subscription.close()

    assert stream.closed
