"""
The thin chat app (Build Plan 2g)
no agent and no AI key here: the agent runs on the host, and this app only sends messages and shows events
needs SANDBOX_ADMIN_KEY in .env (the same value as on the host)
run from sbx/:
    uv run python -m client.client
"""

## IMPORTS ##

import os
import sys
import time

from sdk.sandbox import Sandbox, SandboxError   # also loads .env


## CONFIG ##

POLL_SECONDS = 1          # how often to ask for new events while a turn runs
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

        # the end of the agent's stderr, if it ran
        stderr_lines = data.get("stderr_tail", "").splitlines()
        for line in stderr_lines[-STDERR_LINES_SHOWN:]:
            print(f"      {line}")
        return True

    elif kind in ("paused", "resumed"):
        print(f"[{kind}]")

    # user_message and turn_started: nothing to show
    return False


def wait_for_turn(sandbox: Sandbox, last_seq: int) -> int:
    """
    Shows the turn's events as they arrive, until it finishes or fails
    returns the last seq seen, for the next turn
    """
    while True:
        for event in sandbox.events(after=last_seq):
            last_seq = event["seq"]
            if show_event(event):
                return last_seq

        time.sleep(POLL_SECONDS)


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

    last_seq = 0   # the newest event already shown

    try:
        while (text := input("> ").strip()) != "/quit":
            if not text:
                continue

            try:
                sandbox.send_message(text)
            except SandboxError as error:
                print(f"error: {error.detail}")
                continue

            last_seq = wait_for_turn(sandbox, last_seq)

    except (KeyboardInterrupt, EOFError):
        print()   # Ctrl+C or Ctrl+D: end the line and clean up

    finally:
        # always clean up, even on Ctrl+C or a crash; this also stops a running turn
        sandbox.destroy()
        print(f"sandbox {sandbox.id} destroyed")


if __name__ == "__main__":
    main()
