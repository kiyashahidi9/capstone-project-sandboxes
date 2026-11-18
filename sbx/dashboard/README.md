# Sandbox Dashboard

Single-page React + TypeScript dashboard to view, pause, resume and delete sandboxes from the sbx API server.

```bash
# needs the API reachable at SANDBOX_API (default http://localhost:8000, the SSH tunnel)
# and SANDBOX_ADMIN_KEY set in the repo-root .env
npm install
npm run dev      # http://localhost:5173
```

| File             | What it does                                                         |
| ---------------- | -------------------------------------------------------------------- |
| `src/api.ts`     | Calls `GET /sandboxes`, `POST .../pause`, `POST .../resume`, `DELETE /sandboxes/{id}` |
| `src/App.tsx`    | Table, action buttons, delete confirmation modal, loading and error states |
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
- Destroyed sandboxes stay in the API's database; the dashboard hides them.
