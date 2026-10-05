import type { Limits, PublicConfig } from "./types";

export const DEFAULT_LIMITS: Limits = {
  max_calls: 24,
  max_tokens: 128000,
  max_parallel: 3,
  max_windows: 8,
};

/** Compatibility bounds for servers or cached configuration without budget metadata. */
export const FALLBACK_BUDGET_BOUNDS: Limits = {
  max_calls: 1000,
  max_tokens: 5120000,
  max_parallel: 8,
  max_windows: 1000,
};

export const LIMIT_FIELDS = [
  ["max_calls", "调用上限"],
  ["max_tokens", "Token 上限"],
  ["max_parallel", "并行位置"],
  ["max_windows", "窗口上限"],
] as const;

export function budgetBounds(config: PublicConfig | null): Limits {
  const maximum = { ...FALLBACK_BUDGET_BOUNDS };
  for (const [field] of LIMIT_FIELDS) {
    const value = config?.budget_bounds?.[field];
    if (typeof value === "number" && Number.isInteger(value) && value >= 1) {
      maximum[field] = value;
    }
  }
  return maximum;
}

export function limitsValid(limits: Limits, maximum: Limits, minimum?: Limits): boolean {
  return LIMIT_FIELDS.every(([field]) => Number.isInteger(limits[field]) &&
    limits[field] >= (minimum?.[field] ?? 1) && limits[field] <= maximum[field]);
}

export const sameLimits = (left: Limits, right: Limits) =>
  LIMIT_FIELDS.every(([field]) => left[field] === right[field]);

export const budgetRangeText = (maximum: Limits) =>
  `预算须为范围内的正整数：调用 ≤ ${maximum.max_calls.toLocaleString("zh-CN")}，Token ≤ ${maximum.max_tokens.toLocaleString("zh-CN")}，并行 ≤ ${maximum.max_parallel}，窗口 ≤ ${maximum.max_windows.toLocaleString("zh-CN")}。`;
