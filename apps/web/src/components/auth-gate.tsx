"use client";

import { usePathname } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";
import { accessToken, CALLBACK_PATH, initAuth, signIn } from "@/lib/auth";

type State = "checking" | "ready" | "redirecting";

/**
 * Renders the app once the user may use it: immediately when auth is off (local
 * development), after sign-in when it is on. The callback page renders without a
 * session, since it is what creates one.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [state, setState] = useState<State>("checking");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const config = await initAuth();
      if (cancelled) return;
      if (!config.enabled || pathname === CALLBACK_PATH) {
        setState("ready");
        return;
      }
      if (await accessToken()) {
        setState("ready");
      } else {
        setState("redirecting");
        await signIn();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  if (state === "ready") return <>{children}</>;
  return (
    <p className="p-6 text-muted-foreground text-sm">
      {state === "redirecting" ? "Signing you in…" : "Loading…"}
    </p>
  );
}
