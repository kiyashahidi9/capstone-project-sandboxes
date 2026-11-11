"""Terminal chat: type a task, an OpenAI agent does it in a sandbox. /quit to exit.

Run from the repo root: python -m client.client
"""
import json

from dotenv import load_dotenv
from openai import OpenAI

from client.tools import TOOLS
from sdk.sandbox import Sandbox

load_dotenv()   # reads OPENAI_API_KEY from .env
client = OpenAI()
MODEL = "gpt-4.1"


def run_tool(sbx, name, args):
    if name == "run_command":
        bg = args.get("background", False)
        print(f"[run] {args['cmd']}" + ("   (background)" if bg else ""))
        result = sbx.exec(args["cmd"], background=bg)
        for line in (result.get("stdout", "") + result.get("stderr", "")).splitlines()[:5]:
            print(f"      {line}")
        return json.dumps(result)
    if name == "read_file":
        print(f"[read] {args['path']}")
        return sbx.read_file(args["path"])
    print(f"[write] {args['path']}")
    sbx.write_file(args["path"], args["content"])
    return "ok"


def main():
    sbx = Sandbox.create()
    print(f"sandbox {sbx.id} ready")
    messages = [{"role": "system", "content": "You are a coding agent working in a Linux sandbox. "
                 "Use the tools to do the task. Start long-running servers with background=true."}]
    try:
        while (task := input("> ").strip()) != "/quit":
            messages.append({"role": "user", "content": task})
            for _ in range(50):
                msg = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS).choices[0].message
                messages.append(msg)
                if not msg.tool_calls:
                    print(msg.content)
                    print("done")
                    break
                for call in msg.tool_calls:
                    try:
                        output = run_tool(sbx, call.function.name, json.loads(call.function.arguments))
                    except Exception as e:
                        output = f"error: {e}"
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
    finally:
        sbx.destroy()


if __name__ == "__main__":
    main()
