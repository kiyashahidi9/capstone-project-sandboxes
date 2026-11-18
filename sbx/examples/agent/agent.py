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


def history_to_messages(events: list[dict]) -> list[dict]:
    """
    Turns earlier events back into a chat for the model
    user_message -> a user turn
    agent_output and question lines -> joined into one assistant turn
    """
    messages: list[dict] = []

    for event in events:
        text = event["data"].get("text", "")

        if event["type"] == "user_message":
            messages.append({"role": "user", "content": text})

        elif event["type"] in ("agent_output", "question"):
            if messages and messages[-1]["role"] == "assistant":
                messages[-1]["content"] += "\n" + text
            else:
                messages.append({"role": "assistant", "content": text})

    return messages


## STEPS (docs/agent_contract.md, read by docs/observability.md) ##

def step(kind: str, **data) -> None:
    """Prints one step as a JSON line; the runner stores it as its own event, and the chat doesn't show it."""
    print(json.dumps({"type": kind, **data}, default=str))


def as_dict(message) -> dict:
    """A chat message as plain data: ours are dicts already, the model's replies are objects."""
    if isinstance(message, dict):
        return message
    return message.model_dump(exclude_none=True)


def parse_arguments(text: str):
    """The model's tool arguments as data, or the raw text if they aren't valid JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return text


## TOOL RUNNER ##

def run_tool(sandbox: Sandbox, name: str, args: dict) -> tuple[str, object]:
    """
    Runs one tool call from the model in the sandbox
    prints what it's doing
    returns the result as text for the model, and the result to record in tool_completed
    """
    if name == "run_command":
        background = args.get("background", False)
        print(f"[run] {args['cmd']}" + ("   (background)" if background else ""))

        result = sandbox.exec(args["cmd"], cwd=WORKDIR, background=background)

        output = result.get("stdout", "") + result.get("stderr", "")
        for line in output.splitlines()[:5]:
            print(f"      {line}")

        return json.dumps(result), result

    path = absolute_path(args["path"])

    if name == "read_file":
        print(f"[read] {path}")
        content = sandbox.read_file(path)
        return content, content

    print(f"[write] {path}")
    sandbox.write_file(path, args["content"])
    return "ok", {"path": path, "content": args["content"]}


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

    history = load_history(os.environ.get("SANDBOX_HISTORY"))
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += history_to_messages(history)
    messages.append({"role": "user", "content": task})

    for _ in range(MAX_STEPS):
        # record the request before sending it, so a hang or crash in the call shows as unfinished
        step("llm_request", model=MODEL, system=SYSTEM_PROMPT, tools=TOOLS, messages=[as_dict(m) for m in messages[1:]])
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )
        message = response.choices[0].message
        messages.append(message)

        if not message.tool_calls:
            step("llm_response", text=message.content or "")
            print(message.content or "")    # plain text too, so the chat shows the answer
            return EXIT_FINISHED

        calls = [
            {"tool_call_id": call.id, "name": call.function.name, "arguments": parse_arguments(call.function.arguments)}
            for call in message.tool_calls
        ]
        if message.content:
            step("llm_response", text=message.content, tool_calls=calls)
        else:
            step("llm_response", tool_calls=calls)

        for call, recorded in zip(message.tool_calls, calls):
            step("tool_started", **recorded)
            try:
                args = json.loads(call.function.arguments)
                output, result = run_tool(sandbox, call.function.name, args)
            except Exception as e:
                output = f"error: {e}"
                result = output
            step("tool_completed", tool_call_id=call.id, result=result)

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": output,
            })

    print(f"stopped after {MAX_STEPS} steps without finishing")
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
