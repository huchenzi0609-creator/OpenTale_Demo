import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, request } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("safe browser API boundary", () => {
  it("never exposes raw fetch exceptions", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("raw-private-body-and-header")),
    );
    await expect(request("/runs")).rejects.toMatchObject({
      code: "CONNECTION_FAILED",
      retryable: true,
    });
    await request("/runs").catch((error) =>
      expect(error.message).not.toContain("raw-private"),
    );
  });

  it("never renders a non-JSON response body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => {
          throw new Error("raw-server-body");
        },
      }),
    );
    await expect(request("/runs")).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });

  it("retains the safe structured server error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => ({
          error: {
            code: "BUDGET_EXHAUSTED",
            message: "本次预算已用尽。",
            retryable: false,
          },
        }),
      }),
    );
    try {
      await request("/runs");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({
        code: "BUDGET_EXHAUSTED",
        message: "本次预算已用尽。",
        retryable: false,
      });
    }
  });
});
