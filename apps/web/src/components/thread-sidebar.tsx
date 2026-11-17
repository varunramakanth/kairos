"use client";

import { CalendarClock, MessageSquare, Plus } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { listThreads } from "@/client/sdk.gen";
import type { Thread } from "@/client/types.gen";
import { THREADS_CHANGED } from "@/lib/api";
import { cn } from "@/lib/utils";

export function ThreadSidebar() {
  const pathname = usePathname();
  const [threads, setThreads] = useState<Thread[]>([]);
  const [offline, setOffline] = useState(false);

  const refresh = useCallback(async () => {
    const { data, error } = await listThreads();
    setOffline(Boolean(error) || data === undefined);
    if (data) setThreads(data);
  }, []);

  useEffect(() => {
    void refresh();
    window.addEventListener(THREADS_CHANGED, refresh);
    return () => window.removeEventListener(THREADS_CHANGED, refresh);
  }, [refresh]);

  return (
    <aside className="hidden w-64 shrink-0 flex-col border-r bg-sidebar md:flex">
      <div className="flex items-center justify-between px-4 py-3">
        <Link href="/" className="font-semibold tracking-tight">
          Kairos
        </Link>
        <Link
          href="/"
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-muted-foreground text-sm hover:bg-muted hover:text-foreground"
        >
          <Plus className="size-4" />
          New
        </Link>
      </div>
      <div className="px-2 pb-2">
        <Link
          href="/tasks"
          className={cn(
            "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted",
            pathname.startsWith("/tasks") && "bg-muted font-medium",
          )}
        >
          <CalendarClock className="size-3.5 text-muted-foreground" />
          Tasks &amp; watches
        </Link>
      </div>
      <div className="px-4 pb-1 text-muted-foreground text-xs">Threads</div>
      <nav className="flex-1 overflow-y-auto px-2 pb-4" aria-label="Threads">
        {offline && (
          <p className="px-2 py-1 text-destructive text-xs">
            Cannot reach the API.
          </p>
        )}
        {threads.map((thread) => {
          const href = `/threads/${thread.id}`;
          return (
            <Link
              key={thread.id}
              href={href}
              className={cn(
                "flex items-center gap-2 truncate rounded-md px-2 py-1.5 text-sm hover:bg-muted",
                pathname === href && "bg-muted font-medium",
              )}
            >
              <MessageSquare className="size-3.5 shrink-0 text-muted-foreground" />
              <span className="truncate">{thread.title}</span>
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
