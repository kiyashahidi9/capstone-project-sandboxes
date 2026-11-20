"""Playback of stored turns (sdk/history.py). The model and the tools are not called."""

import json

from api.models import EventType
from runner.agent_state import messages_through
from sdk.history import TURN_ENDED_EARLY, assistant_message, tool_message


def event(kind: EventType, data: dict) -> dict:
    return {"type": kind, "data": data}


WRITE_CALL = {
    "tool_call_id": "call_1",
    "name": "write_file",
    "arguments": {"path": "hello.py", "content": "print('hi')"},
}
COMMAND_CALL = {"tool_call_id": "call_2", "name": "run_command", "arguments": {"cmd": "python hello.py"}}
COMMAND_RESULT = {"exit_code": 0, "stdout": "hi\n", "stderr": ""}

# the same calls in OpenAI's format
WRITE_CALL_OPENAI = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "write_file", "arguments": json.dumps(WRITE_CALL["arguments"])},
}
COMMAND_CALL_OPENAI = {
    "id": "call_2",
    "type": "function",
    "function": {"name": "run_command", "arguments": json.dumps(COMMAND_CALL["arguments"])},
}

TURN = [
    event(EventType.USER_MESSAGE, {"text": "do the thing"}),
    event(EventType.TURN_STARTED, {}),
    event(EventType.LLM_REQUEST, {"model": "gpt-4.1", "messages": [{"role": "user", "content": "do the thing"}]}),
    event(EventType.LLM_RESPONSE, {"tool_calls": [WRITE_CALL]}),
    event(EventType.TOOL_STARTED, WRITE_CALL),
    event(EventType.TOOL_COMPLETED, {"tool_call_id": "call_1", "result": "ok"}),
    event(EventType.LLM_RESPONSE, {"text": "done"}),
    event(EventType.AGENT_OUTPUT, {"text": "done"}),     # display only
    event(EventType.TURN_FINISHED, {"exit_code": 0}),
]


## ONE TURN ##

def test_each_stored_return_extends_the_chat() -> None:
    after_request = messages_through(TURN[:3])
    after_tool_call = messages_through(TURN[:4])
    after_result = messages_through(TURN[:6])
    after_text = messages_through(TURN)

    assert after_request == [{"role": "user", "content": "do the thing"}]
    assert after_tool_call == [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": None, "tool_calls": [WRITE_CALL_OPENAI]},
        {"role": "tool", "tool_call_id": "call_1", "content": TURN_ENDED_EARLY},   # not answered yet
    ]
    assert after_result == [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": None, "tool_calls": [WRITE_CALL_OPENAI]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    assert after_text == after_result + [{"role": "assistant", "content": "done"}]


def test_a_command_result_is_sent_as_json() -> None:
    events = [
        event(EventType.USER_MESSAGE, {"text": "run it"}),
        event(EventType.LLM_RESPONSE, {"text": "running it", "tool_calls": [COMMAND_CALL]}),
        event(EventType.TOOL_COMPLETED, {"tool_call_id": "call_2", "result": COMMAND_RESULT}),
    ]

    assert messages_through(events) == [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": "running it", "tool_calls": [COMMAND_CALL_OPENAI]},
        {"role": "tool", "tool_call_id": "call_2", "content": json.dumps(COMMAND_RESULT)},
    ]


def test_a_result_for_no_open_call_is_left_out() -> None:
    events = [
        event(EventType.USER_MESSAGE, {"text": "run it"}),
        event(EventType.TOOL_COMPLETED, {"tool_call_id": "call_9", "result": "stray"}),
    ]

    assert messages_through(events) == [{"role": "user", "content": "run it"}]


## ACROSS TURNS ##

def test_a_failed_turn_keeps_its_steps_and_the_next_turn_follows() -> None:
    events = [
        event(EventType.USER_MESSAGE, {"text": "do the thing"}),
        event(EventType.LLM_RESPONSE, {"tool_calls": [WRITE_CALL, COMMAND_CALL]}),
        event(EventType.TOOL_COMPLETED, {"tool_call_id": "call_1", "result": "ok"}),
        event(EventType.TURN_FAILED, {"reason": "exit"}),           # died before call_2 finished
        event(EventType.USER_MESSAGE, {"text": "try again"}),
    ]

    assert messages_through(events) == [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": None, "tool_calls": [WRITE_CALL_OPENAI, COMMAND_CALL_OPENAI]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "tool", "tool_call_id": "call_2", "content": TURN_ENDED_EARLY},
        {"role": "user", "content": "try again"},
    ]


def test_a_turn_without_steps_uses_its_plain_output() -> None:
    events = [
        event(EventType.USER_MESSAGE, {"text": "hi"}),
        event(EventType.TURN_STARTED, {}),
        event(EventType.AGENT_OUTPUT, {"text": "[run] echo hi"}),
        event(EventType.AGENT_OUTPUT, {"text": "hi"}),
        event(EventType.QUESTION, {"text": "anything else?"}),
    ]

    assert messages_through(events) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "[run] echo hi\nhi\nanything else?"},
    ]


def test_a_question_after_steps_is_an_assistant_message() -> None:
    events = [
        event(EventType.USER_MESSAGE, {"text": "start the server"}),
        event(EventType.LLM_RESPONSE, {"text": "on it"}),
        event(EventType.QUESTION, {"text": "which port?"}),
    ]

    assert messages_through(events)[-1] == {"role": "assistant", "content": "which port?"}


## SINGLE MESSAGES ##

def test_arguments_that_were_not_json_stay_as_text() -> None:
    message = assistant_message({"tool_calls": [{"tool_call_id": "c", "name": "run_command", "arguments": "{broken"}]})

    assert message["tool_calls"][0]["function"]["arguments"] == "{broken"


def test_a_text_result_is_sent_as_is() -> None:
    assert tool_message("c", "file contents") == {"role": "tool", "tool_call_id": "c", "content": "file contents"}
