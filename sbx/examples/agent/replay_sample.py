"""Tiny agent for a manual replay and idle check. Prints the step lines and exits.

    AGENT_CMD="python examples/agent/replay_sample.py"
"""

import json
import sys

sys.stdout.reconfigure(line_buffering=True)
sys.stdin.read()

print(json.dumps({
    "type": "llm_request",
    "model": "gpt-4.1",
    "system": "You are a coding agent.",
    "tools": [],
    "messages": [{"role": "user", "content": "do the thing"}],
}))
print(json.dumps({
    "type": "llm_response",
    "tool_calls": [{
        "tool_call_id": "call_1",
        "name": "run_command",
        "arguments": {"cmd": "echo hi"},
    }],
}))
print(json.dumps({
    "type": "tool_started",
    "tool_call_id": "call_1",
    "name": "run_command",
    "arguments": {"cmd": "echo hi"},
}))
print(json.dumps({
    "type": "tool_completed",
    "tool_call_id": "call_1",
    "result": {"exit_code": 0, "stdout": "hi\n", "stderr": ""},
}))
print(json.dumps({"type": "llm_response", "text": "done"}))
