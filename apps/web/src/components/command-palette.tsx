"use client";

import { CornerDownLeft } from "lucide-react";
import { useRouter } from "next/navigation";
import {
  type KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { listThreads } from "@/client/sdk.gen";
import type { Thread } from "@/client/types.gen";
import { OPEN_PALETTE } from "@/components/shell";
import { START_TOUR } from "@/components/tour";
import { useMe } from "@/lib/me";
import { navFor } from "@/lib/nav";
import { type Command, filterCommands } from "@/lib/palette";
import { setTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

const ACTIONS: Command[] = [
  { id: "action:new", label: "New chat", group: "Actions", keywords: "ask" },
  {
    id: "action:theme-light",
    label: "Theme: light",
    group: "Actions",
    keywords: "appearance",
  },
  {
    id: "action:theme-dark",
    label: "Theme: dark",
    group: "Actions",
    keywords: "appearance night",
  },
  {
    id: "action:theme-system",
    label: "Theme: follow system",
    group: "Actions",
    keywords: "appearance auto",
  },
  {
    id: "action:tour",
    label: "Show the tour",
    group: "Actions",
    keywords: "help onboarding",
  },
];

/** ⌘K / Ctrl+K: jump to a page or thread, or run an action. */
export function CommandPalette() {
  const router = useRouter();
  const me = useMe();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const [threads, setThreads] = useState<Thread[]>([]);
  const input = useRef<HTMLInputElement>(null);

  const show = useCallback(() => {
    setQuery("");
    setSelected(0);
    setOpen(true);
    void listThreads().then(({ data }) => data && setThreads(data));
  }, []);

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (open) setOpen(false);
        else show();
      }
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener(OPEN_PALETTE, show);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(OPEN_PALETTE, show);
    };
  }, [open, show]);

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  const commands = useMemo<Command[]>(
    () => [
      ...ACTIONS,
      ...navFor(me.role).map((item) => ({
        id: `page:${item.href}`,
        label: item.label,
        group: "Pages" as const,
        keywords: item.keywords,
      })),
      ...threads.map((thread) => ({
        id: `thread:${thread.id}`,
        label: thread.title,
        group: "Threads" as const,
      })),
    ],
    [threads, me.role],
  );
  const results = useMemo(
    () => filterCommands(commands, query),
    [commands, query],
  );

  const run = (command: Command) => {
    setOpen(false);
    const [kind, value] = [
      command.id.slice(0, command.id.indexOf(":")),
      command.id.slice(command.id.indexOf(":") + 1),
    ];
    if (kind === "page") router.push(value);
    else if (kind === "thread") router.push(`/threads/${value}`);
    else if (value === "new") router.push("/");
    else if (value === "tour") window.dispatchEvent(new Event(START_TOUR));
    else if (value.startsWith("theme-")) {
      const theme = value.slice("theme-".length);
      if (theme === "light" || theme === "dark" || theme === "system")
        setTheme(theme);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") setOpen(false);
    else if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelected((i) => Math.min(i + 1, results.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelected((i) => Math.max(i - 1, 0));
    } else if (event.key === "Enter" && results[selected]) {
      event.preventDefault();
      run(results[selected]);
    }
  };

  if (!open) return null;
  let lastGroup: string | null = null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 px-3 pt-[12vh]">
      <button
        type="button"
        aria-label="Close"
        className="absolute inset-0 cursor-default"
        onClick={() => setOpen(false)}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search and commands"
        className="relative w-full max-w-lg overflow-hidden rounded-xl border bg-popover text-popover-foreground shadow-2xl"
      >
        <input
          ref={input}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setSelected(0);
          }}
          onKeyDown={onKeyDown}
          placeholder="Search pages, threads and actions…"
          aria-label="Search"
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-results"
          aria-activedescendant={
            results[selected] ? `palette-${results[selected].id}` : undefined
          }
          className="w-full border-b bg-transparent px-4 py-3 text-sm outline-none"
        />
        <div
          id="palette-results"
          role="listbox"
          className="max-h-[50vh] overflow-y-auto p-1"
        >
          {results.length === 0 && (
            <p className="px-3 py-6 text-center text-muted-foreground text-sm">
              Nothing matches.
            </p>
          )}
          {results.map((command, index) => {
            const heading = command.group !== lastGroup ? command.group : null;
            lastGroup = command.group;
            return (
              <div key={command.id}>
                {heading && (
                  <div className="px-3 pt-2 pb-1 text-muted-foreground text-xs">
                    {heading}
                  </div>
                )}
                <div
                  id={`palette-${command.id}`}
                  role="option"
                  aria-selected={index === selected}
                  tabIndex={-1}
                  onMouseMove={() => setSelected(index)}
                  onClick={() => run(command)}
                  onKeyDown={() => {}}
                  className={cn(
                    "flex cursor-pointer items-center justify-between truncate rounded-md px-3 py-2 text-sm",
                    index === selected && "bg-muted",
                  )}
                >
                  <span className="truncate">{command.label}</span>
                  {index === selected && (
                    <CornerDownLeft className="size-3.5 shrink-0 text-muted-foreground" />
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
