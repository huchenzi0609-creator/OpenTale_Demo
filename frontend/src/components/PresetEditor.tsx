import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError } from "../api";
import type { Preset } from "../types";
import { makeDraft, newPresetId, nextId, validateDraft, type EditorMode, type PresetDraft } from "../presetDraft";
import { ExpressionFields, PositionFields } from "./PresetFields";

export default function PresetEditor({ mode, source, onClose, onSave }: {
  mode: EditorMode;
  source?: Preset;
  onClose: () => void;
  onSave: (draft: PresetDraft, original?: Preset) => Promise<void>;
}) {
  const [draft, setDraft] = useState(() => makeDraft(mode, source));
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [conflict, setConflict] = useState(false);
  const [saveAsCopy, setSaveAsCopy] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const savingRef = useRef(false);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const isEditing = mode === "edit" && !saveAsCopy;
  const title = isEditing ? "编辑自定义预设" : mode === "copy" || saveAsCopy ? "复制为自定义实验" : "自定义实验";

  useEffect(() => {
    if (errors.length) container.current?.querySelector<HTMLElement>(".preset-errors")?.focus();
  }, [errors]);

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    container.current?.querySelector<HTMLInputElement>("input")?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!savingRef.current) closeRef.current();
      }
      if (event.key !== "Tab") return;
      const candidates = container.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]',
      );
      const focusable = Array.from(candidates ?? []).filter((element) => !element.closest("details:not([open])") || element.tagName === "SUMMARY");
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.body.style.overflow = overflow;
      document.removeEventListener("keydown", keydown);
      previous?.focus();
    };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const issues = validateDraft(draft);
    setErrors(issues);
    setConflict(false);
    if (issues.length || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      await onSave(draft, isEditing ? source : undefined);
      onClose();
    } catch (error) {
      setConflict(error instanceof ApiError && error.code.includes("CONFLICT"));
      setErrors([error instanceof ApiError
        ? error.code.includes("CONFLICT")
          ? `${error.message}（${error.code}）。你的填写内容已保留；请先在另一页面核对最新版本再重试。`
          : `${error.message}（${error.code}）`
        : "保存未完成，填写内容已保留，请稍后重试。"]);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  return (
    <div className="preset-overlay">
      <div ref={container} role="dialog" aria-modal="true" aria-labelledby="preset-editor-title"
        aria-describedby="preset-editor-description" className="preset-editor">
        <form onSubmit={(event) => void submit(event)} noValidate>
          <header className="preset-editor-header">
            <div><p className="eyebrow">CUSTOM EXPERIMENT</p><h2 id="preset-editor-title">{title}</h2></div>
            <button type="button" className="icon-button" disabled={saving} aria-label="关闭自定义实验编辑器" onClick={onClose}>×</button>
          </header>
          <div className="preset-editor-body">
            <p id="preset-editor-description" className="preset-explanation">
              设置人物的私人生活与共同表达，保存为本地预设。保存后在左侧点击“创建实验”；保存本身不调用模型，也不修改已有存档。
            </p>
            {errors.length > 0 && <div className="preset-errors" role="alert" tabIndex={-1}><strong>请检查以下内容</strong><ul>{errors.map((error) => <li key={error}>{error}</li>)}</ul>
              {conflict && isEditing && <button type="button" className="button secondary" onClick={() => {
                setSaveAsCopy(true);
                setDraft({ ...draft, id: newPresetId(), name: `${draft.name} · 副本`.slice(0, 100) });
                setConflict(false);
                setErrors([]);
              }}>保留填写，另存为新预设</button>}
            </div>}
            <fieldset className="preset-basic" disabled={saving}>
              <label htmlFor="preset-name">预设名称<input id="preset-name" value={draft.name} maxLength={100}
                onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
              <label htmlFor="preset-description">实验说明（可选）<textarea id="preset-description" rows={3} value={draft.description}
                onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label>
              <details className="field-advanced"><summary>高级：预设 ID</summary><label htmlFor="preset-id">预设 ID
                <input id="preset-id" value={draft.id} disabled={isEditing} autoCapitalize="none" spellCheck={false}
                  onChange={(event) => setDraft({ ...draft, id: event.target.value })} /></label>
                <p className="helper">ID 用于本地目录与引用，名称可以随时修改。仅限小写字母、数字、下划线和连字符。</p>
              </details>
            </fieldset>
            <section className="preset-editor-section" aria-label="人物设计">
              <div className="preset-section-title"><h3>人物设计</h3><span>{draft.positions.length} / 8 位</span></div>
              <p className="helper">写入过往生活、关系、初始看法和愿望。人物 Markdown 是私人初始材料；共有叙事纪律由系统注入，无需重复写入人物背景。初始资料不是永久不变的角色指令。</p>
              {draft.positions.map((position, index) => <PositionFields key={index} position={position} index={index}
                canRemove={draft.positions.length > 3} disabled={saving}
                onChange={(next) => setDraft({ ...draft, positions: draft.positions.map((item, at) => at === index ? next : item) })}
                onRemove={() => setDraft({ ...draft, positions: draft.positions.filter((_, at) => at !== index) })} />)}
              <button type="button" className="button secondary" disabled={saving || draft.positions.length >= 8}
                onClick={() => setDraft({ ...draft, positions: [...draft.positions, { id: nextId("pos_", draft.positions.map((item) => item.id)), name: "", seed: "" }] })}>＋ 添加人物</button>
              <p className="helper">每组 3–8 位人物。私人材料只交给对应位置的模型；模型生成可使它在叙事中显露。</p>
            </section>
            <section className="preset-editor-section" aria-label="共同表达设计">
              <div className="preset-section-title"><h3>共同表达</h3><span>{draft.expressions.length} / 6 份</span></div>
              <p className="helper">共同承接的一段内容，为各位置提供实际关联。当前版本让每份初始共同表达由所有人物承接；各处后续可以形成不同用法和归属。</p>
              {draft.expressions.map((expression, index) => <ExpressionFields key={index} expression={expression} index={index}
                canRemove={draft.expressions.length > 1} disabled={saving}
                onChange={(next) => setDraft({ ...draft, expressions: draft.expressions.map((item, at) => at === index ? next : item) })}
                onRemove={() => setDraft({ ...draft, expressions: draft.expressions.filter((_, at) => at !== index) })} />)}
              <button type="button" className="button secondary" disabled={saving || draft.expressions.length >= 6}
                onClick={() => setDraft({ ...draft, expressions: [...draft.expressions, { id: nextId("e_shared_", draft.expressions.map((item) => item.id)), text: "", role: "共同生活" }] })}>＋ 添加共同表达</button>
            </section>
            <div className="preset-storage"><strong>本地结构化文件</strong>
              <p>保存后生成预设 JSON 与各人物的 Markdown / JSON，存放在 Git 忽略的私人目录中。密钥继续使用服务端配置文件，此处无需填写。</p>
              {isEditing && source?.storage_path && <code>{source.storage_path}</code>}
              {isEditing && source?.version && <small>当前版本：{source.version}</small>}
            </div>
          </div>
          <footer className="preset-editor-footer"><span>只保存预设；创建实验后开始或单步才会生成。</span>
            <button type="button" className="button secondary" disabled={saving} onClick={onClose}>取消</button>
            <button type="submit" className="button primary" disabled={saving}>{saving ? "正在保存…" : "保存预设"}</button>
          </footer>
        </form>
      </div>
    </div>
  );
}
