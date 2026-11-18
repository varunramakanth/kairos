"use client";

import {
  CircleAlert,
  CircleCheck,
  CircleHelp,
  Globe,
  Hand,
  Loader,
  Search,
  ShieldAlert,
  Wrench,
} from "lucide-react";
import { useState } from "react";
import { getBlob } from "@/client/sdk.gen";
import "@/lib/api";
import type { ToolCallView } from "@/lib/run-view";

const ICONS: Record<string, typeof Wrench> = {
  web_search: Search,
  web_fetch: Globe,
};

function summary(call: ToolCallView): string {
  const input = call.input ?? {};
  for (const key of ["query", "url", "timezone"]) {
    const value = input[key];
    if (typeof value === "string") return value;
  }
  return call.argsText.length > 80
    ? `${call.argsText.slice(0, 77)}...`
    : call.argsText;
}

function StatusIcon({ status }: { status: ToolCallView["status"] }) {
  switch (status) {
    case "streaming":
    case "running":
      return (
        <Loader
          className="size-4 animate-spin text-muted-foreground"
          aria-label="running"
        />
      );
    case "awaiting_approval":
      return (
        <Hand
          className="size-4 text-amber-600"
          aria-label="waiting for approval"
        />
      );
    case "done":
      return (
        <CircleCheck className="size-4 text-emerald-600" aria-label="done" />
      );
    case "error":
      return (
        <CircleAlert className="size-4 text-destructive" aria-label="error" />
      );
    case "uncertain":
      return (
        <CircleHelp
          className="size-4 text-amber-600"
          aria-label="outcome unknown"
        />
      );
  }
}

/** One tool call: collapsed to a line, expandable to its input and output. */
export function ToolCard({ call }: { call: ToolCallView }) {
  const Icon = ICONS[call.name] ?? Wrench;
  const [full, setFull] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function loadFull() {
    if (!call.blobId) return;
    setLoading(true);
    const { data } = await getBlob({ path: { blob_id: call.blobId } });
    setFull(data?.content ?? null);
    setLoading(false);
  }

  return (
    <details className="group rounded-lg border bg-muted/30 text-sm">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2">
        <Icon className="size-4 shrink-0 text-muted-foreground" />
        <span className="font-medium font-mono text-xs">{call.name}</span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">
          {summary(call)}
        </span>
        {call.provenance === "untrusted" && (
          <span
            className="inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-amber-900 text-xs dark:bg-amber-900/40 dark:text-amber-200"
            title="Content from outside sources. The model treats it as data, never as instructions."
          >
            <ShieldAlert className="size-3" />
            untrusted
          </span>
        )}
        {call.durationMs !== undefined && (
          <span className="text-muted-foreground text-xs tabular-nums">
            {(call.durationMs / 1000).toFixed(1)}s
          </span>
        )}
        <StatusIcon status={call.status} />
      </summary>
      <div className="space-y-2 border-t px-3 py-2">
        <div>
          <div className="mb-1 text-muted-foreground text-xs">Input</div>
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 font-mono text-xs">
            {call.input
              ? JSON.stringify(call.input, null, 2)
              : call.argsText || "…"}
          </pre>
        </div>
        {call.output !== undefined && (
          <div>
            <div className="mb-1 text-muted-foreground text-xs">Output</div>
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 font-mono text-xs">
              {full ?? call.output}
            </pre>
            {call.truncated && full === null && (
              <button
                type="button"
                onClick={loadFull}
                disabled={loading}
                className="mt-1 text-primary text-xs underline-offset-4 hover:underline"
              >
                {loading ? "Loading…" : "Show the full output"}
              </button>
            )}
          </div>
        )}
      </div>
    </details>
  );
}
