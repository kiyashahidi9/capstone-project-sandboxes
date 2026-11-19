// replay of one turn: what the model was sent at each step, shown as a chat, then what it answered

import { useEffect, useState } from "react";
import { getReplay, type ReplayStep } from "./api";

type Message = Record<string, unknown>;

// one tool call with what it returned, ready to show as a card
type ToolCall = { id: string; name: string; arguments: unknown; result: unknown; finished: boolean };

// one turn of the chat: the user's message and everything the assistant did after it
type ChatGroup = { user: Message | null; items: ChatItem[]; isNew: boolean };
type ChatItem = { kind: "text"; text: string; isNew: boolean } | { kind: "tool"; call: ToolCall; isNew: boolean };


// ## FORMATTING ##

// any value as readable text: strings as they are, anything else as indented JSON
function pretty(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

// JSON text back into data when it is JSON, so results and arguments show their fields
function parsed(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function firstLine(value: unknown): string {
  const line = pretty(value).split("\n").find((part) => part.trim() !== "") ?? "";
  return line.length > 100 ? line.slice(0, 100) + "…" : line;
}

// a tool call in a few words: the command for run_command, the path for files
function describeArguments(args: unknown): string {
  const fields = (parsed(args) ?? {}) as Record<string, unknown>;
  return pretty(fields.cmd ?? fields.path ?? args);
}

// a tool's result in a few words: exit code and first output line for commands
function describeResult(result: unknown): string {
  const value = parsed(result);
  if (value && typeof value === "object" && "exit_code" in value) {
    const fields = value as Record<string, unknown>;
    return `exit ${fields.exit_code} · ${firstLine(fields.stdout) || firstLine(fields.stderr)}`;
  }
  return firstLine(value);
}


// ## MESSAGES INTO A CHAT ##

// groups the messages sent to the model by turn, pairing each tool call with its result
function toChat(messages: Message[], firstNew: number): { system: Message | null; groups: ChatGroup[] } {
  let system: Message | null = null;
  const groups: ChatGroup[] = [];
  const callsById = new Map<string, ToolCall>();

  messages.forEach((message, index) => {
    const isNew = index >= firstNew;

    if (message.role === "system") {
      system = message;
      return;
    }
    if (message.role === "user" || groups.length === 0) {
      groups.push({ user: message.role === "user" ? message : null, items: [], isNew });
      if (message.role === "user") return;
    }

    const group = groups[groups.length - 1];
    group.isNew ||= isNew;

    if (message.role === "assistant") {
      if (typeof message.content === "string" && message.content.trim() !== "") {
        group.items.push({ kind: "text", text: message.content, isNew });
      }
      const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
      for (const raw of calls) {
        const fields = raw as { id?: string; function?: { name?: string; arguments?: string } };
        const call: ToolCall = {
          id: fields.id ?? "",
          name: fields.function?.name ?? "?",
          arguments: fields.function?.arguments,
          result: undefined,
          finished: false,
        };
        callsById.set(call.id, call);
        group.items.push({ kind: "tool", call, isNew });
      }
    } else if (message.role === "tool") {
      // the result belongs on its call's card
      const call = callsById.get(String(message.tool_call_id));
      if (call) {
        call.result = message.content;
        call.finished = true;
      }
    }
  });

  return { system, groups };
}


// ## PIECES ##

function ToolCard({ call, isNew }: { call: ToolCall; isNew: boolean }) {
  const failed = typeof call.result === "string" && call.result.startsWith("error:");

  return (
    <div className={isNew ? "tool-card new" : "tool-card"}>
      <div>
        <code>{call.name}</code> {describeArguments(call.arguments)}
      </div>
      {call.finished ? (
        <details>
          <summary className={failed ? "result failed" : "result"}>returned: {describeResult(call.result)}</summary>
          <pre>{pretty(parsed(call.result))}</pre>
        </details>
      ) : (
        <div className="result failed">never finished</div>
      )}
      <details>
        <summary className="muted">arguments</summary>
        <pre>{pretty(parsed(call.arguments))}</pre>
      </details>
    </div>
  );
}

function AssistantText({ text, isNew }: { text: string; isNew: boolean }) {
  return (
    <div className={isNew ? "bubble assistant new" : "bubble assistant"}>
      <div className="who">assistant</div>
      <pre>{text}</pre>
    </div>
  );
}

// one turn of the chat, collapsible, titled with the start of the user's message
function TurnGroup({ group, number, open }: { group: ChatGroup; number: number; open: boolean }) {
  return (
    <details className="chat-group" open={open}>
      <summary>
        Turn {number}: {firstLine(group.user?.content) || "(no message)"}
      </summary>
      {group.user && (
        <div className="bubble user">
          <div className="who">user</div>
          <pre>{pretty(group.user.content)}</pre>
        </div>
      )}
      {group.items.map((item, itemIndex) =>
        item.kind === "text" ? (
          <AssistantText key={itemIndex} text={item.text} isNew={item.isNew} />
        ) : (
          <ToolCard key={itemIndex} call={item.call} isNew={item.isNew} />
        ),
      )}
    </details>
  );
}

// the system prompt, earlier turns folded into one "Chat history" dropdown, then the current turn open
function Chat({ messages, newCount }: { messages: Message[]; newCount: number }) {
  const { system, groups } = toChat(messages, messages.length - newCount);
  const earlier = groups.slice(0, -1);
  const current = groups[groups.length - 1];

  return (
    <div className="chat">
      {system && (
        <details className="bubble system">
          <summary className="who">system prompt</summary>
          <pre>{pretty(system.content)}</pre>
        </details>
      )}

      {earlier.length > 0 && (
        <details className="chat-history">
          <summary>Chat history ({earlier.length} earlier {earlier.length === 1 ? "turn" : "turns"})</summary>
          {earlier.map((group, index) => (
            <TurnGroup key={index} group={group} number={index + 1} open={false} />
          ))}
        </details>
      )}

      {current && <TurnGroup group={current} number={groups.length} open />}
    </div>
  );
}


// ## ONE STEP ##

function Step({ step }: { step: ReplayStep }) {
  const reply = step.response;

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
      <Chat messages={step.messages} newCount={step.new_messages} />

      <h4>Model replied</h4>
      {reply === null ? (
        <p className="muted">No reply recorded: the call never came back.</p>
      ) : (
        <div className="chat">
          {typeof reply.text === "string" && reply.text.trim() !== "" && <AssistantText text={reply.text} isNew />}
          {step.tools.map((tool) => (
            <ToolCard
              key={tool.tool_call_id}
              call={{ id: tool.tool_call_id, name: tool.name ?? "?", arguments: tool.arguments, result: tool.result, finished: tool.finished }}
              isNew
            />
          ))}
        </div>
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
