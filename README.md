# Capstone: Sandboxes

An example coding agent that uses OpenAI to complete tasks inside a sandbox.
Locally, a mock API and a Docker container stand in for the real Firecracker VMs.

```
example_agent → sdk → mock API (:8000) → guest agent in Docker (:49983)
```

## Setup and test

Run everything from the repo root.

```bash
# install dependencies
pip install fastapi uvicorn httpx openai python-dotenv

# set up env vars, then fill in OPENAI_API_KEY in .env
cp .env.example .env

# terminal 1: start the guest agent (runs commands / files inside the "VM")
# works as-is in Git Bash (Windows), Linux and macOS
MSYS_NO_PATHCONV=1 docker run --rm -p 49983:49983 -v "$(pwd -W 2>/dev/null || pwd)/guest_agent:/app" -w /app python:3.12 python guest_agent.py

# terminal 2: start the mock API (fake sandboxes, forwards to the guest agent)
uvicorn mock.mock_api:app --port 8000

# terminal 3: check the guest agent is up -> {"ok": true}
curl http://localhost:49983/health

# smoke-test the SDK end to end (create, exec, write/read a file, destroy)
python -m sdk.sandbox
```

Expected smoke test output (sandbox number and pid will vary):

```
created Sandbox(id='sbx1', api='http://localhost:8000')
exec       {'exit_code': 0, 'stdout': 'hi\n', 'stderr': '', 'timed_out': False}
read_file  'hello'
background {'pid': 1234}
destroyed sbx1
```

Then run the agent:

```bash
python example_agent/agent_openai.py "create hello.py that prints hello, and run it"
```
