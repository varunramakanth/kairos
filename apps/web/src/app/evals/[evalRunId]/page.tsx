import { Suspense } from "react";
import { EvalRunView } from "./eval-run-view";

// A static shell; the view reads the route params and loads its data in the
// browser, inside a Suspense boundary (required with Cache Components).
export default function Page({ params }: PageProps<"/evals/[evalRunId]">) {
  return (
    <Suspense fallback={<p className="p-6 text-muted-foreground">Loading…</p>}>
      <EvalRunView params={params} />
    </Suspense>
  );
}
