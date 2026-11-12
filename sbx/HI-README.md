# Capstone: Sandboxes

An example coding agent that uses OpenAI to complete tasks inside a sandbox.
Locally, a mock API and a Docker container stand in for the real Firecracker VMs.

```
example_agent → sdk → mock API (:8000) → guest agent in Docker (:49983)
```

## Layout

All program code lives in `sbx/`. The `.env` file stays at the repo root.

```
sbx/
  client/          interactive chat agent (one sandbox across tasks)
  example_agent/   one-shot OpenAI coding agent
  guest_agent/     HTTP server that runs inside the "VM"
  mock/            mock sandbox API
  sdk/             Python SDK + smoke test
```

## Setup

```bash
# install uv if you don't have it: https://docs.astral.sh/uv/getting-started/installation/

# set up env vars (at the repo root), then fill in OPENAI_API_KEY in .env
cp .env.example .env

# run every command below from sbx/
cd sbx

# create .venv and install the locked dependencies
uv sync

# terminal 1: start the guest agent (runs commands / files inside the "VM")
# works as-is in Git Bash (Windows), Linux and macOS
MSYS_NO_PATHCONV=1 docker run --rm -p 49983:49983 -v "$(pwd -W 2>/dev/null || pwd)/guest_agent:/app" -w /app nikolaik/python-nodejs:python3.12-nodejs20 python guest_agent.py

# terminal 2: start the mock API (fake sandboxes, forwards to the guest agent)
uv run uvicorn mock.mock_api:app --port 8000

# terminal 3: check the guest agent is up -> {"ok": true}
curl http://localhost:49983/health

# check the SDK end to end, no OpenAI key needed (prints each call and its output)
uv run python -m sdk.smoke_test
```

Then run the agent:

```bash
uv run python -m example_agent.agent_openai "create hello.py that prints hello, and run it"
```

Or start the client agent, an interactive chat that keeps one sandbox open across tasks:

```bash
# must be run as a module from sbx/
uv run python -m client.client
```

Type a task at the `>` prompt. The agent works on it in the sandbox and prints its answer. Type `/quit` to exit; the sandbox is destroyed automatically. An example session (the agent's exact steps and wording will vary):

```
sandbox sbx1 ready
> create hello.py that prints hello, and run it
[write] hello.py
[run] python hello.py
      hello
I created hello.py and ran it; it printed "hello".
done
> /quit
```
```
sandbox sb1 ready
> clone https://github.com/kiyashahidi9/example-express-app and start the dev server
> curl the health check
> add a /time route that returns the current time, then curl it
> make the time return in EST
> /quit
```

## Telling the agent where it works

The sandbox doesn't tell the agent which directory it's in. The client app has to say so, either in the system prompt or in the tool descriptions. If it doesn't, the agent loses track of its own files.

Commands run in `/root` by default, so relative paths work in `run_command`. The guest agent's file endpoints, though, only accept absolute paths. An agent that doesn't know about `/root` guesses one. In testing, the agent cloned `example-express-app` into `/root` and then kept trying to read `/sandbox/example-express-app/index.js`, which got a 404. Its relative-path attempts got a 400 because they weren't absolute. It gave up without ever finding the file.

The client (`client/client.py`) handles this with a single `WORKDIR` setting, used in three places:

- `build_tools(WORKDIR)` (`client/tools.py`) puts the working directory in each tool's description.
- `run_command` runs with `cwd=WORKDIR`, so commands run where the descriptions say they do.
- `absolute_path()` joins relative `read_file`/`write_file` paths onto `WORKDIR`.

Any other client should do the same. Tell the model the working directory, through the system prompt, the tool descriptions (`client/tools.py`), or both. Make sure the directory it's told about is the one commands actually run in. If you change `WORKDIR`, that folder must already exist in the VM, or `exec` returns a 400.

### Tradeoffs (for the case study)

| Approach | Pro | Con |
|---|---|---|
| System prompt | One sentence; the model sees it from the start | Easy to drift from the real `cwd`; every client must remember to add it |
| Tool description | The rule sits on the tool, right where the model picks a path | Repeated in every tool schema; still only guidance |
| Client resolves relative paths | No model cooperation needed | Can't fix a made-up absolute path like `/sandbox/...` |
| Tool results report `cwd` / errors name it | Agent recovers from its own mistakes | Only helps after a failed call |
| Absolute-paths-only API (current) | Unambiguous on the server side | Pushes the problem onto every client |

Our choice: tool descriptions plus client-side resolution, both driven by one `WORKDIR`. The system prompt stays generic. A system-prompt line and better error messages are cheap additions if agents still guess wrong.
