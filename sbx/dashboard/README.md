# Sandbox Dashboard

Single-page React + TypeScript dashboard to view, pause, resume and delete sandboxes from the sbx API server, and to see each sandbox's turns and timeline ([observability](../docs/observability.md)).

```bash
# needs the API reachable at SANDBOX_API (default http://localhost:8000, the SSH tunnel)
# and SANDBOX_ADMIN_KEY set in the repo-root .env
npm install
npm run dev      # http://localhost:5173
```

| File             | What it does                                                         |
| ---------------- | -------------------------------------------------------------------- |
| `src/api.ts`     | Calls the sandbox endpoints (list, pause, resume, delete) and `GET .../turns`, `GET .../events` |
| `src/App.tsx`    | Table, action buttons, delete confirmation modal, loading and error states |
| `src/SandboxDetail.tsx` | Click a sandbox ID: its turns, the failure summary, and the turn's timeline |
| `src/Replay.tsx` | The turn's **Replay** tab: what the model was sent at each step, its reply, and its tools' results |
| `src/index.css`  | Styles                                                               |
| `vite.config.ts` | Forwards `/api/*` to `SANDBOX_API` and adds the admin key            |

| State | Buttons |
| ----- | ------- |
| `running` | Pause, Delete |
| `paused` | Resume, Delete |
| `starting`, `pausing`, `resuming` | Delete |

- The admin key is added by the dev server, so it never reaches the browser. Restart `npm run dev` after changing `.env`.
- The list reloads every 2 seconds (`POLL_INTERVAL_MS` in `App.tsx`), so auto-pauses and other state changes show up on their own.
- **Agent** column: `working` while the agent runs a turn, otherwise `idle`. Pausing cuts off a running turn.
- Destroyed sandboxes are hidden unless **Show destroyed** is ticked; their history stays readable.
