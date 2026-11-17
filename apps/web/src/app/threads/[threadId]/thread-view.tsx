"use client";

import { use, useCallback, useEffect, useRef, useState } from "react";
import { getThread, sendMessage } from "@/client/sdk.gen";
import type { ThreadDetail } from "@/client/types.gen";
import { Composer } from "@/components/composer";
import { Turn } from "@/components/turn";
import { announceThreadsChanged, errorMessage } from "@/lib/api";

export function ThreadView({
  params,
}: {
  params: Promise<{ threadId: string }>;
}) {
  const { threadId } = use(params);
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    const { data, error: failure } = await getThread({
      path: { thread_id: threadId },
    });
    if (data) setThread(data);
    else setError(errorMessage(failure));
  }, [threadId]);

  useEffect(() => {
    void load();
  }, [load]);

  const turns = thread?.runs.length ?? 0;
  useEffect(() => {
    if (turns > 0) bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const last = thread?.runs.at(-1)?.run;
  const busy =
    last !== undefined &&
    (last.status === "queued" || last.status === "running");

  async function send(text: string): Promise<boolean> {
    setError(null);
    const { data, error: failure } = await sendMessage({
      path: { thread_id: threadId },
      body: { text },
    });
    if (!data) {
      setError(errorMessage(failure));
      return false;
    }
    await load();
    announceThreadsChanged();
    return true;
  }

  return (
    <div className="flex h-full flex-col">
      <header className="border-b px-6 py-3">
        <h1 className="truncate font-medium">{thread?.thread.title ?? "…"}</h1>
      </header>
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-8 px-4 py-6">
          {thread?.runs.map((detail) => (
            <Turn key={detail.run.id} detail={detail} onFinished={load} />
          ))}
          <div ref={bottom} />
        </div>
      </div>
      <div className="mx-auto w-full max-w-3xl px-4 pb-4">
        {error && <p className="mb-2 text-destructive text-sm">{error}</p>}
        <Composer
          onSend={send}
          disabled={busy || thread === null}
          placeholder={busy ? "Kairos is working…" : "Reply…"}
        />
      </div>
    </div>
  );
}
