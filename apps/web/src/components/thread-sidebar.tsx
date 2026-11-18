"use client";

import {
  CalendarClock,
  FlaskConical,
  Inbox,
  MessageSquare,
  Plus,
  ScrollText,
  Settings,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { getSettings, listApprovals, listThreads } from "@/client/sdk.gen";
import type { Thread } from "@/client/types.gen";
import { APPROVALS_CHANGED } from "@/components/approval-card";
import { THREADS_CHANGED } from "@/lib/api";
import { cn } from "@/lib/utils";

export function ThreadSidebar() {
  const pathname = usePathname();
  const [threads, setThreads] = useState<Thread[]>([]);
  const [offline, setOffline] = useState(false);
  const [pending, setPending] = useState(0);
  const [actionsOff, setActionsOff] = useState(false);

  const refresh = useCallback(async () => {
    const [threadList, approvals, settings] = await Promise.all([
      listThreads(),
      listApprovals({ query: { status: "pending" } }),
      getSettings(),
    ]);
    setOffline(Boolean(threadList.error) || threadList.data === undefined);
    if (threadList.data) setThreads(threadList.data);
    if (approvals.data) setPending(approvals.data.length);
    if (settings.data) setActionsOff(!settings.data.actions_enabled);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(refresh, 15_000);
    window.addEventListener(THREADS_CHANGED, refresh);
    window.addEventListener(APPROVALS_CHANGED, refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener(THREADS_CHANGED, refresh);
      window.removeEventListener(APPROVALS_CHANGED, refresh);
    };
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
      <div className="space-y-0.5 px-2 pb-2">
        <Link
          href="/inbox"
          className={cn(
            "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted",
            pathname.startsWith("/inbox") && "bg-muted font-medium",
          )}
        >
          <Inbox className="size-3.5 text-muted-foreground" />
          Approvals
          {pending > 0 && (
            <span className="ml-auto rounded-full bg-amber-500 px-1.5 text-white text-xs tabular-nums">
              {pending}
            </span>
          )}
        </Link>
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
        <Link
          href="/evals"
          className={cn(
            "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted",
            pathname.startsWith("/evals") && "bg-muted font-medium",
          )}
        >
          <FlaskConical className="size-3.5 text-muted-foreground" />
          Evals
        </Link>
        <Link
          href="/audit"
          className={cn(
            "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted",
            pathname.startsWith("/audit") && "bg-muted font-medium",
          )}
        >
          <ScrollText className="size-3.5 text-muted-foreground" />
          Audit log
        </Link>
        <Link
          href="/settings"
          className={cn(
            "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted",
            pathname.startsWith("/settings") && "bg-muted font-medium",
          )}
        >
          <Settings className="size-3.5 text-muted-foreground" />
          Settings
          {actionsOff && (
            <span className="ml-auto rounded bg-destructive px-1.5 text-white text-xs">
              off
            </span>
          )}
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
