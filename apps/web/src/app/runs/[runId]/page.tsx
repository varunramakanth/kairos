import { Suspense } from "react";
import { RunTrace } from "./run-trace";

// The page is a static shell; the view reads the route params and loads its data in
// the browser, inside a Suspense boundary (required with Cache Components).
export default function Page({ params }: PageProps<"/runs/[runId]">) {
  return (
    <Suspense fallback={<p className="p-6 text-muted-foreground">Loading…</p>}>
      <RunTrace params={params} />
    </Suspense>
  );
}
