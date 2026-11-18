"use client";

import { Check, ShieldQuestion, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { decideApproval } from "@/client/sdk.gen";
import type { Approval } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { announceThreadsChanged, errorMessage } from "@/lib/api";
import { formatWhen } from "@/lib/jobs";

export const APPROVALS_CHANGED = "kairos:approvals-changed";

const RISK_LABELS: Record<string, string> = {
  write_self: "changes your data",
  write_external: "acts outside Kairos",
  financial: "involves money",
};

/** One tool call waiting for a decision (or already decided). */
export function ApprovalCard({
  approval,
  onDecided,
  showRunLink = true,
}: {
  approval: Approval;
  onDecided?: (approval: Approval) => void;
  showRunLink?: boolean;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = approval.status === "pending";

  async function decide(approve: boolean) {
    setBusy(true);
    setError(null);
    const { data, error: failure } = await decideApproval({
      path: { approval_id: approval.id },
      body: { approve, note: note.trim() || null },
    });
    setBusy(false);
    if (!data) {
      setError(errorMessage(failure));
      return;
    }
    window.dispatchEvent(new Event(APPROVALS_CHANGED));
    announceThreadsChanged();
    onDecided?.(data);
  }

  return (
    <section
      className={`space-y-2 rounded-lg border px-3 py-2 text-sm ${pending ? "border-amber-400/60 bg-amber-50 dark:bg-amber-950/30" : "bg-muted/30"}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <ShieldQuestion className="size-4 text-amber-600" />
        <span className="font-medium font-mono text-xs">{approval.name}</span>
        <span className="text-muted-foreground text-xs">
          {RISK_LABELS[approval.risk] ?? approval.risk}
        </span>
        <span className="ml-auto text-muted-foreground text-xs">
          {formatWhen(approval.created_at)}
        </span>
      </div>
      <p>{approval.reason}. Kairos wants to run it with:</p>
      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 font-mono text-xs">
        {JSON.stringify(approval.input, null, 2)}
      </pre>
      {pending ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={() => decide(true)} disabled={busy}>
            <Check />
            Approve
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => decide(false)}
            disabled={busy}
          >
            <X />
            Decline
          </Button>
          <input
            aria-label="Note for Kairos"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Optional note (sent to Kairos if you decline)"
            className="min-w-48 flex-1 rounded-md border bg-background px-2 py-1 text-xs outline-none"
          />
        </div>
      ) : (
        <p className="text-muted-foreground text-xs">
          {approval.status === "approved" ? "Approved" : "Declined"}{" "}
          {formatWhen(approval.decided_at)}
          {approval.note ? ` · “${approval.note}”` : ""}
        </p>
      )}
      {showRunLink && (
        <Link
          href={`/runs/${approval.run_id}`}
          className="text-primary text-xs underline-offset-4 hover:underline"
        >
          View the run
        </Link>
      )}
      {error && <p className="text-destructive text-xs">{error}</p>}
    </section>
  );
}
