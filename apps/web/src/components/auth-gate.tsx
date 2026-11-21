"use client";

import { usePathname } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { accessToken, CALLBACK_PATH, initAuth, signIn } from "@/lib/auth";

type State = "checking" | "ready" | "redirecting" | "error";

/**
 * Renders the app once the user may use it: immediately when auth is off (local
 * development), after sign-in when it is on. The callback page renders without a
 * session, since it is what creates one. If the sign-in settings cannot be loaded,
 * it shows an error and a retry instead of assuming auth is off.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [state, setState] = useState<State>("checking");
  const [attempt, setAttempt] = useState(0);

  // biome-ignore lint/correctness/useExhaustiveDependencies: attempt re-runs the check
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
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
      } catch {
        if (!cancelled) setState("error");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname, attempt]);

  const retry = useCallback(() => {
    setState("checking");
    setAttempt((n) => n + 1);
  }, []);

  if (state === "ready") return <>{children}</>;
  if (state === "error")
    return (
      <div className="space-y-3 p-6 text-sm">
        <p>Kairos could not be reached to check your sign-in.</p>
        <Button size="sm" onClick={retry}>
          Try again
        </Button>
      </div>
    );
  return (
    <p className="p-6 text-muted-foreground text-sm">
      {state === "redirecting" ? "Signing you in…" : "Loading…"}
    </p>
  );
}
