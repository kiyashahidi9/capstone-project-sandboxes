"""example_agent/agent_openai.py - the 1h example agent, using OpenAI.

    python -m example_agent.agent_openai "create hello.py that prints hello, and run it"

Run from the repo root.

Needs: pip install openai httpx python-dotenv, OPENAI_API_KEY in .env,
and the API (or mock) running. Optional: SANDBOX_API (default http://localhost:8000).
"""

## IMPORTS ##

import json
import sys

from dotenv import load_dotenv
from openai import OpenAI

from example_agent.tools import TOOLS
from sdk.sandbox import Sandbox, SandboxError

## CONFIG ##

# reads OPENAI_API_KEY from .env (shell variables still win)
load_dotenv()

MODEL = "gpt-4.1"       # any OpenAI model with tool calling
MAX_STEPS = 50          # most model calls per task, so a confused agent can't loop forever
MAX_OUTPUT = 10_000     # characters of stdout/stderr sent back to the model per command

SYSTEM_PROMPT = (
    "You are a coding agent working in a Linux sandbox. "
    "Use the tools to do the task. "
    "Start long-running servers with background=true."
)


## TOOL RUNNER ##

def run_tool(sandbox: Sandbox, name: str, args: dict) -> str:
    """
    Runs one tool call from the model in the sandbox
    returns the result as text for the model
    errors go back as text too, so the model can recover
    """
    try:
        if name == "run_command":
            result = sandbox.exec(args["cmd"], background=args.get("background", False))

            # keep huge outputs from flooding the model
            for key in ("stdout", "stderr"):
                if len(result.get(key, "")) > MAX_OUTPUT:
                    result[key] = result[key][:MAX_OUTPUT] + "\n...[truncated]"

            return json.dumps(result)

        if name == "read_file":
            return sandbox.read_file(args["path"])

        if name == "write_file":
            sandbox.write_file(args["path"], args["content"])
            return "ok"

        return f"error: unknown tool {name}"

    except SandboxError as e:
        return f"error: {e.detail}"

    except KeyError as e:
        return f"error: missing argument {e}"


def print_step(name: str, args: dict, output: str) -> None:
    """
    Prints one tool call to the terminal
    e.g. [run] npm install, plus the first few lines of its output
    errors are printed too
    """
    if name == "run_command":
        background = " (background)" if args.get("background") else ""
        print(f"[run] {args.get('cmd', '')}{background}")

    elif name == "read_file":
        print(f"[read] {args.get('path', '')}")

    elif name == "write_file":
        print(f"[write] {args.get('path', '')}")

    else:
        print(f"[{name}]")

    # errors: show the message
    if output.startswith("error:"):
        print(f"      {output}")

    # commands: show the first few lines of what they printed
    elif name == "run_command":
        result = json.loads(output)
        printed = result.get("stdout", "") + result.get("stderr", "")
        for line in printed.splitlines()[:5]:
            print(f"      {line}")


## AGENT LOOP ##

def run_agent(sandbox: Sandbox, task: str) -> str:
    """
    Runs the agent on one task
    loops: ask the model -> run the tools it asks for -> repeat
    returns the model's final message
    """
    client = OpenAI()

    # the whole conversation, sent to the model on every call
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    for _ in range(MAX_STEPS):
        # ask the model what to do next
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )
        message = response.choices[0].message
        messages.append(message)

        # no tool calls: the model is done
        if not message.tool_calls:
            return message.content or ""

        # run each tool call and send the result back to the model
        for call in message.tool_calls:
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError:
                args = {}       # run_tool reports the missing arguments to the model

            output = run_tool(sandbox, call.function.name, args)
            print_step(call.function.name, args, output)

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": output,
            })

    return f"Stopped after {MAX_STEPS} steps without finishing."


## CLI ##

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: python -m example_agent.agent_openai "<task>"')

    sandbox = Sandbox.create()
    print(f"sandbox {sandbox.id} ready")

    try:
        print(run_agent(sandbox, sys.argv[1]))

    finally:
        # always clean up, even on Ctrl+C or a crash
        sandbox.destroy()
