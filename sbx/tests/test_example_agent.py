"""The example agent prints a replayable step before each side effect."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def load_agent():
    path = Path(__file__).resolve().parents[1] / "examples" / "agent" / "agent.py"
    spec = importlib.util.spec_from_file_location("example_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AGENT = load_agent()


class ScriptedClient:
    """Returns prepared assistant messages, and records the chat it was given."""

    def __init__(self, messages: list) -> None:
        self.pending: list = list(messages)
        self.seen: list = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, model: str, messages: list, tools: list) -> SimpleNamespace:
        self.seen.append(list(messages))
        message = self.pending.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def assistant(content: str | None = None, tool_calls: list | None = None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)

    def model_dump(exclude_none: bool = False) -> dict:
        dumped: dict = {"role": "assistant", "content": content}
        if tool_calls:
            dumped["tool_calls"] = [
                {"id": call.id, "function": {"name": call.function.name, "arguments": call.function.arguments}}
                for call in tool_calls
            ]
        return dumped

    message.model_dump = model_dump
    return message


def tool_call(call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


class FakeSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def exec(self, cmd: str, cwd: str, background: bool = False) -> dict:
        self.calls.append(("exec", cmd, cwd, background))
        return {"exit_code": 0, "stdout": "hi\n", "stderr": ""}

    def read_file(self, path: str) -> str:
        self.calls.append(("read", path))
        return "file-text"

    def write_file(self, path: str, content: str) -> None:
        self.calls.append(("write", path, content))


def lines(capsys) -> list[dict]:
    """The JSON steps the agent printed. Progress lines (plain text, or agent_output lines) are left out."""
    steps: list[dict] = []
    for line in capsys.readouterr().out.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "type" in parsed and parsed["type"] != "agent_output":
            steps.append(parsed)
    return steps


def test_a_command_turn_prints_the_steps_before_the_side_effect(capsys) -> None:
    sandbox = FakeSandbox()
    client = ScriptedClient([
        assistant(tool_calls=[tool_call("call_1", "run_command", {"cmd": "echo hi"})]),
        assistant(content="done"),
    ])

    code = AGENT.run_turn("do the thing", sandbox, client, [])

    assert code == AGENT.EXIT_FINISHED
    assert sandbox.calls == [("exec", "echo hi", "/root", False)]
    assert [step["type"] for step in lines(capsys)] == [
        "llm_request",
        "llm_response",
        "tool_started",
        "tool_completed",
        "llm_request",
        "llm_response",
    ]


def test_the_stored_tool_call_matches_the_call_that_ran(capsys) -> None:
    sandbox = FakeSandbox()
    client = ScriptedClient([
        assistant(tool_calls=[tool_call("call_1", "write_file", {"path": "hello.py", "content": "print('hi')"})]),
        assistant(content="done"),
    ])

    AGENT.run_turn("write it", sandbox, client, [])

    steps = lines(capsys)
    stored = {
        "tool_call_id": "call_1",
        "name": "write_file",
        "arguments": {"path": "hello.py", "content": "print('hi')"},
    }
    assert steps[1]["tool_calls"] == [stored]
    assert steps[2]["tool_call_id"] == "call_1"
    assert steps[2]["arguments"] == stored["arguments"]
    assert steps[3]["result"] == {"path": "/root/hello.py", "content": "print('hi')"}
    assert sandbox.calls == [("write", "/root/hello.py", "print('hi')")]
    request = steps[0]
    assert request["model"] == "gpt-4.1"
    assert request["system"]
    assert "messages" in request
    assert "tools" in request


def test_a_tool_error_is_a_completed_step(capsys) -> None:
    sandbox = FakeSandbox()
    sandbox.exec = lambda cmd, cwd, background=False: (_ for _ in ()).throw(RuntimeError("disk full"))
    client = ScriptedClient([
        assistant(tool_calls=[tool_call("call_9", "run_command", {"cmd": "echo hi"})]),
        assistant(content="stopped"),
    ])

    AGENT.run_turn("do the thing", sandbox, client, [])

    completed = [step for step in lines(capsys) if step["type"] == "tool_completed"]
    assert completed == [{
        "type": "tool_completed",
        "tool_call_id": "call_9",
        "result": "error: disk full",
    }]
    assert client.seen[1][-1]["content"] == "error: disk full"
