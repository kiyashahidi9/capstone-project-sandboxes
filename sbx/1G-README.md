
# Milestone 1G — API Server

## 1. Overview

Build a REST API using **FastAPI** that exposes the Node Agent's sandbox management functionality to external clients.

The API server acts as the entry point for clients and AI agents to interact with Firecracker MicroVM sandboxes.

### Architecture

```text
Client / AI Agent
       |
       | HTTP (REST API)
       v
 FastAPI Server
   (api/main.py)
       |
       | Node Agent functions
       v
    Node Agent
       |
       | Firecracker API
       v
 Firecracker MicroVM
       |
       v
    Guest Agent
```

**Responsibilities:**
- Expose endpoints for sandbox lifecycle management.
- Execute commands inside running sandboxes.
- Read and write files inside sandboxes.
- Persist sandbox metadata using SQLite.
- Recover sandbox state after an API server restart.
- Handle invalid sandbox IDs and states.

**Execution environment:** The API server runs as root on the Linux host to access the Node Agent's privileged operations.

---

## 2. REST API Endpoints

**File:** `api/main.py`

Implement the following endpoints:

| Method | Endpoint | Node Agent Function | Response |
|--------|----------|---------------------|----------|
| POST | `/sandboxes` | `create_sandbox()` | Sandbox ID and state |
| GET | `/sandboxes` | — | List of all sandboxes |
| GET | `/sandboxes/{id}` | — | Sandbox metadata |
| POST | `/sandboxes/{id}/exec` | `exec()` | Command execution result |
| GET | `/sandboxes/{id}/files?path=` | `read_file()` | File contents |
| PUT | `/sandboxes/{id}/files?path=` | `write_file()` | Success confirmation |
| DELETE | `/sandboxes/{id}` | `destroy_sandbox()` | Success confirmation |

### Example Responses

**Create a sandbox**

```http
POST /sandboxes
```

```json
{
  "id": "sbx1",
  "state": "running"
}
```

**List all sandboxes**

```http
GET /sandboxes
```

```json
[
  {
    "id": "sbx1",
    "state": "running"
  },
  {
    "id": "sbx2",
    "state": "destroyed"
  }
]
```

**Get sandbox details**

```http
GET /sandboxes/sbx1
```

```json
{
  "id": "sbx1",
  "state": "running",
  "created_at": 1794470400
}
```

**Execute a command**

```http
POST /sandboxes/sbx1/exec
Content-Type: application/json
```

Request body:

```json
{
  "cmd": "uname -a"
}
```

Return the Guest Agent's command execution result.

**Read a file**

```http
GET /sandboxes/sbx1/files?path=/tmp/hello.txt
```

Return the requested file contents.

**Write a file**

```http
PUT /sandboxes/sbx1/files?path=/tmp/hello.txt
```

Pass the request body to `write_file()`.

Response:

```json
{
  "ok": true
}
```

**Destroy a sandbox**

```http
DELETE /sandboxes/sbx1
```

```json
{
  "ok": true
}
```

---

## 3. Error Handling

Implement the following HTTP status codes:

| Status Code | Condition |
|-------------|-----------|
| 404 Not Found | Sandbox ID does not exist |
| 409 Conflict | Operation requires a running sandbox, but it is not running |

### Example: Unknown Sandbox

```http
GET /sandboxes/unknown
```

```json
{
  "detail": "Sandbox not found"
}
```

**HTTP status:** `404`

### Example: Sandbox Not Running

```http
POST /sandboxes/sbx1/exec
```

```json
{
  "detail": "Sandbox is not running"
}
```

**HTTP status:** `409`

---

## 4. SQLite State Persistence

**Database:** `state.db`

Store sandbox metadata in SQLite so that the API server can recover existing sandboxes after restarting.

### Database Schema

```sql
CREATE TABLE sandboxes (
    id          TEXT PRIMARY KEY,
    n           INTEGER NOT NULL,
    ip          TEXT NOT NULL,
    pid         INTEGER,
    state       TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
```

### Column Definitions

| Column | Description | Example |
|--------|-------------|---------|
| `id` | Unique sandbox identifier | `sbx1` |
| `n` | Sandbox sequence number | `1` |
| `ip` | Sandbox IP address | `10.201.0.1` |
| `pid` | Firecracker process ID | `12345` |
| `state` | Current sandbox lifecycle state | `running` |
| `created_at` | Creation timestamp (Unix seconds) | `1794470400` |
| `updated_at` | Last update timestamp (Unix seconds) | `1794470405` |

### Supported States

For Milestone 1G:

```text
starting → running → destroyed
```

Additional states planned for Milestone 3:

```text
pausing
paused
resuming
```

---

## 5. Sandbox Lifecycle Management

### 5.1 Creating a Sandbox

When receiving:

```http
POST /sandboxes
```

Execute the following sequence:

1. Generate a unique sandbox ID and allocate its metadata.
2. Insert a row into SQLite with `state = 'starting'`.
3. Call the Node Agent's `create_sandbox()`.
4. Wait for successful sandbox creation.
5. Update SQLite with `state = 'running'`.
6. Return the sandbox ID and state to the client.

**Important:** Persist the `starting` state before initiating sandbox creation.

### 5.2 Destroying a Sandbox

When receiving:

```http
DELETE /sandboxes/{id}
```

1. Look up the sandbox in SQLite.
2. Call `destroy_sandbox()` to clean up its resources.
3. Update the sandbox state to `destroyed`.
4. Return `{"ok": true}`.

`starting` is a real state. Delete tears down a sandbox that is still `starting`, the same as one that is `running`. A sandbox that is already `destroyed` is left alone, and that call still returns `{"ok": true}`.

### 5.3 Recovering Sandboxes on Server Startup

When FastAPI starts:

1. Query SQLite for all sandboxes whose state is not `destroyed`.
2. Check whether each sandbox's Firecracker PID is still alive.
3. If the process is alive, retain its state and make it available for subsequent operations.
4. If the process is dead:
   - Clean up remaining sandbox resources.
   - Update its database state to `destroyed`.

**Goal:** Restarting the API server must not destroy healthy Firecracker MicroVMs.

**Implementation note:** Checking whether a PID exists is a minimum recovery check. A production implementation should also verify process identity and sandbox connectivity.

---

## 6. Start the API Server

Run the API on the Linux host:

```bash
sudo .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
```

The server listens on port `8000`.

### Verify API Availability

```bash
curl -i http://localhost:8000/sandboxes
```

Expected result:

```text
HTTP/1.1 200 OK
```

The response should contain the sandbox list, initially `[]` if no sandboxes exist.

**Security note:** The API controls privileged host operations. Binding to `0.0.0.0` exposes it on all host interfaces. Restrict network access and add authentication before allowing untrusted clients to connect.

---

## 7. End-to-End Testing

Use `curl` to verify all API endpoints.

### Test 1 — Create Sandbox

```bash
curl -X POST http://localhost:8000/sandboxes
```

Expected:

```json
{
  "id": "sbx1",
  "state": "running"
}
```

✅ PASS — Sandbox created successfully.

❌ FAIL — Sandbox creation fails or returns an invalid response.

### Test 2 — List Sandboxes

```bash
curl http://localhost:8000/sandboxes
```

✅ PASS — Newly created sandbox appears in the list.

❌ FAIL — Sandbox is missing or has an incorrect state.

### Test 3 — Get Sandbox Details

```bash
curl http://localhost:8000/sandboxes/sbx1
```

✅ PASS — Returns sandbox ID, state, and creation timestamp.

❌ FAIL — Sandbox metadata is missing or incorrect.

### Test 4 — Execute Command

```bash
curl -X POST http://localhost:8000/sandboxes/sbx1/exec \
  -H "Content-Type: application/json" \
  -d '{"cmd":"uname -a"}'
```

✅ PASS — Command executes inside the MicroVM and returns the Guest Agent's result.

❌ FAIL — Command fails, times out, or executes on the host instead of inside the MicroVM.

### Test 5 — Write File

```bash
curl -X PUT \
  "http://localhost:8000/sandboxes/sbx1/files?path=/tmp/hello.txt" \
  -H "Content-Type: text/plain" \
  --data-binary "Hello from FastAPI"
```

Expected:

```json
{
  "ok": true
}
```

✅ PASS — File successfully written inside the MicroVM.

❌ FAIL — File is missing or write operation fails.

### Test 6 — Read File

```bash
curl \
  "http://localhost:8000/sandboxes/sbx1/files?path=/tmp/hello.txt"
```

Expected:

```text
Hello from FastAPI
```

✅ PASS — File content matches the written data.

❌ FAIL — File cannot be read or content differs.

### Test 7 — Destroy Sandbox

```bash
curl -X DELETE http://localhost:8000/sandboxes/sbx1
```

Expected:

```json
{
  "ok": true
}
```

✅ PASS — Sandbox resources are removed and SQLite state is updated.

❌ FAIL — Firecracker continues running or resources are not cleaned up.

### Test 8 — Unknown Sandbox

```bash
curl -i http://localhost:8000/sandboxes/unknown
```

✅ PASS — Returns HTTP `404`.

❌ FAIL — Returns a successful status or an unexpected server error.

---

## 8. Server Restart and State Recovery

Verify that SQLite persistence allows the API server to recover running sandboxes.

### Procedure

1. Create a sandbox using `POST /sandboxes`.
2. Confirm its state is `running`.
3. Stop the Uvicorn server using `Ctrl+C`.
4. Verify that the Firecracker process remains alive.
5. Restart the API server:

```bash
sudo .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
```

6. List the sandboxes:

```bash
curl http://localhost:8000/sandboxes
```

7. Execute a command inside the previously created sandbox:

```bash
curl -X POST http://localhost:8000/sandboxes/sbx1/exec \
  -H "Content-Type: application/json" \
  -d '{"cmd":"echo recovered"}'
```

### Expected Results

✅ PASS — Existing sandbox still appears as `running`.

✅ PASS — Sandbox accepts new commands after the API restart.

✅ PASS — Previously created MicroVM was not recreated or destroyed.

❌ FAIL — Running sandbox disappears from SQLite.

❌ FAIL — API restart terminates the Firecracker process.

❌ FAIL — Sandbox appears as running but cannot accept commands.

---

## 9. Definition of Done

Milestone 1G is complete when:

### API Functionality

- [ ] `POST /sandboxes` creates a sandbox.
- [ ] `GET /sandboxes` lists all sandboxes.
- [ ] `GET /sandboxes/{id}` returns sandbox details.
- [ ] `POST /sandboxes/{id}/exec` executes commands.
- [ ] `GET /sandboxes/{id}/files` reads files.
- [ ] `PUT /sandboxes/{id}/files` writes files.
- [ ] `DELETE /sandboxes/{id}` destroys sandboxes.

### Error Handling

- [ ] Unknown sandbox IDs return HTTP `404`.
- [ ] Operations requiring a running sandbox return HTTP `409` when it is not running.

### State Persistence

- [ ] Sandbox metadata is stored in `state.db`.
- [ ] Sandboxes are recorded as `starting` before creation.
- [ ] Successfully created sandboxes transition to `running`.
- [ ] Destroyed sandboxes transition to `destroyed`.
- [ ] Server startup checks non-destroyed sandbox PIDs.
- [ ] Dead sandboxes are cleaned up and marked `destroyed`.

### Integration Testing

- [ ] Every endpoint works using `curl`.
- [ ] Running sandboxes survive API server restarts.
- [ ] Recovered sandboxes remain accessible and usable.
- [ ] Sandbox commands execute inside the Guest VM, not on the host.

---

## Milestone Completion Criteria

**Milestone 1G is complete when the FastAPI server can create, manage, and interact with Firecracker sandboxes through HTTP, while persisting their state across API server restarts.**
