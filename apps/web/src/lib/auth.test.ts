import { beforeEach, describe, expect, it, vi } from "vitest";

const authConfig = vi.fn();
vi.mock("@/client/sdk.gen", () => ({ authConfig: () => authConfig() }));

beforeEach(() => {
  vi.resetModules();
  authConfig.mockReset();
});

describe("auth", () => {
  it("points the API client at the API before anything else runs", async () => {
    vi.stubEnv("NEXT_PUBLIC_KAIROS_API_URL", "/api");
    await import("@/lib/auth");
    const { client } = await import("@/client/client.gen");
    expect(client.getConfig().baseUrl).toBe("/api");
    vi.unstubAllEnvs();
  });

  it("fails closed when the sign-in settings cannot be read", async () => {
    authConfig.mockResolvedValue({ data: undefined, error: { detail: "404" } });
    const { initAuth, authEnabled } = await import("@/lib/auth");
    await expect(initAuth()).rejects.toThrow(/sign-in settings/);
    expect(authEnabled()).toBe(false);
    // Nothing was cached: the next attempt asks again.
    authConfig.mockResolvedValue({ data: { enabled: false } });
    await expect(initAuth()).resolves.toEqual({ enabled: false });
    expect(authConfig).toHaveBeenCalledTimes(2);
  });

  it("turns sign-in off only when the API says so", async () => {
    authConfig.mockResolvedValue({ data: { enabled: false } });
    const { initAuth, authEnabled } = await import("@/lib/auth");
    expect(await initAuth()).toEqual({ enabled: false });
    expect(authEnabled()).toBe(false);
  });
});
