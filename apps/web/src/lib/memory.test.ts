import { describe, expect, it } from "vitest";
import type { Memory } from "@/client/types.gen";
import { active, groupByKind, proposals } from "@/lib/memory";

function memory(
  id: string,
  kind: Memory["kind"],
  status: Memory["status"] = "active",
): Memory {
  return {
    id,
    kind,
    content: id,
    status,
    source: "user",
    run_id: null,
    created_at: "2026-11-18T00:00:00Z",
    updated_at: "2026-11-18T00:00:00Z",
  };
}

describe("memory page", () => {
  const all = [
    memory("a", "preference"),
    memory("b", "profile"),
    memory("c", "task", "proposed"),
    memory("d", "preference"),
  ];

  it("separates proposals from confirmed memories", () => {
    expect(proposals(all).map((m) => m.id)).toEqual(["c"]);
    expect(active(all).map((m) => m.id)).toEqual(["a", "b", "d"]);
  });

  it("groups confirmed memories by kind in a fixed order", () => {
    expect(
      groupByKind(active(all)).map(([kind, items]) => [
        kind,
        items.map((m) => m.id),
      ]),
    ).toEqual([
      ["profile", ["b"]],
      ["preference", ["a", "d"]],
    ]);
  });
});
