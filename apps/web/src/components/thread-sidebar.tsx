"use client";

import {
  LogOut,
  MessageSquare,
  Monitor,
  Moon,
  Plus,
  Search,
  Sun,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { getSettings, listApprovals, listThreads } from "@/client/sdk.gen";
import type { Thread } from "@/client/types.gen";
import { APPROVALS_CHANGED } from "@/components/approval-card";
import { OPEN_PALETTE, useShell } from "@/components/shell";
import { THREADS_CHANGED } from "@/lib/api";
import { authEnabled, signOut } from "@/lib/auth";
import { useMe } from "@/lib/me";
import { isActive, navFor } from "@/lib/nav";
import {
  nextTheme,
  setTheme,
  storedTheme,
  THEME_CHANGED,
  type Theme,
} from "@/lib/theme";
import { cn } from "@/lib/utils";

export function ThreadSidebar() {
  const pathname = usePathname();
  const me = useMe();
  const { drawerOpen } = useShell();
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
    <aside
      className={cn(
        "fixed inset-y-0 left-0 z-40 flex w-64 shrink-0 flex-col border-r bg-sidebar transition-transform md:static md:translate-x-0",
        drawerOpen ? "translate-x-0" : "-translate-x-full",
      )}
    >
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
      <div data-tour="nav" className="space-y-0.5 px-2 pb-2">
        {navFor(me.role)
          .filter((item) => item.href !== "/welcome")
          .map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted",
                isActive(pathname, item.href) && "bg-muted font-medium",
              )}
            >
              <item.icon className="size-3.5 text-muted-foreground" />
              {item.label}
              {item.href === "/inbox" && pending > 0 && (
                <span className="ml-auto rounded-full bg-amber-500 px-1.5 text-white text-xs tabular-nums">
                  {pending}
                </span>
              )}
              {item.href === "/settings" && actionsOff && (
                <span className="ml-auto rounded bg-destructive px-1.5 text-white text-xs">
                  off
                </span>
              )}
            </Link>
          ))}
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
      <div className="flex items-center gap-1 border-t px-2 py-1.5 text-muted-foreground text-xs">
        <button
          type="button"
          onClick={() => window.dispatchEvent(new Event(OPEN_PALETTE))}
          data-tour="search"
          className="flex items-center gap-2 rounded-md px-2 py-1 hover:bg-muted"
        >
          <Search className="size-3.5" />
          Search
          <kbd className="rounded border px-1 font-mono text-[10px]">⌘K</kbd>
        </button>
        <ThemeButton />
        {authEnabled() && (
          <button
            type="button"
            onClick={() => void signOut()}
            aria-label="Sign out"
            title="Sign out"
            className="rounded-md p-1.5 hover:bg-muted"
          >
            <LogOut className="size-3.5" />
          </button>
        )}
      </div>
    </aside>
  );
}

const THEME_ICONS = { light: Sun, dark: Moon, system: Monitor } as const;

/** Cycles light → dark → system. */
function ThemeButton() {
  const [theme, setCurrent] = useState<Theme>("system");
  useEffect(() => {
    const sync = () => setCurrent(storedTheme());
    sync();
    window.addEventListener(THEME_CHANGED, sync);
    return () => window.removeEventListener(THEME_CHANGED, sync);
  }, []);
  const Icon = THEME_ICONS[theme];
  return (
    <button
      type="button"
      onClick={() => setTheme(nextTheme(theme))}
      aria-label={`Theme: ${theme}`}
      title={`Theme: ${theme}`}
      className="ml-auto rounded-md p-1.5 hover:bg-muted"
    >
      <Icon className="size-3.5" />
    </button>
  );
}
