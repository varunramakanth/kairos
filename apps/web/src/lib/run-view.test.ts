import { describe, expect, it } from "vitest";
import type { RunEvent } from "@/client/types.gen";
import { applyEvent, buildTurn, emptyTurn, isRunEvent } from "@/lib/run-view";

const base = { run_id: "run_1", ts: "2026-11-17T10:00:00Z" };
const usage = {
  input_tokens: 10,
  output_tokens: 5,
  cache_read_input_tokens: 0,
  cache_creation_input_tokens: 0,
};

// Fixtures are built loosely and cast once here; the reducer is what is under test.
function durable(seq: number, event: Record<string, unknown>): RunEvent {
  return { ...base, seq, ...event } as unknown as RunEvent;
}

function live(event: Record<string, unknown>): RunEvent {
  return { ...base, seq: null, ...event } as unknown as RunEvent;
}

const finished = (seq: number, text: string): RunEvent =>
  durable(seq, {
    type: "run_finished",
    stop_reason: "completed",
    output_text: text,
    error: null,
    usage,
    cost_usd: 0.0123,
  });

describe("text", () => {
  it("streams deltas into one message and settles on the final text", () => {
    const view = buildTurn([
      durable(0, { type: "run_started", model: "m", input: "hi" }),
      live({ type: "text_message_start", message_id: "msg_1" }),
      live({ type: "text_message_delta", message_id: "msg_1", delta: "Hel" }),
      live({ type: "text_message_delta", message_id: "msg_1", delta: "lo" }),
    ]);
    expect(view.items).toEqual([
      { kind: "text", id: "msg_1", text: "Hello", streaming: true },
    ]);

    const done = applyEvent(
      view,
      durable(1, {
        type: "text_message_end",
        message_id: "msg_1",
        text: "Hello!",
      }),
    );
    expect(done.items).toEqual([
      { kind: "text", id: "msg_1", text: "Hello!", streaming: false },
    ]);
  });

  it("renders replayed history without any deltas", () => {
    const view = buildTurn([
      durable(0, { type: "run_started", model: "m", input: "hi" }),
      durable(1, {
        type: "text_message_end",
        message_id: "msg_1",
        text: "Hi there",
      }),
      finished(2, "Hi there"),
    ]);
    expect(view.items).toEqual([
      { kind: "text", id: "msg_1", text: "Hi there", streaming: false },
    ]);
    expect(view.finished?.stop_reason).toBe("completed");
    expect(view.costUsd).toBe(0.0123);
  });

  it("drops partial output when the model stream is retried", () => {
    const view = buildTurn([
      live({ type: "text_message_start", message_id: "msg_1" }),
      live({ type: "text_message_delta", message_id: "msg_1", delta: "Hal" }),
      live({ type: "model_stream_reset", step: 1, reason: "overloaded" }),
      live({ type: "text_message_start", message_id: "msg_2" }),
      live({ type: "text_message_delta", message_id: "msg_2", delta: "Hello" }),
    ]);
    expect(view.items).toEqual([
      { kind: "text", id: "msg_2", text: "Hello", streaming: true },
    ]);
  });
});

describe("replay and ordering", () => {
  it("ignores durable events it has already applied", () => {
    const first = buildTurn([
      durable(0, { type: "text_message_end", message_id: "a", text: "x" }),
    ]);
    const again = applyEvent(
      first,
      durable(0, { type: "text_message_end", message_id: "b", text: "y" }),
    );
    expect(again).toBe(first);
  });

  it("ignores live deltas that arrive after the run finished", () => {
    const done = buildTurn([finished(0, "")]);
    expect(
      applyEvent(
        done,
        live({ type: "text_message_start", message_id: "late" }),
      ),
    ).toBe(done);
  });
});

describe("tools", () => {
  it("tracks a call from streaming arguments to its result", () => {
    const view = buildTurn([
      live({ type: "tool_call_start", tool_call_id: "t1", name: "web_search" }),
      live({ type: "tool_call_args", tool_call_id: "t1", delta: '{"query":' }),
      durable(0, {
        type: "tool_call_end",
        tool_call_id: "t1",
        name: "web_search",
        input: { query: "evals" },
      }),
      durable(1, {
        type: "tool_call_result",
        tool_call_id: "t1",
        name: "web_search",
        output: "1. Result",
        is_error: false,
        provenance: "untrusted",
        truncated: false,
        blob_id: null,
        duration_ms: 420,
      }),
    ]);
    expect(view.items).toHaveLength(1);
    const item = view.items[0];
    expect(item.kind).toBe("tool");
    if (item.kind !== "tool") return;
    expect(item.call).toMatchObject({
      id: "t1",
      status: "done",
      input: { query: "evals" },
      output: "1. Result",
      provenance: "untrusted",
      durationMs: 420,
    });
  });

  it("turns update_plan calls into the plan checklist", () => {
    const view = buildTurn([
      durable(0, {
        type: "tool_call_end",
        tool_call_id: "p1",
        name: "update_plan",
        input: {
          items: [
            { step: "Search", status: "completed" },
            { step: "Read", status: "in_progress" },
            { step: "bogus", status: "unknown" },
          ],
        },
      }),
    ]);
    expect(view.plan).toEqual([
      { step: "Search", status: "completed" },
      { step: "Read", status: "in_progress" },
    ]);
  });

  it("shows compaction as a notice", () => {
    const view = buildTurn([
      durable(0, {
        type: "context_compacted",
        epoch: 1,
        messages_summarized: 12,
        input_tokens_before: 160000,
        summary: "s",
        archive_blob_id: "blob_1",
        usage,
        cost_usd: 0.01,
      }),
    ]);
    expect(view.items[0]).toMatchObject({ kind: "notice", id: "compacted-1" });
  });
});

describe("pause and resume", () => {
  it("marks a call that waits for approval", () => {
    const view = buildTurn([
      durable(0, {
        type: "tool_call_end",
        tool_call_id: "t1",
        name: "notify_me",
        input: {},
      }),
      durable(1, {
        type: "approval_requested",
        tool_call_id: "t1",
        name: "notify_me",
        reason: "r",
        input: {},
        risk: "write_external",
      }),
    ]);
    const tool = view.items.find((i) => i.kind === "tool");
    expect(tool?.kind === "tool" && tool.call.status).toBe("awaiting_approval");
  });

  it("shows what a silent pause is waiting on instead of per-call notices", () => {
    const summary =
      "Waiting for your approval before I go ahead:\n- notify_me (title: Laundry)";
    const view = buildTurn([
      durable(0, {
        type: "approval_requested",
        tool_call_id: "t1",
        name: "notify_me",
        reason: "r",
        input: {},
        risk: "write_external",
      }),
      durable(1, {
        type: "run_finished",
        stop_reason: "awaiting_approval",
        output_text: summary,
        error: null,
        usage,
        cost_usd: 0,
      }),
    ]);
    expect(view.items.filter((i) => i.kind === "notice")).toEqual([
      { kind: "notice", id: "paused-1", text: summary },
    ]);
  });

  it("does not repeat the model's own words as a pause notice", () => {
    const view = buildTurn([
      durable(0, {
        type: "text_message_end",
        message_id: "m",
        text: "I'll send it once you approve.",
      }),
      durable(1, {
        type: "run_finished",
        stop_reason: "awaiting_approval",
        output_text: "I'll send it once you approve.",
        error: null,
        usage,
        cost_usd: 0,
      }),
    ]);
    expect(view.items.map((i) => i.kind)).toEqual(["text"]);
  });

  it("is live again after a paused run resumes", () => {
    const paused = buildTurn([
      durable(0, {
        type: "run_finished",
        stop_reason: "awaiting_approval",
        output_text: null,
        error: null,
        usage,
        cost_usd: 0,
      }),
    ]);
    expect(paused.finished?.stop_reason).toBe("awaiting_approval");
    const resumed = applyEvent(
      paused,
      durable(1, { type: "run_resumed", attempt: 2, from_step: 1 }),
    );
    expect(resumed.finished).toBeNull();
    const typing = applyEvent(
      resumed,
      live({ type: "text_message_start", message_id: "m" }),
    );
    expect(typing.items).toHaveLength(1);
  });
});

describe("isRunEvent", () => {
  it("accepts event payloads and rejects anything else", () => {
    expect(isRunEvent({ type: "run_started", run_id: "r" })).toBe(true);
    expect(isRunEvent("ping")).toBe(false);
    expect(isRunEvent(null)).toBe(false);
    expect(emptyTurn().lastSeq).toBe(-1);
  });
});
