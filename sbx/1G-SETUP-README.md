# Milestone 1G — Setup

The HTTP API lives in `sbx/api/`. These tests check that API and its SQLite database. They use a fake node agent, so they do not boot a virtual machine and they do not need root.

## Run the tests

From `sbx/`, with [uv](https://docs.astral.sh/uv/) installed and Python 3.11 or newer:

```bash
cd sbx
uv sync
uv run pytest tests/test_api.py tests/test_db.py -v
```

`uv sync` installs the project, including pytest.

Run one file:

```bash
uv run pytest tests/test_api.py -v
uv run pytest tests/test_db.py -v
```

| File | What it covers |
|------|----------------|
| `tests/test_api.py` | Create, list, get, delete, exec, and file read/write. The boot, command, and file calls are faked. |
| `tests/test_db.py` | The SQLite tables: insert, state changes, lookup, and list order. Uses a temporary database file. |

`uv run pytest` also runs `tests/test_node_agent_helpers.py`. That file checks slot allocation and does not need a virtual machine. `tests/test_node_agent.py` boots real Firecracker virtual machines and skips itself on a machine without root and `/dev/kvm`.
