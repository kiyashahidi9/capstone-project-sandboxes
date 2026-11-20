"""
Example agent that follows docs/agent_contract.md
runs once per message: reads it on stdin, works in the sandbox, prints its steps, exits
try it by hand from sbx/:
    echo "create hello.py that prints hello, and run it" | \
      SANDBOX_ID=<id> SANDBOX_API=http://127.0.0.1:8000 SANDBOX_HISTORY=/dev/null \
      uv run python examples/agent/agent.py
"""

import importlib.util
import json
import os
import posixpath
import sys

from openai import OpenAI

from sdk.history import assistant_message, chat_from_events, tool_message
from sdk.sandbox import Sandbox


def load_build_tools():
    """Loads build_tools from the tools.py next to this file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools.py")
    spec = importlib.util.spec_from_file_location("example_agent_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_tools


build_tools = load_build_tools()


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

def emit(event: dict) -> None:
    """Prints one step as a single JSON line. The runner stores that line as an event."""
    print(json.dumps(event, default=str), flush=True)


def say(text: str) -> None:
    """Shows the user one message, as one agent_output event even when it has many lines; skips blank ones."""
    if text.strip():
        emit({"type": "agent_output", "text": text})


def call_record(call) -> dict:
    """The stored shape of one tool call: id, name, and arguments as an object when the JSON parses."""
    raw: str = call.function.arguments
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError:
        arguments = raw
    return {"tool_call_id": call.id, "name": call.function.name, "arguments": arguments}


## TOOL RUNNER ##

def run_tool(sandbox: Sandbox, name: str, args: dict) -> dict | str:
    """
    Runs one tool call from the model in the sandbox
    prints what it's doing
    returns the result to store and to show the model (text as is, anything else as JSON)
    """
    if name == "run_command":
        background = args.get("background", False)
        say(f"[run] {args['cmd']}" + ("   (background)" if background else ""))

        result: dict = sandbox.exec(args["cmd"], cwd=WORKDIR, background=background)

        # the first few lines of output, as one message
        output = result.get("stdout", "") + result.get("stderr", "")
        say("\n".join(f"      {line}" for line in output.splitlines()[:5]))

        return result

    path = absolute_path(args["path"])

    if name == "read_file":
        say(f"[read] {path}")
        return {"path": path, "content": sandbox.read_file(path)}

    say(f"[write] {path}")
    sandbox.write_file(path, args["content"])
    return {"path": path, "content": args["content"]}


def absolute_path(path: str) -> str:
    """Turns a path relative to WORKDIR into an absolute one; absolute paths pass through."""
    return posixpath.join(WORKDIR, path)


## TURN ##

def run_turn(task: str, sandbox: Sandbox, client: OpenAI, history: list[dict]) -> int:
    """
    Does one turn: the task, with the earlier events in history
    prints each model call and tool result before the next side effect
    builds its chat with sdk/history.py, the same code replay uses, so the record matches what the model was sent
    returns the exit code: 0 when finished, 1 when it gave up
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += chat_from_events(history)
    messages.append({"role": "user", "content": task})

    for _ in range(MAX_STEPS):
        # record the request before sending it, so a hang or crash in the call shows as unfinished
        emit({
            "type": "llm_request",
            "model": MODEL,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "messages": messages[1:],
        })
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
            reply["tool_calls"] = [call_record(call) for call in message.tool_calls]
        emit({"type": "llm_response", **reply})
        messages.append(assistant_message(reply))

        if not message.tool_calls:
            say(message.content or "")      # as output too, so the chat shows the answer
            return EXIT_FINISHED

        for record in reply["tool_calls"]:
            emit({
                "type": "tool_started",
                "tool_call_id": record["tool_call_id"],
                "name": record["name"],
                "arguments": record["arguments"],
            })
            try:
                arguments = record["arguments"]
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments are not a JSON object")
                result: dict | str = run_tool(sandbox, record["name"], arguments)
            except Exception as error:
                result = f"error: {error}"

            emit({
                "type": "tool_completed",
                "tool_call_id": record["tool_call_id"],
                "result": result,
            })
            messages.append(tool_message(record["tool_call_id"], result))

    say(f"stopped after {MAX_STEPS} steps without finishing")
    return EXIT_FAILED


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

    history = load_history(os.environ.get("SANDBOX_HISTORY"))
    return run_turn(task, Sandbox.from_env(), OpenAI(), history)


if __name__ == "__main__":
    sys.exit(main())
