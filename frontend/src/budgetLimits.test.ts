import { describe, expect, it } from "vitest";
import { DEFAULT_LIMITS, budgetBounds, budgetExhausted, budgetText, limitsValid } from "./budgetLimits";
import type { Limits, PublicConfig } from "./types";

const unlimited: Limits = { max_calls: null, max_tokens: null, max_parallel: null, max_windows: null };
const config = (bounds: Limits): PublicConfig => ({ provider: "deepseek", configured: false,
  model: "fixture", timeout_seconds: 90, max_output_tokens: 1600, budget_bounds: bounds });

describe("saved budgets and unbounded service capabilities", () => {
  it("keeps saved defaults finite without restoring former service ceilings", () => {
    expect(DEFAULT_LIMITS.max_tokens).toBe(128000);
    expect(budgetBounds(null)).toEqual(unlimited);
    expect(budgetBounds(config(unlimited))).toEqual(unlimited);
    expect(limitsValid({ max_calls: 100000, max_tokens: 512000000,
      max_parallel: 1000, max_windows: 100000 }, unlimited)).toBe(true);
  });

  it("accepts explicit unlimited budgets independently of prior usage", () => {
    expect(limitsValid(unlimited, unlimited, { max_calls: 200, max_tokens: 6000000,
      max_parallel: 1, max_windows: 2000 })).toBe(true);
    expect(budgetExhausted(100000000, null)).toBe(false);
    expect(budgetText(null)).toBe("不设限");
  });

  it("preserves finite occupancy enforcement including exact-bound exhaustion", () => {
    expect(limitsValid({ ...DEFAULT_LIMITS, max_tokens: 500 }, unlimited,
      { max_calls: 1, max_tokens: 650, max_parallel: 1, max_windows: 1 })).toBe(false);
    expect(budgetExhausted(650, 650)).toBe(true);
    expect(budgetExhausted(649, 650)).toBe(false);
  });

  it.each([0, -1, 1.5, Number.POSITIVE_INFINITY, Number.NaN, Number.MAX_SAFE_INTEGER + 1])(
    "rejects nonpositive or imprecise finite budgets: %s", (value) => {
      expect(limitsValid({ ...DEFAULT_LIMITS, max_calls: value }, unlimited)).toBe(false);
    },
  );

  it("respects older advertised numeric ceilings without claiming unlimited support", () => {
    const legacy = { max_calls: 1000, max_tokens: 5120000, max_parallel: 8, max_windows: 1000 };
    expect(budgetBounds(config(legacy))).toEqual(legacy);
    expect(limitsValid({ ...DEFAULT_LIMITS, max_tokens: 5120001 }, legacy)).toBe(false);
    expect(limitsValid(unlimited, legacy)).toBe(false);
  });
});
