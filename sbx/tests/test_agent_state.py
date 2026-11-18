"""Playback of one stored turn. The model and the tools are not called."""

import json

from api.models import EventType
from runner.agent_state import messages_through


def event(kind: EventType, data: dict) -> dict:
    return {"type": kind, "data": data}


WRITE_CALL = {
    "tool_call_id": "call_1",
    "name": "write_file",
    "arguments": {"path": "hello.py", "content": "print('hi')"},
}
WRITE_RESULT = {"path": "hello.py", "content": "print('hi')"}
COMMAND_RESULT = {"exit_code": 0, "stdout": "hi\n", "stderr": ""}

TURN = [
    event(EventType.USER_MESSAGE, {"text": "do the thing"}),
    event(EventType.TURN_STARTED, {}),
    event(EventType.LLM_REQUEST, {
        "model": "gpt-4.1",
        "system": "You are a coding agent.",
        "tools": [],
        "messages": [{"role": "user", "content": "do the thing"}],
    }),
    event(EventType.LLM_RESPONSE, {"tool_calls": [WRITE_CALL]}),
    event(EventType.TOOL_STARTED, WRITE_CALL),
    event(EventType.TOOL_COMPLETED, {"tool_call_id": "call_1", "result": WRITE_RESULT}),
    event(EventType.LLM_RESPONSE, {"text": "done"}),
    event(EventType.AGENT_OUTPUT, {"text": "still plain text"}),
    event(EventType.QUESTION, {"text": "which port?"}),
]


def test_each_stored_return_extends_the_chat() -> None:
    after_request = messages_through(TURN[:3])
    after_tool_call = messages_through(TURN[:4])
    after_start = messages_through(TURN[:5])
    after_result = messages_through(TURN[:6])
    after_text = messages_through(TURN)

    assert after_request == [{"role": "user", "content": "do the thing"}]
    assert after_tool_call == [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": None, "tool_calls": [WRITE_CALL]},
    ]
    assert after_start == after_tool_call
    assert after_result == [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": None, "tool_calls": [WRITE_CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(WRITE_RESULT)},
    ]
    assert after_text == [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": None, "tool_calls": [WRITE_CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(WRITE_RESULT)},
        {"role": "assistant", "content": "done"},
    ]


def test_a_command_result_keeps_its_exit_and_output() -> None:
    events = [
        event(EventType.USER_MESSAGE, {"text": "run it"}),
        event(EventType.TOOL_COMPLETED, {"tool_call_id": "call_2", "result": COMMAND_RESULT}),
    ]

    assert messages_through(events) == [
        {"role": "user", "content": "run it"},
        {"role": "tool", "tool_call_id": "call_2", "content": json.dumps(COMMAND_RESULT)},
    ]


def test_turn_failed_stops_on_the_previous_step() -> None:
    events = [
        event(EventType.USER_MESSAGE, {"text": "do the thing"}),
        event(EventType.LLM_RESPONSE, {"tool_calls": [WRITE_CALL]}),
        event(EventType.TOOL_COMPLETED, {"tool_call_id": "call_1", "result": WRITE_RESULT}),
        event(EventType.TURN_FAILED, {"reason": "exit"}),
        event(EventType.LLM_RESPONSE, {"text": "this return was not stored in time"}),
    ]

    assert messages_through(events) == [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": None, "tool_calls": [WRITE_CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(WRITE_RESULT)},
    ]
