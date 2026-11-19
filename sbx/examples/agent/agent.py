"""
Example agent that follows docs/agent_contract.md
runs once per message: reads it on stdin, works in the sandbox, prints its steps, exits
try it by hand from sbx/:
    echo "create hello.py that prints hello, and run it" | \
      SANDBOX_ID=<id> SANDBOX_API=http://127.0.0.1:8000 SANDBOX_HISTORY=/dev/null \
      uv run python examples/agent/agent.py
"""

import json
import os
import posixpath
import sys

from openai import OpenAI

from sdk.history import assistant_message, chat_from_events, tool_message
from sdk.sandbox import Sandbox
from tools import build_tools   # examples/agent/tools.py, next to this file


## CONFIG ##

MODEL = "gpt-4.1"
MAX_STEPS = 50      # most model calls per turn, so a confused agent can't loop forever

WORKDIR = "/root"   # where commands run and relative file paths point; must exist in the VM
TOOLS = build_tools(WORKDIR)   # the tool descriptions tell the model about WORKDIR

SYSTEM_PROMPT = (
    "You are a coding agent working in a Linux sandbox. "
    "Use the tools to do the task. "
    "Start long-running servers with background=true."
)

EXIT_FINISHED = 0
EXIT_FAILED = 1


## HISTORY ##

def load_history(path: str | None) -> list[dict]:
    """
    Reads the earlier events from SANDBOX_HISTORY
    no path, a missing file or an empty one (e.g. /dev/null) means no history
    """
    if not path or not os.path.exists(path):
        return []

    with open(path, encoding="utf-8") as file:
        text = file.read()

    if not text.strip():
        return []
    return json.loads(text)


## STEPS (docs/agent_contract.md, read by docs/observability.md) ##

def step(kind: str, **data) -> None:
    """Prints one step as a JSON line; the runner stores it as its own event, and the chat doesn't show it."""
    print(json.dumps({"type": kind, **data}, default=str))


def parse_arguments(text: str):
    """The model's tool arguments as data, or the raw text if they aren't valid JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return text


## TOOL RUNNER ##

def run_tool(sandbox: Sandbox, name: str, args: dict) -> object:
    """
    Runs one tool call from the model in the sandbox
    prints what it's doing
    returns exactly what the model is given (recorded in tool_completed): text as is, anything else as JSON
    """
    if name == "run_command":
        background = args.get("background", False)
        print(f"[run] {args['cmd']}" + ("   (background)" if background else ""))

        result = sandbox.exec(args["cmd"], cwd=WORKDIR, background=background)

        output = result.get("stdout", "") + result.get("stderr", "")
        for line in output.splitlines()[:5]:
            print(f"      {line}")

        return result

    path = absolute_path(args["path"])

    if name == "read_file":
        print(f"[read] {path}")
        return sandbox.read_file(path)

    print(f"[write] {path}")
    sandbox.write_file(path, args["content"])
    return "ok"


def absolute_path(path: str) -> str:
    """Turns a path relative to WORKDIR into an absolute one; absolute paths pass through."""
    return posixpath.join(WORKDIR, path)


## TURN ##

def main() -> int:
    """
    Does one turn: the message on stdin, with the earlier turns from SANDBOX_HISTORY
    lets the model call tools until it answers in plain text
    returns the exit code: 0 when finished, 1 when it gave up
    """
    sys.stdout.reconfigure(line_buffering=True)

    task = sys.stdin.read().strip()
    if not task:
        print("no message on stdin", file=sys.stderr)
        return EXIT_FAILED

    sandbox = Sandbox.from_env()
    client = OpenAI()

    # the same rebuild replay uses, so the record matches what the model was really sent
    history = load_history(os.environ.get("SANDBOX_HISTORY"))
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += chat_from_events(history)
    messages.append({"role": "user", "content": task})

    for _ in range(MAX_STEPS):
        # record the request before sending it, so a hang or crash in the call shows as unfinished
        step("llm_request", model=MODEL, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages[1:])
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )
        message = response.choices[0].message

        # record the reply, then add the recorded version to the chat, so the two can't differ
        reply: dict = {}
        if message.content or not message.tool_calls:
            reply["text"] = message.content or ""
        if message.tool_calls:
            reply["tool_calls"] = [
                {"tool_call_id": call.id, "name": call.function.name, "arguments": parse_arguments(call.function.arguments)}
                for call in message.tool_calls
            ]
        step("llm_response", **reply)
        messages.append(assistant_message(reply))

        if not message.tool_calls:
            print(message.content or "")    # plain text too, so the chat shows the answer
            return EXIT_FINISHED

        for call, recorded in zip(message.tool_calls, reply["tool_calls"]):
            step("tool_started", **recorded)
            try:
                args = json.loads(call.function.arguments)
                result = run_tool(sandbox, call.function.name, args)
            except Exception as e:
                result = f"error: {e}"
            step("tool_completed", tool_call_id=call.id, result=result)
            messages.append(tool_message(call.id, result))

    print(f"stopped after {MAX_STEPS} steps without finishing")
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
