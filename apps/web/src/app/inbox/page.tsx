"use client";

import { useCallback, useEffect, useState } from "react";
import { listApprovals } from "@/client/sdk.gen";
import type { Approval } from "@/client/types.gen";
import { APPROVALS_CHANGED, ApprovalCard } from "@/components/approval-card";
import { errorMessage } from "@/lib/api";

export default function InboxPage() {
  const [approvals, setApprovals] = useState<Approval[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const { data, error: failure } = await listApprovals();
    if (data) setApprovals(data);
    else setError(errorMessage(failure));
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, 10_000);
    window.addEventListener(APPROVALS_CHANGED, load);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener(APPROVALS_CHANGED, load);
    };
  }, [load]);

  const pending = approvals?.filter((a) => a.status === "pending") ?? [];
  const decided =
    approvals?.filter((a) => a.status !== "pending").slice(0, 20) ?? [];

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 overflow-y-auto px-4 md:px-6 py-6">
      <header>
        <h1 className="font-semibold text-xl">Approval inbox</h1>
        <p className="text-muted-foreground text-sm">
          Every action with a side effect waits here for you. Declining tells
          Kairos, with your note.
        </p>
      </header>
      {error && <p className="text-destructive text-sm">{error}</p>}
      <section className="space-y-3">
        {approvals && pending.length === 0 && (
          <p className="rounded-lg border border-dashed p-8 text-center text-muted-foreground text-sm">
            Nothing is waiting for you.
          </p>
        )}
        {pending.map((approval) => (
          <ApprovalCard
            key={approval.id}
            approval={approval}
            onDecided={() => void load()}
          />
        ))}
      </section>
      {decided.length > 0 && (
        <section className="space-y-2">
          <h2 className="font-medium text-muted-foreground text-sm">
            Recently decided
          </h2>
          {decided.map((approval) => (
            <ApprovalCard key={approval.id} approval={approval} />
          ))}
        </section>
      )}
    </div>
  );
}
