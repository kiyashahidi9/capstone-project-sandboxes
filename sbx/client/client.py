"""Terminal chat: type a task, an OpenAI agent does it in a sandbox. /quit to exit.

Run from sbx/: python -m client.client
"""

## IMPORTS ##

import json

from dotenv import load_dotenv
from openai import OpenAI

from client.tools import TOOLS
from sdk.sandbox import Sandbox

## CONFIG ##

# reads OPENAI_API_KEY from .env
load_dotenv()
client = OpenAI()

MODEL = "gpt-4.1"
MAX_STEPS = 50      # most model calls per task, so a confused agent can't loop forever

SYSTEM_PROMPT = (
    "You are a coding agent working in a Linux sandbox. "
    "Use the tools to do the task. "
    "Start long-running servers with background=true."
)


## TOOL RUNNER ##

def run_tool(sandbox: Sandbox, name: str, args: dict) -> str:
    """
    Runs one tool call from the model in the sandbox
    prints what it's doing
    returns the result as text for the model
    """
    if name == "run_command":
        background = args.get("background", False)
        print(f"[run] {args['cmd']}" + ("   (background)" if background else ""))

        result = sandbox.exec(args["cmd"], background=background)

        # show the first few lines of output
        output = result.get("stdout", "") + result.get("stderr", "")
        for line in output.splitlines()[:5]:
            print(f"      {line}")

        return json.dumps(result)

    if name == "read_file":
        print(f"[read] {args['path']}")
        return sandbox.read_file(args["path"])

    # otherwise it's write_file
    print(f"[write] {args['path']}")
    sandbox.write_file(args["path"], args["content"])
    return "ok"


## CHAT LOOP ##

def main() -> None:
    """
    Starts a sandbox
    reads tasks from the terminal until /quit
    lets the model call tools until it answers in plain text
    destroys the sandbox on exit
    """
    sandbox = Sandbox.create()
    print(f"sandbox {sandbox.id} ready")

    # the whole conversation, sent to the model on every call
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    try:
        while (task := input("> ").strip()) != "/quit":
            messages.append({"role": "user", "content": task})

            for _ in range(MAX_STEPS):
                # ask the model what to do next
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=TOOLS,
                )
                message = response.choices[0].message
                messages.append(message)

                # no tool calls: the model is done, print its answer
                if not message.tool_calls:
                    print(message.content)
                    print("done")
                    break

                # run each tool call and send the result back to the model
                for call in message.tool_calls:
                    try:
                        args = json.loads(call.function.arguments)
                        output = run_tool(sandbox, call.function.name, args)
                    except Exception as e:
                        output = f"error: {e}"      # the model sees the error and can retry

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": output,
                    })

    finally:
        # always clean up, even on Ctrl+C or a crash
        sandbox.destroy()


if __name__ == "__main__":
    main()
