"use client";

import { Check, Pencil, Trash2, X } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  createMemory,
  deleteMemory,
  listMemories,
  updateMemory,
} from "@/client/sdk.gen";
import type { Memory, MemoryKind } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { formatWhen } from "@/lib/jobs";
import {
  active,
  groupByKind,
  KIND_LABELS,
  KINDS,
  proposals,
  SOURCE_LABELS,
} from "@/lib/memory";

export default function MemoryPage() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Memory[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const { data, error: failure } = await listMemories({
      query: { limit: 500 },
    });
    if (data) setMemories(data);
    else setError(errorMessage(failure));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(run: () => Promise<{ error?: unknown }>) {
    const { error: failure } = await run();
    if (failure) setError(errorMessage(failure));
    else {
      setError(null);
      setResults(null);
      await load();
    }
  }

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) {
      setResults(null);
      return;
    }
    const { data, error: failure } = await listMemories({
      query: { q: query.trim() },
    });
    if (data) setResults(data);
    else setError(errorMessage(failure));
  }

  const pending = proposals(memories);
  const shown = results ?? active(memories);

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 overflow-y-auto px-4 md:px-6 py-6">
      <header>
        <h1 className="font-semibold text-xl">Memory</h1>
        <p className="text-muted-foreground text-sm">
          What Kairos remembers about you across conversations. Kairos can only
          suggest memories; nothing it suggests is used until you confirm it.
        </p>
      </header>
      {error && <p className="text-destructive text-sm">{error}</p>}

      {pending.length > 0 && (
        <section className="space-y-2 rounded-lg border border-primary/30 bg-primary/5 p-4">
          <h2 className="font-medium">
            Suggested by Kairos ({pending.length})
          </h2>
          <ul className="space-y-2">
            {pending.map((m) => (
              <li key={m.id} className="flex items-start gap-2">
                <div className="flex-1">
                  <p className="text-sm">{m.content}</p>
                  <p className="text-muted-foreground text-xs">
                    {KIND_LABELS[m.kind]} · from {SOURCE_LABELS[m.source]} ·{" "}
                    {formatWhen(m.created_at)}
                  </p>
                </div>
                <Button
                  size="sm"
                  onClick={() =>
                    act(() =>
                      updateMemory({
                        path: { memory_id: m.id },
                        body: { status: "active" },
                      }),
                    )
                  }
                >
                  <Check className="size-3.5" /> Remember
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    act(() => deleteMemory({ path: { memory_id: m.id } }))
                  }
                >
                  <X className="size-3.5" /> Discard
                </Button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <AddMemory
        onAdd={(kind, content) =>
          act(() => createMemory({ body: { kind, content } }))
        }
      />

      <form onSubmit={search} className="flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search memories"
          aria-label="Search memories"
          className="flex-1 rounded-md border bg-background px-3 py-1.5 text-sm"
        />
        <Button type="submit" size="sm" variant="outline">
          Search
        </Button>
      </form>

      {shown.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          {results ? "Nothing matches." : "No memories yet."}
        </p>
      ) : (
        groupByKind(shown).map(([kind, items]) => (
          <section key={kind} className="space-y-1">
            <h2 className="text-muted-foreground text-xs">
              {KIND_LABELS[kind]}
            </h2>
            <ul className="divide-y rounded-lg border">
              {items.map((m) => (
                <MemoryRow
                  key={m.id}
                  memory={m}
                  onSave={(content) =>
                    act(() =>
                      updateMemory({
                        path: { memory_id: m.id },
                        body: { content },
                      }),
                    )
                  }
                  onDelete={() =>
                    act(() => deleteMemory({ path: { memory_id: m.id } }))
                  }
                />
              ))}
            </ul>
          </section>
        ))
      )}
    </div>
  );
}

function AddMemory({
  onAdd,
}: {
  onAdd: (kind: MemoryKind, content: string) => Promise<void>;
}) {
  const [kind, setKind] = useState<MemoryKind>("preference");
  const [content, setContent] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!content.trim()) return;
    await onAdd(kind, content.trim());
    setContent("");
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap gap-2">
      <select
        value={kind}
        onChange={(e) => setKind(e.target.value as MemoryKind)}
        aria-label="Kind"
        className="rounded-md border bg-background px-2 py-1.5 text-sm"
      >
        {KINDS.map((k) => (
          <option key={k} value={k}>
            {KIND_LABELS[k]}
          </option>
        ))}
      </select>
      <input
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder="Add something Kairos should know, e.g. I prefer aisle seats"
        aria-label="New memory"
        maxLength={1000}
        className="min-w-64 flex-1 rounded-md border bg-background px-3 py-1.5 text-sm"
      />
      <Button type="submit" size="sm">
        Add
      </Button>
    </form>
  );
}

function MemoryRow({
  memory,
  onSave,
  onDelete,
}: {
  memory: Memory;
  onSave: (content: string) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(memory.content);

  return (
    <li className="flex items-start gap-2 px-3 py-2">
      {editing ? (
        <form
          className="flex flex-1 gap-2"
          onSubmit={async (e) => {
            e.preventDefault();
            await onSave(draft.trim());
            setEditing(false);
          }}
        >
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="Edit memory"
            maxLength={1000}
            className="flex-1 rounded-md border bg-background px-2 py-1 text-sm"
          />
          <Button type="submit" size="sm">
            Save
          </Button>
        </form>
      ) : (
        <div className="flex-1">
          <p className="text-sm">{memory.content}</p>
          <p className="text-muted-foreground text-xs">
            from {SOURCE_LABELS[memory.source]} ·{" "}
            {formatWhen(memory.updated_at)}
          </p>
        </div>
      )}
      <Button
        size="sm"
        variant="ghost"
        aria-label="Edit"
        onClick={() => setEditing((v) => !v)}
      >
        <Pencil className="size-3.5" />
      </Button>
      <Button size="sm" variant="ghost" aria-label="Delete" onClick={onDelete}>
        <Trash2 className="size-3.5" />
      </Button>
    </li>
  );
}
