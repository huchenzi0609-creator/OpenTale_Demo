import type { FiniteLimits, Limits, PublicConfig } from "./types";

export const DEFAULT_LIMITS: FiniteLimits = {
  max_calls: 24,
  max_tokens: 128000,
  max_parallel: 3,
  max_windows: 8,
};

/** Missing metadata does not reinstate obsolete software ceilings. */
export const FALLBACK_BUDGET_BOUNDS: Limits = {
  max_calls: null,
  max_tokens: null,
  max_parallel: null,
  max_windows: null,
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
    if (value === null || (typeof value === "number" && Number.isSafeInteger(value) && value >= 1)) {
      maximum[field] = value;
    }
  }
  return maximum;
}

export function limitsValid(limits: Limits, maximum: Limits, minimum?: FiniteLimits): boolean {
  return LIMIT_FIELDS.every(([field]) => {
    const value = limits[field];
    const ceiling = maximum[field];
    if (value === null) return ceiling === null;
    return Number.isSafeInteger(value) && value >= (minimum?.[field] ?? 1) &&
      (ceiling === null || value <= ceiling);
  });
}

export const sameLimits = (left: Limits, right: Limits) =>
  LIMIT_FIELDS.every(([field]) => left[field] === right[field]);

export const budgetText = (value: number | null) =>
  value === null ? "不设限" : value.toLocaleString("zh-CN");

export const budgetExhausted = (used: number, limit: number | null) => limit !== null && used >= limit;

export const budgetRangeText = (maximum: Limits) => {
  const bounded = LIMIT_FIELDS.filter(([field]) => maximum[field] !== null)
    .map(([field, label]) => `${label} ≤ ${budgetText(maximum[field])}`);
  return "有限预算须为可精确表示的正整数；各项可选择不设限。" +
    (bounded.length ? `当前服务范围：${bounded.join("，")}。` : "");
};
