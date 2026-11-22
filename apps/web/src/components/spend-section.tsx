import { Wallet } from "lucide-react";

/** Your model spend today against your daily cap (Settings page). ``own`` means the
 * admin gave you a cap of your own; otherwise it is the overall cap (ADR-018). */
export function SpendSection({
  spent,
  cap,
  own = false,
}: {
  spent: number;
  cap: number | null;
  own?: boolean;
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
        <meter
          className="h-2 w-full"
          aria-label="Share of the daily cap spent"
          min={0}
          max={cap}
          high={cap * 0.8}
          value={Math.min(spent, cap)}
        >
          {(share * 100).toFixed(0)}%
        </meter>
      )}
      <p className="text-muted-foreground text-xs">
        {reached
          ? "The cap is reached: new runs and job firings wait until midnight."
          : "At the cap, runs stop until midnight and you get one notification."}{" "}
        {own
          ? "The admin sets your cap on the Users page."
          : "Change it with KAIROS_DAILY_SPEND_CAP_USD in .env."}
      </p>
    </section>
  );
}
