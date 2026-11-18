"use client";

import { useEffect, useState } from "react";
import { runApprovals } from "@/client/sdk.gen";
import type { Approval } from "@/client/types.gen";
import { ApprovalCard } from "@/components/approval-card";

/** Pending approvals of one run, decided right in the chat. */
export function RunApprovals({
  runId,
  onAllDecided,
}: {
  runId: string;
  onAllDecided?: () => void;
}) {
  const [approvals, setApprovals] = useState<Approval[]>([]);

  useEffect(() => {
    void runApprovals({ path: { run_id: runId } }).then(({ data }) => {
      if (data) setApprovals(data.filter((a) => a.status === "pending"));
    });
  }, [runId]);

  function decided(done: Approval) {
    const remaining = approvals.filter((a) => a.id !== done.id);
    setApprovals(remaining);
    if (remaining.length === 0) onAllDecided?.();
  }

  return (
    <div className="space-y-2">
      {approvals.map((approval) => (
        <ApprovalCard
          key={approval.id}
          approval={approval}
          onDecided={decided}
          showRunLink={false}
        />
      ))}
    </div>
  );
}
