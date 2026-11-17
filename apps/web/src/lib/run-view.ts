/**
 * Folds a run's event stream into what the chat shows for one turn.
 *
 * The same function handles replayed history (durable events from the API) and
 * live streams (durable plus ephemeral deltas), so a page reload renders exactly
 * what the live view rendered. Durable events carry a `seq`; any `seq` at or below
 * the last one applied is a replay and is ignored.
 */
import type {
  ApprovalRequested,
  ContextCompacted,
  Provenance,
  RunEvent,
  RunFinished,
} from "@/client/types.gen";

export type ToolStatus =
  | "streaming"
  | "running"
  | "done"
  | "error"
  | "uncertain";

export interface ToolCallView {
  id: string;
  name: string;
  argsText: string;
  input: Record<string, unknown> | null;
  status: ToolStatus;
  output?: string;
  provenance?: Provenance;
  truncated?: boolean;
  blobId?: string | null;
  durationMs?: number;
}

export interface PlanItem {
  step: string;
  status: "pending" | "in_progress" | "completed";
}

export type TurnItem =
  | { kind: "text"; id: string; text: string; streaming: boolean }
  | { kind: "tool"; call: ToolCallView }
  | { kind: "notice"; id: string; text: string };

export interface TurnView {
  items: TurnItem[];
  plan: PlanItem[] | null;
  finished: RunFinished | null;
  lastSeq: number;
  costUsd: number;
}

export const PLAN_TOOL = "update_plan";

export function emptyTurn(): TurnView {
  return { items: [], plan: null, finished: null, lastSeq: -1, costUsd: 0 };
}

export function buildTurn(events: readonly RunEvent[]): TurnView {
  return events.reduce(applyEvent, emptyTurn());
}

/** Minimal runtime check for SSE payloads, which arrive as `unknown`. */
export function isRunEvent(value: unknown): value is RunEvent {
  return (
    typeof value === "object" &&
    value !== null &&
    "type" in value &&
    typeof value.type === "string" &&
    "run_id" in value
  );
}

export function applyEvent(view: TurnView, event: RunEvent): TurnView {
  const seq = event.seq ?? null;
  if (seq !== null && seq <= view.lastSeq) return view;
  if (seq === null && view.finished) return view; // late live delta after the end
  const next = seq === null ? view : { ...view, lastSeq: seq };

  switch (event.type) {
    case "text_message_start":
      return withItems(next, [
        ...next.items,
        { kind: "text", id: event.message_id, text: "", streaming: true },
      ]);
    case "text_message_delta":
      return mapText(next, event.message_id, (item) => ({
        ...item,
        text: item.text + event.delta,
      }));
    case "text_message_end": {
      const exists = next.items.some(
        (i) => i.kind === "text" && i.id === event.message_id,
      );
      if (!exists) {
        return withItems(next, [
          ...next.items,
          {
            kind: "text",
            id: event.message_id,
            text: event.text,
            streaming: false,
          },
        ]);
      }
      return mapText(next, event.message_id, (item) => ({
        ...item,
        text: event.text,
        streaming: false,
      }));
    }
    case "model_stream_reset":
      // The model call is being retried: drop whatever it streamed so far.
      return withItems(
        next,
        next.items.filter(
          (i) =>
            !(i.kind === "text" && i.streaming) &&
            !(i.kind === "tool" && i.call.status === "streaming"),
        ),
      );
    case "tool_call_start":
      return upsertTool(next, event.tool_call_id, (call) => call, {
        id: event.tool_call_id,
        name: event.name,
        argsText: "",
        input: null,
        status: "streaming",
      });
    case "tool_call_args":
      return upsertTool(next, event.tool_call_id, (call) => ({
        ...call,
        argsText: call.argsText + event.delta,
      }));
    case "tool_call_end": {
      const input = event.input as Record<string, unknown>;
      const updated = upsertTool(
        next,
        event.tool_call_id,
        (call) => ({
          ...call,
          input,
          argsText: JSON.stringify(input),
          status: "running",
        }),
        {
          id: event.tool_call_id,
          name: event.name,
          argsText: "",
          input,
          status: "running",
        },
      );
      return event.name === PLAN_TOOL
        ? { ...updated, plan: readPlan(input) ?? updated.plan }
        : updated;
    }
    case "tool_call_result":
      return upsertTool(next, event.tool_call_id, (call) => ({
        ...call,
        status: event.is_error ? "error" : "done",
        output: event.output,
        provenance: event.provenance,
        truncated: event.truncated ?? false,
        blobId: event.blob_id ?? null,
        durationMs: event.duration_ms,
      }));
    case "tool_call_uncertain":
      return upsertTool(next, event.tool_call_id, (call) => ({
        ...call,
        status: "uncertain",
      }));
    case "approval_requested":
      return notice(
        next,
        `approval-${event.tool_call_id}`,
        approvalText(event),
      );
    case "context_compacted":
      return notice(next, `compacted-${event.epoch}`, compactedText(event));
    case "model_call_finished":
      return { ...next, costUsd: next.costUsd + event.cost_usd };
    case "run_finished":
      return {
        ...withItems(
          next,
          next.items.map((i) =>
            i.kind === "text" && i.streaming ? { ...i, streaming: false } : i,
          ),
        ),
        finished: event,
        costUsd: event.cost_usd,
      };
    default:
      return next;
  }
}

function withItems(view: TurnView, items: TurnItem[]): TurnView {
  return { ...view, items };
}

function mapText(
  view: TurnView,
  id: string,
  update: (item: Extract<TurnItem, { kind: "text" }>) => TurnItem,
): TurnView {
  return withItems(
    view,
    view.items.map((i) => (i.kind === "text" && i.id === id ? update(i) : i)),
  );
}

function upsertTool(
  view: TurnView,
  id: string,
  update: (call: ToolCallView) => ToolCallView,
  created?: ToolCallView,
): TurnView {
  const index = view.items.findIndex(
    (i) => i.kind === "tool" && i.call.id === id,
  );
  if (index === -1) {
    return created
      ? withItems(view, [...view.items, { kind: "tool", call: created }])
      : view;
  }
  const items = view.items.slice();
  const item = items[index];
  if (item.kind === "tool")
    items[index] = { kind: "tool", call: update(item.call) };
  return withItems(view, items);
}

function notice(view: TurnView, id: string, text: string): TurnView {
  return withItems(view, [...view.items, { kind: "notice", id, text }]);
}

function readPlan(input: Record<string, unknown>): PlanItem[] | null {
  const items = input.items;
  if (!Array.isArray(items)) return null;
  return items.flatMap((raw: unknown): PlanItem[] => {
    if (typeof raw !== "object" || raw === null) return [];
    const { step, status } = raw as { step?: unknown; status?: unknown };
    if (typeof step !== "string") return [];
    if (
      status !== "pending" &&
      status !== "in_progress" &&
      status !== "completed"
    )
      return [];
    return [{ step, status }];
  });
}

function approvalText(event: ApprovalRequested): string {
  return `Waiting for approval to run ${event.name}: ${event.reason}`;
}

function compactedText(event: ContextCompacted): string {
  return `Context compacted: ${event.messages_summarized} earlier messages were summarized to stay within the model's limit.`;
}
