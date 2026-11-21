import { describe, expect, it } from "vitest";
import { navFor } from "@/lib/nav";
import { capLabel, parseCap } from "@/lib/users";

describe("caps", () => {
  it("reads blank as no cap and rejects nonsense", () => {
    expect(parseCap("")).toBeNull();
    expect(parseCap(" 2.5 ")).toBe(2.5);
    expect(parseCap("0")).toBe(0);
    expect(parseCap("-1")).toBeNull();
    expect(parseCap("lots")).toBeNull();
  });

  it("describes a cap", () => {
    expect(capLabel(null)).toBe("no cap of their own");
    expect(capLabel(2)).toBe("$2.00 a day");
  });
});

describe("navigation by role", () => {
  it("hides admin pages from members", () => {
    const member = navFor("member").map((item) => item.href);
    expect(member).not.toContain("/users");
    expect(member).not.toContain("/evals");
    expect(member).toContain("/memory");
    const admin = navFor("admin").map((item) => item.href);
    expect(admin).toContain("/users");
    expect(admin).toContain("/evals");
  });
});
