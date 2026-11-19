// one sandbox's turns and the timeline of the chosen turn (docs/observability.md)

import { useEffect, useState } from "react";
import { listEvents, listTurns, type SandboxEvent, type TurnSummary } from "./api";
import Replay from "./Replay";

const POLL_INTERVAL_MS = 2000;
const SUMMARY_CHARS = 140;


// ## FORMATTING ##

function formatTime(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString();
}

function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

// any value as one line of text
function text(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function firstLine(value: unknown): string {
  return text(value).split("\n").find((line) => line.trim() !== "") ?? "";
}

// a tool call in a few words: the command for run_command, the path for files
function describeTool(name: unknown, args: unknown): string {
  const fields = (args ?? {}) as Record<string, unknown>;
  return `${text(name)} ${text(fields.cmd ?? fields.path ?? args)}`;
}

// one line saying what an event was
function summarize(event: SandboxEvent): string {
  const data = event.data;
  let line: string;

  switch (event.type) {
    case "llm_request":
      line = `${text(data.model)} · ${Array.isArray(data.messages) ? data.messages.length : 0} messages`;
      break;
    case "llm_response": {
      const calls = Array.isArray(data.tool_calls) ? data.tool_calls : [];
      const names = calls.map((call) => describeTool(call.name, call.arguments)).join(", ");
      line = [firstLine(data.text), names && `calls ${names}`].filter(Boolean).join(" · ");
      break;
    }
    case "tool_started":
      line = describeTool(data.name, data.arguments);
      break;
    case "tool_completed": {
      const result = data.result as Record<string, unknown> | string | undefined;
      if (result && typeof result === "object" && "exit_code" in result) {
        line = `exit ${text(result.exit_code)} · ${firstLine(result.stdout) || firstLine(result.stderr)}`;
      } else if (result && typeof result === "object" && "path" in result) {
        line = `wrote ${text(result.path)}`;
      } else {
        line = firstLine(result);
      }
      break;
    }
    case "turn_finished":
      line = `exit ${text(data.exit_code)}`;
      break;
    case "turn_failed":
      line = [text(data.reason), data.cause && `cause: ${text(data.cause)}`].filter(Boolean).join(" · ");
      break;
    default:
      line = firstLine(data.text);
  }

  return line.length > SUMMARY_CHARS ? line.slice(0, SUMMARY_CHARS) + "…" : line;
}


// ## FAILURE SUMMARY ##

function FailureSummary({ turn }: { turn: TurnSummary }) {
  if (turn.status === "finished" || turn.status === "running") return null;

  let headline: string;
  if (turn.status === "unresponsive") {
    headline = `Unresponsive: no events for ${formatDuration(turn.seconds_since_last_event)}`;
  } else if (turn.status === "paused") {
    headline = "Paused: a pause stopped this turn";
  } else if (turn.status === "incomplete") {
    headline = "Incomplete: no end was recorded";
  } else {
    const exit = turn.exit_code !== null ? `, exit ${turn.exit_code}` : "";
    headline = `Failed: ${turn.cause ?? "unknown"} (${turn.reason}${exit})`;
  }

  return (
    <div className="failure">
      <strong>{headline}</strong>
      {turn.last_action && (
        <div>Last action: <code>{turn.last_action.type}</code> {summarize(turn.last_action)}</div>
      )}
      {turn.unfinished_step && (
        <div>Never finished: <code>{turn.unfinished_step.type}</code> {summarize(turn.unfinished_step)}</div>
      )}
      {turn.stderr_tail && <pre>{turn.stderr_tail}</pre>}
    </div>
  );
}


// ## PAGE ##

export default function SandboxDetail({ sandboxId, onBack }: { sandboxId: string; onBack: () => void }) {
  const [turns, setTurns] = useState<TurnSummary[] | null>(null);
  const [chosenTurn, setChosenTurn] = useState<number | null>(null);   // null: follow the newest turn
  const [events, setEvents] = useState<SandboxEvent[]>([]);
  const [openSeq, setOpenSeq] = useState<number | null>(null);         // the event whose full data is shown
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"timeline" | "replay">("timeline");

  const shownTurn = chosenTurn ?? (turns && turns.length > 0 ? turns[turns.length - 1].turn : null);
  const turn = turns?.find((t) => t.turn === shownTurn);

  // reload the turn summaries every few seconds
  useEffect(() => {
    function load() {
      listTurns(sandboxId).then(setTurns).catch((e) => setError(e.message));
    }

    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [sandboxId]);

  // load the shown turn's events, then only the new ones every few seconds
  useEffect(() => {
    if (shownTurn === null) return;
    let stopped = false;
    let lastSeq = 0;
    setEvents([]);

    function load() {
      listEvents(sandboxId, shownTurn!, lastSeq)
        .then((newEvents) => {
          if (stopped || newEvents.length === 0) return;
          lastSeq = Math.max(lastSeq, newEvents[newEvents.length - 1].seq);

          // two loads can overlap, so only keep events past the newest one already shown
          setEvents((list) => {
            const newest = list.length > 0 ? list[list.length - 1].seq : 0;
            return [...list, ...newEvents.filter((event) => event.seq > newest)];
          });
        })
        .catch((e) => !stopped && setError(e.message));
    }

    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [sandboxId, shownTurn]);

  return (
    <main>
      <button onClick={onBack}>← All sandboxes</button>
      <h2 className="id">{sandboxId}</h2>

      {error && (
        <div className="error">
          {error}
          <button onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}

      {turns === null ? (
        <p>Loading turns…</p>
      ) : turns.length === 0 ? (
        <p>No turns yet: nobody has sent this sandbox a message.</p>
      ) : (
        <>
          <h3>Turns</h3>
          <table>
            <thead>
              <tr>
                <th>Turn</th>
                <th>Message</th>
                <th>Status</th>
                <th>Started</th>
                <th>Took</th>
                <th>Last action</th>
              </tr>
            </thead>
            <tbody>
              {turns.map((t) => (
                <tr
                  key={t.turn}
                  className={t.turn === shownTurn ? "clickable chosen" : "clickable"}
                  onClick={() => setChosenTurn(t.turn)}
                >
                  <td>{t.turn}</td>
                  <td className="message-cell" title={t.message}>{firstLine(t.message)}</td>
                  <td><span className={`badge ${t.status}`}>{t.status}</span></td>
                  <td>{formatTime(t.started_at)}</td>
                  <td>{t.ended_at !== null ? formatDuration(t.ended_at - t.started_at) : "…"}</td>
                  <td className="muted">{t.last_action ? summarize(t.last_action) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {turn && (
            <>
              <header>
                <h3>Turn {turn.turn}</h3>
                <div className="steps">
                  <button className={view === "timeline" ? "toggle on" : "toggle"} onClick={() => setView("timeline")}>
                    Timeline
                  </button>
                  <button className={view === "replay" ? "toggle on" : "toggle"} onClick={() => setView("replay")}>
                    Replay
                  </button>
                </div>
              </header>
              <FailureSummary turn={turn} />
              {view === "replay" ? (
                <Replay sandboxId={sandboxId} turn={turn.turn} lastSeq={turn.last_seq} />
              ) : (
              <table className="timeline">
                <tbody>
                  {events.map((event) =>
                    event.type === "user_message" ? (
                      // the user's message: highlighted and shown in full, since it's what the turn is about
                      <tr key={event.seq} className="user-row">
                        <td className="muted">{formatTime(event.at)}</td>
                        <td><span className="badge user">User</span></td>
                        <td>
                          <div className="bubble user">
                            <pre>{String(event.data.text ?? "")}</pre>
                          </div>
                        </td>
                      </tr>
                    ) : (
                      <tr key={event.seq} className="clickable" onClick={() => setOpenSeq(openSeq === event.seq ? null : event.seq)}>
                        <td className="muted">{formatTime(event.at)}</td>
                        <td><code>{event.type}</code></td>
                        <td>
                          {summarize(event)}
                          {openSeq === event.seq && <pre>{JSON.stringify(event.data, null, 2)}</pre>}
                        </td>
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
              )}
            </>
          )}
        </>
      )}
    </main>
  );
}
