/** Matching and ranking for the command palette. Pure, so it is unit-tested. */

export type Command = {
  id: string;
  label: string;
  group: "Actions" | "Pages" | "Threads";
  keywords?: string;
};

/**
 * Score how well `query` matches a command; null when it does not.
 *
 * Every query word must appear in the label or keywords. A label that starts with
 * the query ranks first, then labels where every query word starts a word (in any
 * order), then any other match.
 */
export function score(command: Command, query: string): number | null {
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  const label = command.label.toLowerCase();
  const haystack = `${label} ${command.keywords?.toLowerCase() ?? ""}`;
  const words = q.split(/\s+/);
  if (!words.every((word) => haystack.includes(word))) return null;
  if (label.startsWith(q)) return 3;
  const labelWords = label.split(/[^a-z0-9]+/);
  if (words.every((word) => labelWords.some((lw) => lw.startsWith(word))))
    return 2;
  return 1;
}

const GROUP_ORDER: Record<Command["group"], number> = {
  Actions: 0,
  Pages: 1,
  Threads: 2,
};

/** Matching commands, best first; ties keep group order, then the given order. */
export function filterCommands(
  commands: readonly Command[],
  query: string,
  limit = 30,
): Command[] {
  return commands
    .map((command, index) => ({ command, index, s: score(command, query) }))
    .filter((entry): entry is typeof entry & { s: number } => entry.s !== null)
    .sort(
      (a, b) =>
        b.s - a.s ||
        GROUP_ORDER[a.command.group] - GROUP_ORDER[b.command.group] ||
        a.index - b.index,
    )
    .slice(0, limit)
    .map((entry) => entry.command);
}
