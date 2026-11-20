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

// a duration in words: "9 seconds", "1 second", or "1m 4s" once it's a minute or more
function describeDuration(seconds: number): string {
  if (seconds >= 60) return formatDuration(seconds);
  return seconds === 1 ? "1 second" : `${seconds} seconds`;
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
  return `${text(name)} ${toolTarget(args)}`;
}

// what a tool call works on: the command for run_command, the path for files
function toolTarget(args: unknown): string {
  const fields = (args ?? {}) as Record<string, unknown>;
  return text(fields.cmd ?? fields.path ?? args);
}

// a tool result counts as failed when it's an error message or a command that exited non-zero
function toolFailed(result: unknown): boolean {
  if (typeof result === "string") return result.startsWith("error:");
  if (result && typeof result === "object" && "exit_code" in result) {
    return (result as { exit_code: unknown }).exit_code !== 0;
  }
  return false;
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


// ## AGENT OUTPUT ##

// what the agent told the user: its first line, with a dropdown for the rest when there is more
function AgentText({ text }: { text: string }) {
  const [first, ...rest] = text.split("\n");
  const shortFirst = first.length > SUMMARY_CHARS ? first.slice(0, SUMMARY_CHARS) + "…" : first;
  const hasMore = rest.some((line) => line.trim() !== "") || shortFirst !== first;

  if (!hasMore) {
    return <div className="bubble agent"><pre>{first}</pre></div>;
  }

  return (
    <details className="bubble agent">
      <summary>{shortFirst}</summary>
      <pre>{shortFirst !== first ? text : rest.join("\n")}</pre>
    </details>
  );
}


// ## TIMELINE PIECES ##

// one timeline row: an ordinary event, or an llm_request paired with its llm_response as one "llm call"
type TimelineItem =
  | { seq: number; event: SandboxEvent; request?: undefined }
  | { seq: number; request: SandboxEvent; response: SandboxEvent | null; event?: undefined };

// a run of rows, or one divider (turn_started / turn_finished / turn_failed) between them
type TimelinePiece = { divider: SandboxEvent; rows?: undefined } | { divider?: undefined; rows: TimelineItem[] };

const DIVIDER_TYPES = ["turn_started", "turn_finished", "turn_failed"];

// pairs each llm_request with the llm_response after it (before the next request); null while it's still waiting
function timelineItems(events: SandboxEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const paired = new Set<number>();   // responses already shown inside an llm call

  events.forEach((event, index) => {
    if (paired.has(event.seq)) return;

    if (event.type !== "llm_request") {
      items.push({ seq: event.seq, event });
      return;
    }

    let response: SandboxEvent | null = null;
    for (const later of events.slice(index + 1)) {
      if (later.type === "llm_request") break;
      if (later.type === "llm_response") {
        response = later;
        paired.add(later.seq);
        break;
      }
    }
    items.push({ seq: event.seq, request: event, response });
  });

  return items;
}

function timelinePieces(events: SandboxEvent[]): TimelinePiece[] {
  const pieces: TimelinePiece[] = [];

  for (const item of timelineItems(events)) {
    const last = pieces[pieces.length - 1];
    if (item.event && DIVIDER_TYPES.includes(item.event.type)) {
      pieces.push({ divider: item.event });
    } else if (last && last.rows) {
      last.rows.push(item);
    } else {
      pieces.push({ rows: [item] });
    }
  }
  return pieces;
}

// a divider's words: what happened and when; a finish or failure also says how long the turn took
// (from the user's message, like the turn's "Took"), and a failure says why
function dividerText(event: SandboxEvent, turnStartedAt: number): string {
  const when = formatTime(event.at);
  const took = `took ${describeDuration(event.at - turnStartedAt)}`;

  if (event.type === "turn_started") return `turn started · ${when}`;
  if (event.type === "turn_finished") return `turn finished · ${when} · ${took}`;

  const why = [text(event.data.reason), event.data.cause && `cause: ${text(event.data.cause)}`].filter(Boolean).join(", ");
  return `turn failed · ${when} · ${took}${why ? ` · ${why}` : ""}`;
}


// ## TOOLS ##

// which tool a call used, as an outlined label, then what it worked on: "$ command" or the file's path
function ToolLabel({ call }: { call: SandboxEvent | undefined }) {
  if (!call) return <span className="tool-chip">tool</span>;

  const name = text(call.data.name);
  const target = toolTarget(call.data.arguments);

  return (
    <>
      <span className="tool-chip">{name}</span>
      <code className="tool-target">{name === "run_command" ? `$ ${target}` : target}</code>
    </>
  );
}


// ## LLM CALLS ##

// the model, what went in (behind a toggle), then exactly what came out, each in a teal box
function LlmCallDetails({ request, response }: { request: SandboxEvent; response: SandboxEvent | null }) {
  const messages = Array.isArray(request.data.messages) ? (request.data.messages as Record<string, unknown>[]) : [];
  const calls = response && Array.isArray(response.data.tool_calls) ? response.data.tool_calls : [];

  return (
    <div className="llm-call" onClick={(e) => e.stopPropagation()}>
      <div className="llm-part">
        <div className="llm-label">Model</div>
        <div className="teal-box"><code>{text(request.data.model) || "not recorded"}</code></div>
      </div>

      <div className="llm-part">
        <div className="llm-label">Input</div>
        {messages.length === 0 ? (
          <div className="muted">no messages recorded</div>
        ) : (
          <details>
            <summary className="muted">{messages.length} messages</summary>
            <div className="teal-box">
              <pre>{JSON.stringify(messages, null, 2)}</pre>
            </div>
          </details>
        )}
      </div>

      <div className="llm-part">
        <div className="llm-label">Output</div>
        {response === null ? (
          <div className="muted">waiting for a response</div>
        ) : (
          // exactly what the model returned: its text as written (teal), and its tool calls as recorded (orange, like tools)
          <div className="llm-output">
            {typeof response.data.text === "string" && response.data.text.trim() !== "" && (
              <div className="teal-box">
                <pre>{response.data.text}</pre>
              </div>
            )}
            {calls.length > 0 && (
              <div className="orange-box">
                <pre>{JSON.stringify(calls, null, 2)}</pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}


// ## TURN INFO (a turn row's dropdown) ##

// the user's message, when the turn started, how long it took, and the agent's last action
function TurnInfo({ turn }: { turn: TurnSummary }) {
  const took = turn.ended_at !== null ? formatDuration(turn.ended_at - turn.started_at) : "still going";

  return (
    <div className="turn-info">
      <div className="bubble user">
        <div className="who">user</div>
        <pre>{turn.message}</pre>
      </div>
      <dl>
        <dt>Started</dt>
        <dd>{formatTime(turn.started_at)}</dd>
        <dt>Took</dt>
        <dd>{took}</dd>
        <dt>Last action</dt>
        <dd>
          {turn.last_action ? (
            <><code>{turn.last_action.type}</code> {summarize(turn.last_action)}</>
          ) : (
            <span className="muted">none yet</span>
          )}
        </dd>
      </dl>
    </div>
  );
}


// ## PAGE ##

export default function SandboxDetail({ sandboxId, onBack }: { sandboxId: string; onBack: () => void }) {
  const [turns, setTurns] = useState<TurnSummary[] | null>(null);
  const [chosenTurn, setChosenTurn] = useState<number | null>(null);   // null: follow the newest turn
  const [expanded, setExpanded] = useState<number[]>([]);              // turn rows whose info dropdown is open
  const [events, setEvents] = useState<SandboxEvent[]>([]);
  const [openSeq, setOpenSeq] = useState<number | null>(null);         // the event whose full data is shown
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"timeline" | "replay">("timeline");

  const shownTurn = chosenTurn ?? (turns && turns.length > 0 ? turns[turns.length - 1].turn : null);
  const turn = turns?.find((t) => t.turn === shownTurn);


  // opens or closes one turn row's info, without choosing that turn for the timeline
  function toggleInfo(number: number) {
    setExpanded((list) => (list.includes(number) ? list.filter((n) => n !== number) : [...list, number]));
  }

  // reload the turn summaries every few seconds
  useEffect(() => {
    function load() {
      listTurns(sandboxId).then(setTurns).catch((e) => setError(e.message));
    }

    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [sandboxId]);

  // each tool call's tool_started, so a tool result can say which tool and command it came from
  const toolCalls = new Map(
    events.filter((event) => event.type === "tool_started").map((event) => [text(event.data.tool_call_id), event]),
  );

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

  // an llm_request and its llm_response as one row: input size on one line, what came out on the next; click for the model and both
  function llmCallRow(request: SandboxEvent, response: SandboxEvent | null) {
    const count = Array.isArray(request.data.messages) ? request.data.messages.length : 0;
    const open = openSeq === request.seq;

    return (
      <tr key={request.seq} className="clickable llm-row" onClick={() => setOpenSeq(open ? null : request.seq)}>
        <td className="muted">{formatTime(request.at)}</td>
        <td><span className="badge llm">llm call</span></td>
        <td>
          <div className="io-line">
            <span className="io-label">Input</span> {count} messages
          </div>
          <div className="io-line">
            <span className="io-label">Output</span>{" "}
            {response ? summarize(response) || "(empty reply)" : <span className="muted">waiting for a response</span>}
          </div>
          {open && <LlmCallDetails request={request} response={response} />}
        </td>
      </tr>
    );
  }

  // one timeline row; turn_started and turn_finished aren't rows, they're dividers between tables
  function timelineRow(item: TimelineItem) {
    if (item.request) {
      return llmCallRow(item.request, item.response);
    }

    const event = item.event;
    const toggle = () => setOpenSeq(openSeq === event.seq ? null : event.seq);

    if (event.type === "user_message") {
      // the user's message: highlighted and shown in full, since it's what the turn is about
      return (
        <tr key={event.seq} className="user-row">
          <td className="muted">{formatTime(event.at)}</td>
          <td><span className="badge user">{event.type}</span></td>
          <td>
            <div className="bubble user">
              <pre>{String(event.data.text ?? "")}</pre>
            </div>
          </td>
        </tr>
      );
    }

    if (event.type === "agent_output" || event.type === "question") {
      // what the agent told the user: its first line, with a dropdown for the rest
      return (
        <tr key={event.seq} className="agent-row">
          <td className="muted">{formatTime(event.at)}</td>
          <td><span className="badge agent">{event.type}</span></td>
          <td><AgentText text={String(event.data.text ?? "")} /></td>
        </tr>
      );
    }

    if (event.type === "tool_started" || event.type === "tool_completed") {
      // a tool the agent ran: its name and command, then whether it worked
      const failed = toolFailed(event.data.result);
      return (
        <tr key={event.seq} className="clickable tool-row" onClick={toggle}>
          <td className="muted">{formatTime(event.at)}</td>
          <td><span className="badge tool">{event.type}</span></td>
          <td>
            {event.type === "tool_started" ? (
              <ToolLabel call={event} />
            ) : (
              // the tool and command it answers (matched by tool_call_id, in case an agent runs several at once), then the result
              <>
                <div className="io-line">
                  <span className="io-label">Input</span> <ToolLabel call={toolCalls.get(text(event.data.tool_call_id))} />
                </div>
                <div className="io-line">
                  <span className="io-label">Output</span>{" "}
                  <span className={failed ? "result failed" : "result"}>
                    {failed ? "✗" : "✓"} {summarize(event)}
                  </span>
                </div>
              </>
            )}
            {openSeq === event.seq && <pre>{JSON.stringify(event.data, null, 2)}</pre>}
          </td>
        </tr>
      );
    }

    return (
      <tr key={event.seq} className="clickable" onClick={toggle}>
        <td className="muted">{formatTime(event.at)}</td>
        <td><code>{event.type}</code></td>
        <td>
          {summarize(event)}
          {openSeq === event.seq && <pre>{JSON.stringify(event.data, null, 2)}</pre>}
        </td>
      </tr>
    );
  }

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
                <th></th>
                <th>Turn</th>
                <th>Status</th>
              </tr>
            </thead>
            {turns.map((t) => {
              const infoOpen = expanded.includes(t.turn);
              return (
                // one tbody per turn: its row, and its info dropdown under it when open
                <tbody key={t.turn}>
                  <tr
                    className={t.turn === shownTurn ? "clickable chosen" : "clickable"}
                    onClick={() => setChosenTurn(t.turn)}
                  >
                    <td>
                      <button
                        className="link dropdown-icon"
                        title={infoOpen ? "Hide this turn's info" : "Show more about this turn"}
                        onClick={(e) => {
                          e.stopPropagation();    // only the arrow opens the info; the row still picks the turn
                          toggleInfo(t.turn);
                        }}
                      >
                        ⋮
                      </button>
                    </td>
                    <td>Turn {t.turn}</td>
                    <td className="status-cell"><span className={`badge ${t.status}`}>{t.status}</span></td>
                  </tr>
                  {infoOpen && (
                    <tr>
                      <td colSpan={3}><TurnInfo turn={t} /></td>
                    </tr>
                  )}
                </tbody>
              );
            })}
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
                // the timeline: tables of rows, split by faded dividers where the turn starts and finishes
                <div>
                  {timelinePieces(events).map((piece) =>
                    piece.divider ? (
                      <div key={piece.divider.seq} className={piece.divider.type === "turn_failed" ? "divider failed" : "divider"}>
                        {dividerText(piece.divider, turn.started_at)}
                      </div>
                    ) : (
                      <table key={piece.rows[0].seq} className="timeline">
                        <colgroup>
                          <col className="time-column" />
                          <col className="type-column" />
                          <col />
                        </colgroup>
                        <tbody>{piece.rows.map(timelineRow)}</tbody>
                      </table>
                    ),
                  )}
                </div>
              )}
            </>
          )}
        </>
      )}
    </main>
  );
}
