"""example_agent/agent_openai.py - the 1h example agent, using OpenAI.

    python example_agent/agent_openai.py "create hello.py that prints hello, and run it"

Needs: pip install openai httpx, $OPENAI_API_KEY set,
and the API (or mock) running. Optional: SANDBOX_API (default http://localhost:8000).
"""
import json
import os
import sys

from openai import OpenAI

# Let `python example_agent/agent_openai.py` find the sdk package in the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sdk.sandbox import Sandbox, SandboxError  # noqa: E402

# ---------------------------------------------------------------- settings

MODEL = "gpt-4.1"     # any OpenAI model with tool calling
MAX_STEPS = 50
MAX_OUTPUT = 10_000   # characters of stdout/stderr sent back to the model per command

SYSTEM_PROMPT = (
    "You are a coding agent working in a Linux sandbox. Use the tools to do the task. "
    "Start long-running servers with background=true."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the sandbox (working directory /root). "
                           "Returns exit_code, stdout and stderr. With background=true it returns "
                           "a pid immediately and output goes to /tmp/bg-<pid>.log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "The shell command to run."},
                    "background": {"type": "boolean", "description": "Start it and return immediately."},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file in the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute path."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file in the sandbox, creating parent folders. Overwrites it if it exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path."},
                    "content": {"type": "string", "description": "The full new contents."},
                },
                "required": ["path", "content"],
            },
        },
    },
]


# ---------------------------------------------------------------- tools

def run_tool(sbx: Sandbox, name: str, args: dict) -> str:
    """
    Runs one tool call from the model
    returns the text sent back to the model
    errors go back as text too, so the model can recover
    """
    try:
        if name == "run_command":
            result = sbx.exec(args["cmd"], background=args.get("background", False))
            # keep huge outputs from flooding the model
            for key in ("stdout", "stderr"):
                if len(result.get(key, "")) > MAX_OUTPUT:
                    result[key] = result[key][:MAX_OUTPUT] + "\n...[truncated]"
            return json.dumps(result)

        if name == "read_file":
            return sbx.read_file(args["path"])

        if name == "write_file":
            sbx.write_file(args["path"], args["content"])
            return "ok"

        return f"error: unknown tool {name}"

    except SandboxError as e:
        return f"error: {e.detail}"
    except KeyError as e:
        return f"error: missing argument {e}"


def print_step(name: str, args: dict, output: str) -> None:
    """Prints one step, e.g. [run_command] npm install, plus the start of its output."""
    print(f"[{name}] {args.get('cmd') or args.get('path', '')}")
    for line in output[:500].splitlines()[:5]:
        print(f"      {line}")


# ---------------------------------------------------------------- the loop

def run_agent(sbx: Sandbox, task: str) -> str:
    """
    Runs the agent on one task
    loops: ask the model -> run the tools it asks for -> repeat
    returns the model's final message
    """
    client = OpenAI()   # reads $OPENAI_API_KEY
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    for _ in range(MAX_STEPS):
        response = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
        message = response.choices[0].message
        messages.append(message)

        # no tool calls: the agent is done
        if not message.tool_calls:
            return message.content or ""

        # run each tool and send its result back to the model
        for call in message.tool_calls:
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                args = {}
            output = run_tool(sbx, call.function.name, args)
            print_step(call.function.name, args, output)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

    return f"Stopped after {MAX_STEPS} steps without finishing."


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: python example_agent/agent_openai.py "<task>"')
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("set the OPENAI_API_KEY environment variable")

    sbx = Sandbox.create()
    print(f"sandbox {sbx.id} ready")
    try:
        print(run_agent(sbx, sys.argv[1]))
    finally:
        sbx.destroy()
