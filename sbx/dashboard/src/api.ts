// talks to the sbx API server; /api is forwarded to it by the dev server (see vite.config.ts)

export type SandboxState = "starting" | "running" | "pausing" | "paused" | "resuming" | "destroyed";
export type TurnState = "idle" | "working";

// same fields as the API's SandboxInfo (minus ip and updated_at, which the page doesn't show)
export type Sandbox = {
  id: string;
  state: SandboxState;
  turn_state: TurnState;    // working while the agent runs a turn
  created_at: number;       // unix seconds
  paused_at: number | null; // unix seconds; null unless paused
};

// one entry in a sandbox's event log; data depends on type (see docs/agent_contract.md)
export type SandboxEvent = {
  seq: number;
  type: string;
  data: Record<string, unknown>;
  at: number;               // unix seconds
};

export type TurnStatus = "running" | "unresponsive" | "finished" | "failed" | "paused" | "incomplete";

// same fields as the API's TurnSummary (docs/observability.md)
export type TurnSummary = {
  turn: number;
  status: TurnStatus;
  reason: string | null;
  cause: "agent" | "sandbox" | "host" | "paused" | "unknown" | null;
  exit_code: number | null;
  stderr_tail: string | null;
  started_at: number;
  ended_at: number | null;
  seconds_since_last_event: number;
  last_action: SandboxEvent | null;
  unfinished_step: SandboxEvent | null;
};


// ## REQUESTS ##

async function request<T>(path: string, method = "GET"): Promise<T> {
  // fetch only throws when the server can't be reached at all
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { method });
  } catch {
    throw new Error("Could not reach the API server. Is it running, and is the SSH tunnel open?");
  }

  // FastAPI puts its error message in "detail"
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail ?? "Could not reach the API server. Is it running, and is the SSH tunnel open?";
    // FastAPI says just "Not Found" when the route itself doesn't exist, i.e. the server runs older code
    const hint =
      response.status === 401 ? ". Check SANDBOX_ADMIN_KEY in the repo-root .env, then restart npm run dev"
      : detail === "Not Found" ? ". The API server doesn't have this endpoint: pull the latest code on the host and restart it"
      : "";
    throw new Error(`Request failed (${response.status}): ${detail}${hint}`);
  }
  return response.json();
}


// ## API ##

// destroyed sandboxes stay in the API's database (with their history); hidden unless asked for
export async function listSandboxes(includeDestroyed = false): Promise<Sandbox[]> {
  const body = await request<{ sandboxes: Sandbox[] }>("/sandboxes");
  return body.sandboxes.filter((s) => includeDestroyed || s.state !== "destroyed");
}

export async function listTurns(id: string): Promise<TurnSummary[]> {
  const body = await request<{ turns: TurnSummary[] }>(`/sandboxes/${id}/turns`);
  return body.turns;
}

// one turn's events with seq greater than after
export async function listEvents(id: string, turn: number, after = 0): Promise<SandboxEvent[]> {
  const body = await request<{ events: SandboxEvent[] }>(`/sandboxes/${id}/events?turn=${turn}&after=${after}`);
  return body.events;
}

// saves the VM to disk and frees its memory; cuts off the agent's turn if one is running
export function pauseSandbox(id: string): Promise<Sandbox> {
  return request<Sandbox>(`/sandboxes/${id}/pause`, "POST");
}

export function resumeSandbox(id: string): Promise<Sandbox> {
  return request<Sandbox>(`/sandboxes/${id}/resume`, "POST");
}

export async function deleteSandbox(id: string): Promise<void> {
  await request(`/sandboxes/${id}`, "DELETE");
}
