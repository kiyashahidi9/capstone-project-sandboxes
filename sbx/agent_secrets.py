"""
The provider keys a client keeps in their agent folder's secrets.env
setup_agents.sh makes the file root only, so the agent itself can't read it; only the API's key proxy does
read fresh on every call, so a key replaced with install.sh --update-secrets works on the next model call
"""

from __future__ import annotations

import os

from dotenv import dotenv_values


SECRETS_FILE_NAME = "secrets.env"
EXAMPLE_AGENT_DIR = "/opt/agents"        # where setup_agents.sh installs the example agent
SECRETS_FILE_ENV = "AGENT_SECRETS_FILE"  # in .env, to point at a different file (development and tests)


def secrets_path() -> str:
    """Gives where the current agent's secrets.env is: its own folder, or the example agent's."""
    override = os.environ.get(SECRETS_FILE_ENV, "").strip()
    if override:
        return override
    folder = os.environ.get("AGENT_DIR", "").strip() or EXAMPLE_AGENT_DIR
    return os.path.join(folder, SECRETS_FILE_NAME)


def read_secret(name: str) -> str | None:
    """Looks up one key in secrets.env, or gives nothing if the file or the key isn't there."""
    value = dotenv_values(secrets_path()).get(name)
    return value or None
