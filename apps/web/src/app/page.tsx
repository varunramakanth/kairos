"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { createThread } from "@/client/sdk.gen";
import { Composer } from "@/components/composer";
import { announceThreadsChanged, errorMessage } from "@/lib/api";
import { EXAMPLES } from "@/lib/examples";

export default function NewChat() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  async function start(text: string): Promise<boolean> {
    setError(null);
    const { data, error: failure } = await createThread({ body: { text } });
    if (!data) {
      setError(errorMessage(failure));
      return false;
    }
    announceThreadsChanged();
    router.push(`/threads/${data.thread_id}`);
    return true;
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center gap-6 px-4 py-10 md:py-16">
      <header className="space-y-1">
        <h1 className="font-semibold text-3xl tracking-tight">Kairos</h1>
        <p className="text-muted-foreground">
          Your always-on agent. Ask a question to start a thread.
        </p>
      </header>
      <Composer onSend={start} />
      {error && <p className="text-destructive text-sm">{error}</p>}
      <ul className="flex flex-wrap gap-2">
        {EXAMPLES.map((example) => (
          <li key={example.prompt}>
            <button
              type="button"
              onClick={() => void start(example.prompt)}
              title={example.prompt}
              className="rounded-full border px-3 py-1 text-left text-muted-foreground text-sm hover:bg-muted hover:text-foreground"
            >
              {example.shows}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
