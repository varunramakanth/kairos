"use client";

import { CircleAlert, CircleCheck, CircleDashed } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { createThread, getSetupStatus } from "@/client/sdk.gen";
import type { SetupStatus } from "@/client/types.gen";
import { START_TOUR } from "@/components/tour";
import { Button } from "@/components/ui/button";
import { announceThreadsChanged, errorMessage } from "@/lib/api";
import { EXAMPLES } from "@/lib/examples";

/** Setup checklist, example prompts and the tour: where a new install starts. */
export default function WelcomePage() {
  const router = useRouter();
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void getSetupStatus().then(({ data, error: failure }) => {
      if (data) setStatus(data);
      else setError(errorMessage(failure));
    });
  }, []);

  async function start(text: string) {
    const { data, error: failure } = await createThread({ body: { text } });
    if (!data) {
      setError(errorMessage(failure));
      return;
    }
    announceThreadsChanged();
    router.push(`/threads/${data.thread_id}`);
  }

  const ready = status?.items.filter((item) => item.ok).length ?? 0;
  return (
    <div className="mx-auto w-full max-w-2xl space-y-8 px-4 py-8 md:py-12">
      <header className="space-y-1">
        <h1 className="font-semibold text-2xl tracking-tight">
          Welcome to Kairos
        </h1>
        <p className="text-muted-foreground">
          An agent that works while you are away: it answers, watches and
          reminds, and asks before it does anything on your behalf.
        </p>
        {status?.demo && (
          <p className="rounded-md bg-muted px-3 py-2 text-sm">
            Demo mode: answers are scripted and free. Try the examples below.
          </p>
        )}
      </header>

      <section aria-labelledby="setup" className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2 id="setup" className="font-medium">
            Setup
          </h2>
          {status && (
            <span className="text-muted-foreground text-sm tabular-nums">
              {ready} of {status.items.length} on
            </span>
          )}
        </div>
        {error && <p className="text-destructive text-sm">{error}</p>}
        {!status && !error && (
          <p className="text-muted-foreground text-sm">Checking…</p>
        )}
        <ul className="divide-y rounded-lg border">
          {status?.items.map((item) => {
            const Icon = item.ok
              ? CircleCheck
              : item.required
                ? CircleAlert
                : CircleDashed;
            return (
              <li key={item.id} className="flex gap-3 px-3 py-2.5">
                <Icon
                  aria-hidden
                  className={
                    item.ok
                      ? "mt-0.5 size-4 shrink-0 text-emerald-600"
                      : item.required
                        ? "mt-0.5 size-4 shrink-0 text-destructive"
                        : "mt-0.5 size-4 shrink-0 text-muted-foreground"
                  }
                />
                <div className="min-w-0">
                  <div className="font-medium text-sm">
                    {item.label}
                    <span className="sr-only">
                      {item.ok
                        ? " (on)"
                        : item.required
                          ? " (needed)"
                          : " (off)"}
                    </span>
                  </div>
                  <p className="text-muted-foreground text-sm">{item.detail}</p>
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      <section aria-labelledby="try" className="space-y-3">
        <h2 id="try" className="font-medium">
          Try it
        </h2>
        <ul className="grid gap-2 sm:grid-cols-2">
          {EXAMPLES.map((example) => (
            <li key={example.prompt}>
              <button
                type="button"
                onClick={() => void start(example.prompt)}
                className="h-full w-full rounded-lg border p-3 text-left hover:bg-muted"
              >
                <div className="text-muted-foreground text-xs">
                  {example.shows}
                </div>
                <div className="mt-1 text-sm">{example.prompt}</div>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          onClick={() => window.dispatchEvent(new Event(START_TOUR))}
        >
          Show the tour again
        </Button>
        <Button variant="ghost" onClick={() => router.push("/")}>
          Start a chat
        </Button>
      </div>
    </div>
  );
}
