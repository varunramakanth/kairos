"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { getRun, getRunMessages } from "@/client/sdk.gen";
import type { Message, RunDetail, RunEvent } from "@/client/types.gen";
import { errorMessage } from "@/lib/api";

/** The trace viewer: every durable event of a run on a timeline, plus what the model saw. */
export function RunTrace({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = use(params);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      const [run, history] = await Promise.all([
        getRun({ path: { run_id: runId } }),
        getRunMessages({ path: { run_id: runId } }),
      ]);
      if (run.data) setDetail(run.data);
      else setError(errorMessage(run.error));
      if (history.data) setMessages(history.data);
    })();
  }, [runId]);

  if (error) return <p className="p-6 text-destructive">{error}</p>;
  if (!detail) return <p className="p-6 text-muted-foreground">Loading…</p>;

  const { run, events } = detail;
  const start = events.length > 0 ? Date.parse(events[0].ts) : 0;
  const finished = events.find((e) => e.type === "run_finished");
  const steps = events.filter((e) => e.type === "model_call_finished");
  const tools = events.filter((e) => e.type === "tool_call_result");
  const elapsed =
    events.length > 0
      ? (Date.parse(events[events.length - 1].ts) - start) / 1000
      : 0;

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 overflow-y-auto px-4 md:px-6 py-6">
      <header className="space-y-1">
        <div className="text-muted-foreground text-sm">
          {run.thread_id ? (
            <Link
              href={`/threads/${run.thread_id}`}
              className="underline-offset-4 hover:underline"
            >
              ← Back to thread
            </Link>
          ) : (
            "Run"
          )}
        </div>
        <h1 className="font-semibold text-xl">{run.input}</h1>
        <p className="font-mono text-muted-foreground text-xs">{run.id}</p>
      </header>

      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-6">
        <Stat label="Status" value={run.stop_reason ?? run.status} />
        <Stat label="Model" value={run.model} />
        <Stat label="Model calls" value={String(steps.length)} />
        <Stat label="Tool calls" value={String(tools.length)} />
        <Stat
          label="Tokens"
          value={finished ? totalTokens(finished.usage).toLocaleString() : "—"}
        />
        <Stat
          label="Cost"
          value={`$${run.cost_usd.toFixed(4)} · ${elapsed.toFixed(1)}s`}
        />
      </dl>

      <section>
        <h2 className="mb-2 font-medium">Timeline</h2>
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full min-w-[36rem] text-sm">
            <thead className="bg-muted/50 text-left text-muted-foreground text-xs">
              <tr>
                <th className="px-3 py-2 font-normal">+s</th>
                <th className="px-3 py-2 font-normal">seq</th>
                <th className="px-3 py-2 font-normal">event</th>
                <th className="px-3 py-2 font-normal">detail</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {events.map((event) => (
                <tr key={event.seq} className={rowTone(event)}>
                  <td className="px-3 py-1.5 font-mono text-muted-foreground text-xs tabular-nums">
                    {((Date.parse(event.ts) - start) / 1000).toFixed(2)}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-muted-foreground text-xs">
                    {event.seq}
                  </td>
                  <td className="px-3 py-1.5 font-mono text-xs">
                    {event.type}
                  </td>
                  <td
                    className="max-w-xl truncate px-3 py-1.5 text-xs"
                    title={describe(event)}
                  >
                    {describe(event)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="mb-2 font-medium">Model input (current context)</h2>
        <div className="space-y-2">
          {messages.map((message, index) => (
            // Messages are an append-only list; their position is their identity.
            // biome-ignore lint/suspicious/noArrayIndexKey: see above
            <MessageView key={index} message={message} />
          ))}
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border px-3 py-2">
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd className="truncate font-medium text-sm" title={value}>
        {value}
      </dd>
    </div>
  );
}

function totalTokens(usage: {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
}): number {
  return (
    (usage.input_tokens ?? 0) +
    (usage.output_tokens ?? 0) +
    (usage.cache_read_input_tokens ?? 0) +
    (usage.cache_creation_input_tokens ?? 0)
  );
}

function rowTone(event: RunEvent): string {
  if (event.type === "tool_call_result" && event.is_error)
    return "bg-destructive/5";
  if (event.type === "run_finished" && event.stop_reason !== "completed")
    return "bg-destructive/5";
  if (event.type === "model_call_finished" || event.type === "step_started")
    return "bg-muted/30";
  return "";
}

function describe(event: RunEvent): string {
  switch (event.type) {
    case "run_started":
      return `model ${event.model}`;
    case "run_resumed":
      return `attempt ${event.attempt}, from step ${event.from_step}`;
    case "step_started":
    case "step_finished":
      return `step ${event.step}`;
    case "model_call_finished": {
      const u = event.usage;
      return `${event.model} · ${event.stop_reason} · in ${u.input_tokens ?? 0} (+${u.cache_read_input_tokens ?? 0} cached, ${u.cache_creation_input_tokens ?? 0} written) · out ${u.output_tokens ?? 0} · $${event.cost_usd.toFixed(4)}`;
    }
    case "text_message_end":
      return event.text;
    case "tool_call_end":
      return `${event.name} ${JSON.stringify(event.input)}`;
    case "tool_call_result":
      return `${event.name} · ${event.duration_ms} ms${event.is_error ? " · error" : ""}${event.truncated ? " · truncated" : ""} · ${event.provenance} · ${event.output.slice(0, 160)}`;
    case "tool_call_uncertain":
      return `${event.name}: outcome unknown`;
    case "approval_requested":
      return `${event.name}: ${event.reason}`;
    case "budget_updated":
      return `${event.spent.total_tokens ?? 0} tokens · $${(event.spent.cost_usd ?? 0).toFixed(4)} · ${event.spent.tool_calls ?? 0} tool calls`;
    case "context_compacted":
      return `epoch ${event.epoch}: ${event.messages_summarized} messages, ~${event.input_tokens_before} tokens → summary`;
    case "run_finished":
      return `${event.stop_reason}${event.error ? `: ${event.error}` : ""}`;
    default:
      return "";
  }
}

/** Tool blocks have ids; text and opaque blocks only have their position. */
function blockKey(block: Message["content"][number], index: number): string {
  if (block.type === "tool_use") return block.id;
  if (block.type === "tool_result") return `result-${block.tool_use_id}`;
  return `${block.type}-${index}`;
}

function MessageView({ message }: { message: Message }) {
  return (
    <details
      className="rounded-lg border text-sm"
      open={message.role === "user" && message.content.length < 3}
    >
      <summary className="cursor-pointer px-3 py-1.5 font-mono text-muted-foreground text-xs">
        {message.role} · {message.content.map((block) => block.type).join(", ")}
      </summary>
      <div className="space-y-2 border-t px-3 py-2">
        {message.content.map((block, index) => (
          <pre
            key={blockKey(block, index)}
            className="max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-xs"
          >
            {block.type === "text"
              ? block.text
              : block.type === "tool_use"
                ? `tool_use ${block.name} ${JSON.stringify(block.input, null, 2)}`
                : block.type === "tool_result"
                  ? `tool_result (${block.provenance ?? "trusted"})\n${block.content}`
                  : `[${block.kind}: provider-native block, replayed unchanged]`}
          </pre>
        ))}
      </div>
    </details>
  );
}
