import { Suspense } from "react";
import { JobView } from "./job-view";

// The page is a static shell; the view reads the route params and loads its data in
// the browser, inside a Suspense boundary (required with Cache Components).
export default function Page({ params }: PageProps<"/tasks/[jobId]">) {
  return (
    <Suspense fallback={<p className="p-6 text-muted-foreground">Loading…</p>}>
      <JobView params={params} />
    </Suspense>
  );
}
