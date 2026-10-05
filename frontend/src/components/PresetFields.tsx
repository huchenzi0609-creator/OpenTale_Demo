import type { Position } from "../types";
import { codePointLength, type PresetDraft } from "../presetDraft";

export function PositionFields({
  position, index, canRemove, disabled, onChange, onRemove,
}: {
  position: Position;
  index: number;
  canRemove: boolean;
  disabled: boolean;
  onChange: (position: Position) => void;
  onRemove: () => void;
}) {
  const number = index + 1;
  return (
    <fieldset className="preset-card" disabled={disabled}>
      <legend>人物 {number}</legend>
      <div className="preset-card-heading">
        <label htmlFor={`person-name-${index}`}>
          人物 {number} 名称
          <input id={`person-name-${index}`} value={position.name} maxLength={80}
            onChange={(event) => onChange({ ...position, name: event.target.value })} />
        </label>
        <button type="button" className="text-button remove-field" disabled={!canRemove}
          aria-label={`删除人物 ${number}`} onClick={onRemove}>删除人物</button>
      </div>
      <label htmlFor={`person-seed-${index}`}>
        人物 {number} 私人背景（Markdown）
        <textarea id={`person-seed-${index}`} value={position.seed} rows={8}
          placeholder="# 过往生活\n\n# 与其他人的关系\n\n# 初始看法、愿望与尚未完成的事"
          onChange={(event) => onChange({ ...position, seed: event.target.value })}
          aria-describedby={`person-count-${index}`} />
      </label>
      <p id={`person-count-${index}`} className={`field-count ${codePointLength(position.seed) > 12000 ? "over-limit" : ""}`}>
        {codePointLength(position.seed).toLocaleString("zh-CN")} / 12,000 字符
      </p>
      <details className="field-advanced">
        <summary>高级：人物 ID</summary>
        <label htmlFor={`person-id-${index}`}>
          人物 {number} ID
          <input id={`person-id-${index}`} value={position.id} autoCapitalize="none" spellCheck={false}
            onChange={(event) => onChange({ ...position, id: event.target.value })} />
        </label>
      </details>
    </fieldset>
  );
}

export function ExpressionFields({
  expression, index, canRemove, disabled, onChange, onRemove,
}: {
  expression: PresetDraft["expressions"][number];
  index: number;
  canRemove: boolean;
  disabled: boolean;
  onChange: (expression: PresetDraft["expressions"][number]) => void;
  onRemove: () => void;
}) {
  const number = index + 1;
  return (
    <fieldset className="preset-card" disabled={disabled}>
      <legend>共同表达 {number}</legend>
      <div className="preset-card-heading">
        <label htmlFor={`expression-role-${index}`}>
          共同表达 {number} 初始用法
          <input id={`expression-role-${index}`} value={expression.role} maxLength={80}
            onChange={(event) => onChange({ ...expression, role: event.target.value })} />
        </label>
        <button type="button" className="text-button remove-field" disabled={!canRemove}
          aria-label={`删除共同表达 ${number}`} onClick={onRemove}>删除表达</button>
      </div>
      <label htmlFor={`expression-text-${index}`}>
        共同表达 {number} 内容
        <textarea id={`expression-text-${index}`} value={expression.text} rows={5}
          placeholder="描述几位人物共同承接的一段生活、事件或尚未完成的表达。"
          onChange={(event) => onChange({ ...expression, text: event.target.value })}
          aria-describedby={`expression-count-${index}`} />
      </label>
      <p id={`expression-count-${index}`} className={`field-count ${codePointLength(expression.text) > 6000 ? "over-limit" : ""}`}>
        {codePointLength(expression.text).toLocaleString("zh-CN")} / 6,000 字符
      </p>
      <details className="field-advanced">
        <summary>高级：共同表达 ID</summary>
        <label htmlFor={`expression-id-${index}`}>
          共同表达 {number} ID
          <input id={`expression-id-${index}`} value={expression.id} autoCapitalize="none" spellCheck={false}
            onChange={(event) => onChange({ ...expression, id: event.target.value })} />
        </label>
      </details>
    </fieldset>
  );
}
