"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { completeSignIn, initAuth } from "@/lib/auth";

/** Where Cognito sends the browser after sign-in: exchange the code, then go on. */
export default function AuthCallback() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        await initAuth();
        router.replace(await completeSignIn());
      } catch (failure) {
        setError(failure instanceof Error ? failure.message : String(failure));
      }
    })();
  }, [router]);

  return (
    <p className="p-6 text-muted-foreground text-sm">
      {error ? `Sign-in failed: ${error}` : "Finishing sign-in…"}
    </p>
  );
}
