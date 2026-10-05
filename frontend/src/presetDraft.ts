import type { Position, Preset } from "./types";

export interface PresetDraft {
  id: string;
  name: string;
  description: string;
  positions: Position[];
  expressions: { id: string; text: string; role: string }[];
}
export type EditorMode = "create" | "copy" | "edit";
export const codePointLength = (text: string) => Array.from(text).length;
const ID = /^[a-z][a-z0-9_-]{0,63}$/;
const POSITION_ID = /^[a-z][a-z0-9_]{0,39}$/;

export function newPresetId() {
  return `custom_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
}

export function makeDraft(mode: EditorMode, source?: Preset): PresetDraft {
  if (source) {
    return {
      id: mode === "edit" ? source.id : newPresetId(),
      name: mode === "edit" ? source.name : `${source.name} · 副本`,
      description: source.description,
      positions: source.positions.map((item) => ({ ...item })),
      expressions: source.expressions.map((item) => ({
        id: item.id,
        text: item.text,
        role: item.role ?? "共同生活",
      })),
    };
  }
  return {
    id: newPresetId(),
    name: "",
    description: "",
    positions: [1, 2, 3].map((number) => ({
      id: `pos_${number}`,
      name: "",
      seed: "",
    })),
    expressions: [{ id: "e_shared_1", text: "", role: "共同生活" }],
  };
}

export function nextId(prefix: string, ids: string[]) {
  let number = 1;
  while (ids.includes(`${prefix}${number}`)) number += 1;
  return `${prefix}${number}`;
}

/** Mirror public authoring limits; the server remains the authoritative validator. */
export function validateDraft(draft: PresetDraft): string[] {
  const errors: string[] = [];
  if (!ID.test(draft.id))
    errors.push("预设 ID 须以小写字母开头，仅含小写字母、数字、下划线或连字符，最多 64 字符。");
  if (!draft.name.trim() || codePointLength(draft.name.trim()) > 100)
    errors.push("预设名称须为 1–100 字符。");
  if (codePointLength(draft.description) > 2000)
    errors.push("实验说明最多 2,000 字符。");
  if (draft.positions.length < 3 || draft.positions.length > 8)
    errors.push("请设置 3–8 位人物。");
  if (draft.expressions.length < 1 || draft.expressions.length > 6)
    errors.push("请设置 1–6 份共同表达。");
  const seen = new Set<string>();
  for (const [index, position] of draft.positions.entries()) {
    if (!POSITION_ID.test(position.id)) errors.push(`人物 ${index + 1} 的 ID 须以小写字母开头，仅含小写字母、数字或下划线，最多 40 字符。`);
    if (seen.has(position.id)) errors.push(`人物 ID「${position.id}」重复。`);
    seen.add(position.id);
    if (!position.name.trim() || codePointLength(position.name.trim()) > 80)
      errors.push(`人物 ${index + 1} 的名称须为 1–80 字符。`);
    const length = codePointLength(position.seed);
    if (!position.seed.trim() || length > 12000)
      errors.push(`人物 ${index + 1} 的私人背景须为 1–12,000 字符。`);
  }
  seen.clear();
  for (const [index, expression] of draft.expressions.entries()) {
    if (!ID.test(expression.id) || /^(seed-|expr-)/.test(expression.id))
      errors.push(`共同表达 ${index + 1} 的 ID 格式无效，不能使用 seed- 或 expr- 保留前缀。`);
    if (seen.has(expression.id)) errors.push(`共同表达 ID「${expression.id}」重复。`);
    seen.add(expression.id);
    if (!expression.text.trim() || codePointLength(expression.text) > 6000)
      errors.push(`共同表达 ${index + 1} 的内容须为 1–6,000 字符。`);
    if (!expression.role.trim() || codePointLength(expression.role.trim()) > 80)
      errors.push(`共同表达 ${index + 1} 的初始用法须为 1–80 字符。`);
  }
  return errors;
}

export function presetPayload(draft: PresetDraft): PresetDraft {
  return {
    ...draft,
    name: draft.name.trim(),
    description: draft.description.trim(),
    positions: draft.positions.map((position) => ({
      ...position,
      name: position.name.trim(),
    })),
    expressions: draft.expressions.map((expression) => ({
      ...expression,
      role: expression.role.trim(),
    })),
  };
}
