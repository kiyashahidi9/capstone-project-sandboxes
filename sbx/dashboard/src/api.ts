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
    const hint = response.status === 401 ? ". Check SANDBOX_ADMIN_KEY in the repo-root .env, then restart npm run dev" : "";
    throw new Error(`Request failed (${response.status}): ${detail}${hint}`);
  }
  return response.json();
}


// ## API ##

// destroyed sandboxes stay in the API's database, so hide them here
export async function listSandboxes(): Promise<Sandbox[]> {
  const body = await request<{ sandboxes: Sandbox[] }>("/sandboxes");
  return body.sandboxes.filter((s) => s.state !== "destroyed");
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
