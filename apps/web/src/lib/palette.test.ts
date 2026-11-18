import { describe, expect, it } from "vitest";
import { type Command, filterCommands, score } from "@/lib/palette";

const commands: Command[] = [
  { id: "new", label: "New chat", group: "Actions" },
  {
    id: "dark",
    label: "Theme: dark",
    group: "Actions",
    keywords: "appearance night",
  },
  { id: "tasks", label: "Tasks & watches", group: "Pages", keywords: "jobs" },
  { id: "t1", label: "Cheap flights to Lisbon", group: "Threads" },
  { id: "t2", label: "Watch the Lisbon fare", group: "Threads" },
];

describe("score", () => {
  it("ranks a label prefix above a word prefix above any match", () => {
    expect(score(commands[2], "tas")).toBe(3);
    expect(score(commands[4], "fare")).toBe(2);
    expect(score(commands[2], "atch")).toBe(1);
  });

  it("matches keywords and needs every word", () => {
    expect(score(commands[1], "night")).toBe(1);
    expect(score(commands[3], "lisbon cheap")).toBe(2);
    expect(score(commands[3], "lisbon rome")).toBeNull();
  });
});

describe("filterCommands", () => {
  it("returns everything in group order for an empty query", () => {
    expect(filterCommands(commands, "  ").map((c) => c.id)).toEqual([
      "new",
      "dark",
      "tasks",
      "t1",
      "t2",
    ]);
  });

  it("puts the best match first", () => {
    expect(filterCommands(commands, "watch").map((c) => c.id)).toEqual([
      "t2",
      "tasks",
    ]);
  });

  it("respects the limit", () => {
    expect(filterCommands(commands, "", 2)).toHaveLength(2);
  });
});
