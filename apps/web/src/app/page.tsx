import { Button } from "@/components/ui/button";

const milestones = [
  { id: "M0", name: "Scaffold", state: "current" },
  { id: "M1", name: "Harness core", state: "next" },
  { id: "M2", name: "API + chat UI", state: "later" },
] as const;

export default function Home() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-xl flex-col justify-center gap-8 px-4 py-16">
      <header className="space-y-2">
        <p className="text-muted-foreground text-sm">
          Always-on personal agent
        </p>
        <h1 className="font-semibold text-3xl tracking-tight">Kairos</h1>
        <p className="text-muted-foreground">
          The web UI arrives in M2. This page only confirms the toolchain works.
        </p>
      </header>
      <ul className="divide-y rounded-lg border">
        {milestones.map((m) => (
          <li
            key={m.id}
            className="flex items-center justify-between px-4 py-3"
          >
            <span>
              <span className="font-mono text-muted-foreground text-sm">
                {m.id}
              </span>{" "}
              {m.name}
            </span>
            <span className="text-muted-foreground text-sm">{m.state}</span>
          </li>
        ))}
      </ul>
      <div>
        <Button
          variant="outline"
          nativeButton={false}
          render={
            <a href="https://github.com/CopilotKit/openfuse">
              Reference: OpenFuse
            </a>
          }
        />
      </div>
    </main>
  );
}
