import { Wallet } from "lucide-react";

/** Today's model spend against the daily cap (Settings page). */
export function SpendSection({
  spent,
  cap,
}: {
  spent: number;
  cap: number | null;
}) {
  const share = cap ? Math.min(1, spent / cap) : 0;
  const reached = cap !== null && spent >= cap;
  return (
    <section
      className={`space-y-2 rounded-lg border p-4 ${reached ? "border-destructive bg-destructive/5" : ""}`}
    >
      <h2 className="flex items-center gap-2 font-medium">
        <Wallet className="size-4" />
        Spend today
      </h2>
      <p className="text-sm">
        ${spent.toFixed(2)}
        {cap !== null
          ? ` of the $${cap.toFixed(2)} daily cap`
          : " (no daily cap)"}
      </p>
      {cap !== null && (
        <div
          className="h-1.5 w-full rounded bg-muted"
          role="meter"
          aria-label="Share of the daily cap spent"
          aria-valuemin={0}
          aria-valuemax={cap}
          aria-valuenow={spent}
        >
          <div
            className={`h-1.5 rounded ${reached ? "bg-destructive" : "bg-primary"}`}
            style={{ width: `${(share * 100).toFixed(1)}%` }}
          />
        </div>
      )}
      <p className="text-muted-foreground text-xs">
        {reached
          ? "The cap is reached: new runs and job firings wait until midnight."
          : "At the cap, runs stop until midnight and you get one notification."}{" "}
        Change it with KAIROS_DAILY_SPEND_CAP_USD in .env.
      </p>
    </section>
  );
}
