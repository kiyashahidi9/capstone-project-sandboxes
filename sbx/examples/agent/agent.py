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


## STEPS ##

def emit(event: dict) -> None:
    """Prints one step as a single JSON line. The runner stores that line as an event."""
    print(json.dumps(event), flush=True)


def to_jsonable(value):
    """Turns a model message into plain JSON so the request line can be stored."""
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    return value


def call_record(call) -> dict:
    """The stored shape of one tool call: id, name, and arguments as an object when the JSON parses."""
    raw: str = call.function.arguments
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError:
        arguments = raw
    return {"tool_call_id": call.id, "name": call.function.name, "arguments": arguments}


def result_text(result: dict | str) -> str:
    """The tool message the model reads. An object result is stored as JSON text."""
    if isinstance(result, str):
        return result
    return json.dumps(result)


## TOOL RUNNER ##

def run_tool(sandbox: Sandbox, name: str, args: dict) -> dict | str:
    """
    Runs one tool call from the model in the sandbox
    prints what it's doing
    returns the result to store and to show the model
    """
    if name == "run_command":
        background = args.get("background", False)
        print(f"[run] {args['cmd']}" + ("   (background)" if background else ""))

        result: dict = sandbox.exec(args["cmd"], cwd=WORKDIR, background=background)

        output = result.get("stdout", "") + result.get("stderr", "")
        for line in output.splitlines()[:5]:
            print(f"      {line}")

        return result

    path = absolute_path(args["path"])

    if name == "read_file":
        print(f"[read] {path}")
        return {"path": path, "content": sandbox.read_file(path)}

    print(f"[write] {path}")
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
    returns the exit code: 0 when finished, 1 when it gave up
    """
    messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += history_to_messages(history)
    messages.append({"role": "user", "content": task})

    for _ in range(MAX_STEPS):
        emit({
            "type": "llm_request",
            "model": MODEL,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "messages": to_jsonable(messages),
        })
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )
        message = response.choices[0].message
        messages.append(message)

        if not message.tool_calls:
            text: str = message.content or ""
            emit({"type": "llm_response", "text": text})
            print(text)
            return EXIT_FINISHED

        records: list[dict] = [call_record(call) for call in message.tool_calls]
        emit({"type": "llm_response", "tool_calls": records})

        for record in records:
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
            messages.append({
                "role": "tool",
                "tool_call_id": record["tool_call_id"],
                "content": result_text(result),
            })

    print(f"stopped after {MAX_STEPS} steps without finishing")
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
