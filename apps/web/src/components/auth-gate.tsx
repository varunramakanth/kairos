"use client";

import { usePathname } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { getMe } from "@/client/sdk.gen";
import type { Me } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import {
  accessToken,
  CALLBACK_PATH,
  initAuth,
  signIn,
  signOut,
} from "@/lib/auth";
import { MeProvider } from "@/lib/me";

type State = "checking" | "ready" | "redirecting" | "not-invited" | "error";

/** Who is signed in; ``null`` with a 403 means a valid sign-in with no invite. */
async function loadMe(): Promise<Me | "not-invited"> {
  const { data, response } = await getMe();
  if (data) return data;
  if (response?.status === 403) return "not-invited";
  throw new Error("Could not load your account from Kairos.");
}

/**
 * Renders the app once the user may use it: immediately when auth is off (local
 * development), after sign-in when it is on. The callback page renders without a
 * session, since it is what creates one. If the sign-in settings cannot be loaded,
 * it shows an error and a retry instead of assuming auth is off. A valid sign-in that
 * nobody invited sees a "not invited" screen (ADR-018).
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [state, setState] = useState<State>("checking");
  const [attempt, setAttempt] = useState(0);
  const [me, setMe] = useState<Me | null>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: attempt re-runs the check
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const config = await initAuth();
        if (cancelled) return;
        if (pathname === CALLBACK_PATH) {
          setState("ready");
          return;
        }
        if (!config.enabled || (await accessToken())) {
          const who = await loadMe();
          if (cancelled) return;
          if (who === "not-invited") {
            setState("not-invited");
          } else {
            setMe(who);
            setState("ready");
          }
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

  if (state === "ready")
    return me ? <MeProvider me={me}>{children}</MeProvider> : children;
  if (state === "not-invited")
    return (
      <div className="space-y-3 p-6 text-sm">
        <p className="font-medium">You are signed in, but not invited.</p>
        <p className="text-muted-foreground">
          Ask the owner of this Kairos to invite you, or sign in with another
          account.
        </p>
        <Button size="sm" variant="outline" onClick={() => void signOut()}>
          Sign out
        </Button>
      </div>
    );
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
