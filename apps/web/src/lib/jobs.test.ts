import { describe, expect, it } from "vitest";
import {
  describeCondition,
  describeSchedule,
  formatWhen,
  proposedJobId,
} from "@/lib/jobs";

describe("job display", () => {
  it("describes each schedule kind", () => {
    expect(
      describeSchedule({
        kind: "cron",
        expression: "0 8 * * mon-fri",
        timezone: "America/Toronto",
      }),
    ).toBe("cron 0 8 * * mon-fri (America/Toronto)");
    expect(describeSchedule({ kind: "interval", every_seconds: 5400 })).toBe(
      "every 1h 30m",
    );
    expect(
      describeSchedule({ kind: "one_shot", at: "2026-11-18T13:00:00Z" }),
    ).toMatch(/^once, /);
  });

  it("describes conditions", () => {
    expect(describeCondition({ kind: "below", threshold: 1500 })).toBe(
      "below 1500",
    );
    expect(describeCondition({ kind: "contains", text: "In stock" })).toBe(
      "contains “In stock”",
    );
    expect(describeCondition({ kind: "changed", min_delta: 0 })).toBe(
      "changes",
    );
  });

  it("formats times in a given zone", () => {
    expect(formatWhen("2026-11-18T13:00:00Z", "America/Toronto")).toContain(
      "8:00",
    );
    expect(formatWhen(null)).toBe("—");
  });

  it("finds the proposed job id in a tool result", () => {
    const output = "Proposed task job_0123456789abcdef0123: 'Brief', every 1h.";
    expect(proposedJobId(output)).toBe("job_0123456789abcdef0123");
    expect(proposedJobId("Cannot create the job")).toBeNull();
  });
});
