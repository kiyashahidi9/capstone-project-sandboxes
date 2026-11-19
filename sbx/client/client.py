"""
The thin chat app (Build Plan 2g)
no agent and no AI key here: the agent runs on the host, and this app only sends messages and shows events
needs SANDBOX_ADMIN_KEY in .env (the same value as on the host)
run from sbx/:
    uv run python -m client.client
"""

import os
import queue
import sys
import threading

import httpx

from sdk.sandbox import Sandbox, SandboxError   # also loads .env


## CONFIG ##

STDERR_LINES_SHOWN = 5    # how much of a failed agent's stderr to show


## SHOWING EVENTS ##

def show_event(event: dict) -> bool:
    """
    Prints one event for the user
    returns True when the event ends the turn
    """
    kind = event["type"]
    data = event["data"]

    if kind == "agent_output":
        print(data["text"])

    elif kind == "question":
        print(f"[waiting on you] {data['text']}")

    elif kind == "turn_finished":
        print("done")
        return True

    elif kind == "turn_failed":
        print(f"error: {data.get('reason', 'unknown')}")

        stderr_lines = data.get("stderr_tail", "").splitlines()
        for line in stderr_lines[-STDERR_LINES_SHOWN:]:
            print(f"      {line}")
        return True

    elif kind in ("paused", "resumed"):
        print(f"[{kind}]")

    return False


def follow(sandbox: Sandbox, events: queue.Queue[dict | None]) -> None:
    """
    Holds one subscription for the life of the sandbox
    a dropped connection subscribes again from the last seq; the server ending the stream stops it
    """
    last_seq = 0
    while True:
        try:
            for event in sandbox.subscribe(after=last_seq):
                last_seq = event["seq"]
                events.put(event)
        except httpx.TransportError:
            continue
        except SandboxError:
            events.put(None)
            return
        else:
            events.put(None)
            return


def wait_for_turn(events: queue.Queue[dict | None]) -> None:
    """Shows events from the open subscription until the turn finishes or fails."""
    while True:
        event = events.get()
        if event is None or show_event(event):
            return


## CHAT LOOP ##

def main() -> None:
    """
    Starts a sandbox
    sends each message to the agent on the host and shows its progress, until /quit
    destroys the sandbox on exit
    """
    admin_key = os.environ.get("SANDBOX_ADMIN_KEY")
    if not admin_key:
        sys.exit("SANDBOX_ADMIN_KEY is not set; add it to .env (the same value as on the host)")

    sandbox = Sandbox.create(token=admin_key)
    print(f"sandbox {sandbox.id} ready")

    events: queue.Queue[dict | None] = queue.Queue()
    reader = threading.Thread(target=follow, args=(sandbox, events), name="sandbox-events", daemon=True)
    reader.start()

    try:
        while (text := input("> ").strip()) != "/quit":
            if not text:
                continue

            try:
                sandbox.send_message(text)
            except SandboxError as error:
                print(f"error: {error.detail}")
                continue

            wait_for_turn(events)

    except (KeyboardInterrupt, EOFError):
        print()

    finally:
        sandbox.destroy()
        reader.join(timeout=5)
        print(f"sandbox {sandbox.id} destroyed")


if __name__ == "__main__":
    main()
