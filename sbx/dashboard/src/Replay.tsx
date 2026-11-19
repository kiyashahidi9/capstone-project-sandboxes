// replay of one turn: what the model was sent at each step, what it answered, and what its tools returned

import { useEffect, useState } from "react";
import { getReplay, type ReplayStep } from "./api";


// ## FORMATTING ##

// any value as readable text: strings as they are, anything else as indented JSON
function pretty(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

// a tool call in OpenAI's format, as name(arguments)
function describeCall(call: unknown): string {
  const fields = (call ?? {}) as { function?: { name?: string; arguments?: string } };
  return `${fields.function?.name ?? "?"}(${fields.function?.arguments ?? ""})`;
}

// the first line of a message, for its collapsed row
function preview(message: Record<string, unknown>): string {
  const calls = Array.isArray(message.tool_calls) ? message.tool_calls.map(describeCall).join(", ") : "";
  const line = (pretty(message.content).split("\n")[0] || calls).trim();
  return line.length > 100 ? line.slice(0, 100) + "…" : line;
}


// ## ONE MESSAGE ##

function Message({ message, open }: { message: Record<string, unknown>; open: boolean }) {
  const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];

  return (
    <details className="message" open={open}>
      <summary>
        <code>{String(message.role)}</code> {preview(message)}
      </summary>
      {message.content !== null && message.content !== undefined && <pre>{pretty(message.content)}</pre>}
      {calls.map((call, index) => (
        <pre key={index}>calls {describeCall(call)}</pre>
      ))}
    </details>
  );
}


// ## ONE STEP ##

function Step({ step }: { step: ReplayStep }) {
  const firstNew = step.messages.length - step.new_messages;

  return (
    <div>
      <p>
        Step {step.step} · {step.model ?? "unknown model"} · {new Date(step.at * 1000).toLocaleTimeString()}{" "}
        {step.matches_record === true && <span className="badge finished">matches what the agent recorded</span>}
        {step.matches_record === false && (
          <span className="badge failed" title="Long text cut at 64,000 characters, or an agent that doesn't build its chat with sdk/history.py">
            differs from what the agent recorded
          </span>
        )}
      </p>

      <h4>Sent to the model ({step.messages.length} messages, {step.new_messages} new)</h4>
      {step.messages.map((message, index) => (
        <Message key={index} message={message} open={index >= firstNew} />
      ))}

      <h4>Model replied</h4>
      {step.response === null ? (
        <p className="muted">No reply recorded: the call never came back.</p>
      ) : (
        <>
          {typeof step.response.text === "string" && <pre>{step.response.text}</pre>}
          {step.tools.map((tool) => (
            <div key={tool.tool_call_id} className="tool">
              <code>{tool.name}</code>
              <pre>{pretty(tool.arguments)}</pre>
              {tool.finished ? (
                <>
                  <div className="muted">returned</div>
                  <pre>{pretty(tool.result)}</pre>
                </>
              ) : (
                <div className="muted">never finished</div>
              )}
            </div>
          ))}
        </>
      )}
    </div>
  );
}


// ## PAGE ##

// lastSeq changes whenever the turn gets a new event, which reloads the replay
export default function Replay({ sandboxId, turn, lastSeq }: { sandboxId: string; turn: number; lastSeq: number }) {
  const [steps, setSteps] = useState<ReplayStep[] | null>(null);
  const [chosen, setChosen] = useState<number | null>(null);   // null: follow the newest step
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    getReplay(sandboxId, turn)
      .then((list) => !stopped && setSteps(list))
      .catch((e) => !stopped && setError(e.message));
    return () => {
      stopped = true;
    };
  }, [sandboxId, turn, lastSeq]);

  // a different turn starts back at its newest step
  useEffect(() => setChosen(null), [turn]);

  if (error) return <div className="error">{error}</div>;
  if (steps === null) return <p>Loading replay…</p>;
  if (steps.length === 0) {
    return <p className="muted">No model calls recorded in this turn: the agent didn't print step events (see docs/agent_contract.md).</p>;
  }

  const shown = steps[Math.min(chosen ?? steps.length - 1, steps.length - 1)];

  return (
    <div>
      <div className="steps">
        {steps.map((step, index) => (
          <button key={step.seq} className={step === shown ? "toggle on" : "toggle"} onClick={() => setChosen(index)}>
            Step {step.step}
          </button>
        ))}
      </div>
      <Step step={shown} />
    </div>
  );
}
