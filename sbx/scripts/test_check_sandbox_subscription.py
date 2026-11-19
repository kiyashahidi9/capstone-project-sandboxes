#!/usr/bin/env python3
"""Check a sandbox subscription by saving the JSON from each agent turn.

Run on the laptop, from sbx/, with the SSH tunnel to the host open.
The repo-root .env needs SANDBOX_ADMIN_KEY and SANDBOX_API=http://localhost:8000.

    uv run python scripts/test_check_sandbox_subscription.py

The client subscribes for the life of the sandbox, then sends three short tasks.
Each turn's events are written to stream-log/<sandbox id>/turn-N.json, in the
order the client received them, through turn_finished or turn_failed.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path

import httpx

from sdk.sandbox import Sandbox, SandboxError


TASKS: list[str] = [
    "Create /root/count.txt containing exactly the text 1.",
    "Read /root/count.txt and reply with only what is in the file.",
    "Change /root/count.txt so it contains exactly the text 2.",
]
TURN_END: set[str] = {"turn_finished", "turn_failed"}
LOG_DIR = Path("stream-log")


def follow(sandbox: Sandbox, events: queue.Queue[dict | None]) -> None:
    """One subscription for the life of the sandbox. A dropped connection resumes from the last seq."""
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


def take_turn(events: queue.Queue[dict | None]) -> list[dict]:
    """The events received for one turn, including the event that ends it."""
    batch: list[dict] = []
    while True:
        event = events.get()
        if event is None:
            return batch
        batch.append(event)
        print(json.dumps(event), flush=True)
        if event["type"] in TURN_END:
            return batch


def main() -> None:
    sandbox = Sandbox.create()
    print(f"sandbox {sandbox.id} subscribed", flush=True)

    incoming: queue.Queue[dict | None] = queue.Queue()
    reader = threading.Thread(target=follow, args=(sandbox, incoming), name="sandbox-events", daemon=True)
    reader.start()

    out = LOG_DIR / sandbox.id
    out.mkdir(parents=True)

    try:
        for number, task in enumerate(TASKS, start=1):
            print(f"\n--- turn {number}: {task}", flush=True)
            sandbox.send_message(task)
            batch = take_turn(incoming)
            path = out / f"turn-{number}.json"
            path.write_text(json.dumps(batch, indent=2) + "\n")
            kinds = [event["type"] for event in batch]
            print(f"saved {path} ({len(batch)} events): {', '.join(kinds)}", flush=True)
            if batch and batch[-1]["type"] == "turn_failed":
                sys.exit(1)
    except SandboxError as error:
        sys.exit(f"error: {error.detail}")
    finally:
        sandbox.destroy()
        reader.join(timeout=5)
        print(f"sandbox {sandbox.id} destroyed", flush=True)


if __name__ == "__main__":
    main()
