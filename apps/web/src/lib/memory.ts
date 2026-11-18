/** Memory page helpers. Pure functions, unit-tested. */
import type { Memory, MemoryKind } from "@/client/types.gen";

export const KINDS: readonly MemoryKind[] = [
  "profile",
  "preference",
  "task",
  "episodic",
];

export const KIND_LABELS: Record<MemoryKind, string> = {
  profile: "About you",
  preference: "Preferences",
  task: "Ongoing goals",
  episodic: "Things that happened",
};

export const SOURCE_LABELS: Record<Memory["source"], string> = {
  user: "you",
  agent: "Kairos, during a chat",
  reflection: "Kairos, after a chat",
};

/** Active memories grouped by kind, in a fixed order, empty groups dropped. */
export function groupByKind(
  memories: readonly Memory[],
): [MemoryKind, Memory[]][] {
  return KINDS.map(
    (kind) =>
      [kind, memories.filter((m) => m.kind === kind)] as [MemoryKind, Memory[]],
  ).filter(([, items]) => items.length > 0);
}

export function proposals(memories: readonly Memory[]): Memory[] {
  return memories.filter((m) => m.status === "proposed");
}

export function active(memories: readonly Memory[]): Memory[] {
  return memories.filter((m) => m.status === "active");
}
