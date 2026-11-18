"use client";

import { Menu, Search } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useState,
} from "react";
import { applyTheme, storedTheme } from "@/lib/theme";

export const OPEN_PALETTE = "kairos:open-palette";

type Shell = { drawerOpen: boolean; setDrawerOpen: (open: boolean) => void };

const ShellContext = createContext<Shell>({
  drawerOpen: false,
  setDrawerOpen: () => {},
});

export function useShell(): Shell {
  return useContext(ShellContext);
}

/**
 * Holds the mobile drawer state and keeps the theme in step with the system.
 * On small screens the sidebar is a drawer opened from the top bar; from `md` up it
 * is always visible.
 */
export function ShellProvider({ children }: { children: ReactNode }) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const pathname = usePathname();

  // Navigating closes the drawer.
  // biome-ignore lint/correctness/useExhaustiveDependencies: runs on path change only
  useEffect(() => setDrawerOpen(false), [pathname]);

  // The service worker serves the offline page and push; register it on every visit.
  useEffect(() => {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        // Unsupported context (e.g. a private window): the app works without it.
      });
    }
  }, []);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const follow = () => applyTheme(storedTheme());
    media.addEventListener("change", follow);
    return () => media.removeEventListener("change", follow);
  }, []);

  return (
    <ShellContext value={{ drawerOpen, setDrawerOpen }}>
      {children}
      {drawerOpen && (
        <button
          type="button"
          aria-label="Close menu"
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          onClick={() => setDrawerOpen(false)}
        />
      )}
    </ShellContext>
  );
}

/** The top bar on small screens: menu, name, search. */
export function MobileBar() {
  const { setDrawerOpen } = useShell();
  return (
    <header className="flex shrink-0 items-center gap-2 border-b bg-background px-2 py-2 md:hidden">
      <button
        type="button"
        aria-label="Open menu"
        onClick={() => setDrawerOpen(true)}
        className="rounded-md p-2 hover:bg-muted"
      >
        <Menu className="size-5" />
      </button>
      <Link href="/" className="font-semibold tracking-tight">
        Kairos
      </Link>
      <button
        type="button"
        aria-label="Search and commands"
        onClick={() => window.dispatchEvent(new Event(OPEN_PALETTE))}
        className="ml-auto rounded-md p-2 hover:bg-muted"
      >
        <Search className="size-5" />
      </button>
    </header>
  );
}
