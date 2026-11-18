"""
A fake agent that tries to break out of the 2f lockdown; every attempt must fail
installed to /opt/agents by scripts/setup_agents.sh and run by scripts/check_guardrails.sh
stdin: "<another sandbox's id> <path to the host's .env>"
prints PASS or FAIL per check; exits 0 only if all pass
standard library only, so it runs on any Python
"""

import os
import pwd
import sys
import urllib.error
import urllib.request


## CONFIG ##

API = os.environ["SANDBOX_API"]
OWN_ID = os.environ["SANDBOX_ID"]
TOKEN = os.environ["SANDBOX_TOKEN"]
METADATA_URL = "http://169.254.169.254/computeMetadata/v1/"


## HELPERS ##

def call(method: str, path: str, token: str | None = TOKEN, body: bytes | None = None) -> int:
    """Calls the API and gives the HTTP status."""
    request = urllib.request.Request(f"{API}{path}", data=body, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def reaches_metadata_server() -> bool:
    try:
        request = urllib.request.Request(METADATA_URL, headers={"Metadata-Flavor": "Google"})
        urllib.request.urlopen(request, timeout=3)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def can_read(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as file:
            file.read()
        return True
    except OSError:
        return False


## CHECKS ##

def main() -> int:
    other_id, env_path = sys.stdin.read().split()
    run_true = b'{"cmd": "true"}'

    checks = [
        ("its own token works on its own sandbox (exec -> 200)", call("POST", f"/sandboxes/{OWN_ID}/exec", body=run_true) == 200),
        ("exec on another sandbox with its token -> 403", call("POST", f"/sandboxes/{other_id}/exec", body=run_true) == 403),
        ("DELETE its own sandbox -> 403", call("DELETE", f"/sandboxes/{OWN_ID}") == 403),
        ("call the API with no key -> 401", call("GET", "/sandboxes", token=None) == 401),
        ("the metadata server is blocked", not reaches_metadata_server()),
        ("cat the .env file -> permission denied", not can_read(env_path)),
        ("whoami prints sbx-agent", pwd.getpwuid(os.getuid()).pw_name == "sbx-agent"),
    ]

    for description, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {description}", flush=True)

    return 0 if all(passed for _, passed in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
