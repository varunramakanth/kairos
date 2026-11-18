"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { listAudit } from "@/client/sdk.gen";
import type { AuditEntry } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { formatWhen } from "@/lib/jobs";

const ACTOR_STYLE: Record<string, string> = {
  agent: "bg-primary/10 text-primary",
  user: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  system: "bg-muted text-muted-foreground",
};

export default function AuditPage() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [more, setMore] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (before?: number) => {
    const { data, error: failure } = await listAudit({
      query: { before, limit: 100 },
    });
    if (!data) {
      setError(errorMessage(failure));
      return;
    }
    setEntries((current) => (before ? [...current, ...data] : data));
    setMore(data.length === 100);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="mx-auto w-full max-w-5xl space-y-4 overflow-y-auto px-6 py-6">
      <header>
        <h1 className="font-semibold text-xl">Audit log</h1>
        <p className="text-muted-foreground text-sm">
          Every action with a side effect, every decision you made, and every
          notification sent. Append-only.
        </p>
      </header>
      {error && <p className="text-destructive text-sm">{error}</p>}
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-left text-muted-foreground text-xs">
            <tr>
              <th className="px-3 py-2 font-normal">When</th>
              <th className="px-3 py-2 font-normal">Who</th>
              <th className="px-3 py-2 font-normal">Action</th>
              <th className="px-3 py-2 font-normal">Subject</th>
              <th className="px-3 py-2 font-normal">Detail</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {entries.map((entry) => (
              <tr key={entry.id}>
                <td className="whitespace-nowrap px-3 py-1.5 text-xs">
                  {formatWhen(entry.at)}
                </td>
                <td className="px-3 py-1.5">
                  <span
                    className={`rounded px-1.5 py-0.5 text-xs ${ACTOR_STYLE[entry.actor] ?? ""}`}
                  >
                    {entry.actor}
                  </span>
                </td>
                <td className="px-3 py-1.5 font-mono text-xs">
                  {entry.action}
                </td>
                <td className="px-3 py-1.5 text-xs">
                  {entry.run_id ? (
                    <Link
                      href={`/runs/${entry.run_id}`}
                      className="underline-offset-4 hover:underline"
                    >
                      {entry.subject}
                    </Link>
                  ) : (
                    entry.subject
                  )}
                </td>
                <td
                  className="max-w-md truncate px-3 py-1.5 font-mono text-muted-foreground text-xs"
                  title={JSON.stringify(entry.detail)}
                >
                  {JSON.stringify(entry.detail)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {more && entries.length > 0 && (
        <Button
          variant="outline"
          onClick={() => load(entries[entries.length - 1].id)}
        >
          Older
        </Button>
      )}
    </div>
  );
}
