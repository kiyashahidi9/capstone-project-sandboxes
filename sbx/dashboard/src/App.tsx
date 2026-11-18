import { useEffect, useState } from "react";
import { deleteSandbox, listSandboxes, pauseSandbox, resumeSandbox, type Sandbox } from "./api";

const POLL_INTERVAL_MS = 2000;

// the sandbox with an action in progress, and what to show while it runs
type Busy = { id: string; label: string };

function formatTime(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString();
}

export default function App() {
  const [sandboxes, setSandboxes] = useState<Sandbox[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<Busy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  // load the list on page open, then reload it every few seconds (this also catches auto-pauses)
  useEffect(() => {
    function load() {
      listSandboxes()
        .then(setSandboxes)
        .catch((e) => setError(e.message))
        .finally(() => setLoading(false));
    }

    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);


  // ## ACTIONS ##

  async function runAction(id: string, label: string, action: () => Promise<void>) {
    setBusy({ id, label });
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  // replace one sandbox in the list with what the API sent back
  function updateRow(updated: Sandbox) {
    setSandboxes((list) => list.map((s) => (s.id === updated.id ? updated : s)));
  }

  function pause(id: string) {
    runAction(id, "Pausing…", async () => updateRow(await pauseSandbox(id)));
  }

  function resume(id: string) {
    runAction(id, "Resuming…", async () => updateRow(await resumeSandbox(id)));
  }

  function confirmDelete() {
    const id = confirmDeleteId!;
    setConfirmDeleteId(null);
    runAction(id, "Deleting…", async () => {
      await deleteSandbox(id);
      setSandboxes((list) => list.filter((s) => s.id !== id));
    });
  }


  // ## PAGE ##

  return (
    <main>
      <h1>Sandboxes</h1>

      {error && (
        <div className="error">
          {error}
          <button onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}

      {loading ? (
        <p>Loading sandboxes…</p>
      ) : sandboxes.length === 0 ? (
        <p>No sandboxes.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>State</th>
              <th>Agent</th>
              <th>Created</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {sandboxes.map((s) => (
              <tr key={s.id}>
                <td className="id">{s.id}</td>
                <td>
                  <span className={`badge ${s.state}`}>{s.state}</span>
                  {s.paused_at && <div className="muted">since {formatTime(s.paused_at)}</div>}
                </td>
                <td>
                  <span className={`badge ${s.turn_state}`}>{s.turn_state}</span>
                </td>
                <td>{formatTime(s.created_at)}</td>
                <td className="actions">
                  {busy?.id === s.id ? (
                    <span className="muted">{busy.label}</span>
                  ) : (
                    <>
                      {s.state === "running" && (
                        <button onClick={() => pause(s.id)} title="Saves the sandbox to disk; stops the agent's turn if one is running">
                          Pause
                        </button>
                      )}
                      {s.state === "paused" && <button onClick={() => resume(s.id)}>Resume</button>}
                      <button className="danger" onClick={() => setConfirmDeleteId(s.id)}>
                        Delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {confirmDeleteId && (
        <div className="overlay" onClick={() => setConfirmDeleteId(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <p>Are you sure you want to permanently delete this sandbox? This action cannot be undone.</p>
            <p className="id">{confirmDeleteId}</p>
            <div className="modal-buttons">
              <button onClick={() => setConfirmDeleteId(null)}>Cancel</button>
              <button className="danger" onClick={confirmDelete}>Confirm Delete</button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
