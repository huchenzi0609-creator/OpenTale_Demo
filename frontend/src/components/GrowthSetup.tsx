import { useRef, useState } from "react";
import { budgetBounds, DEFAULT_LIMITS, LIMIT_FIELDS, limitsValid } from "../budgetLimits";
import type { GrowthWorkbenchModel } from "../useGrowthWorkbench";
import type { Limits, Provider } from "../types";
import { GROWTH_DEFAULT_LIMITS } from "../graphTypes";

interface WindowDraft { id: string; label: string; seed: string; direction: string }
export default function GrowthSetup({ model }: { model: GrowthWorkbenchModel }) {
  const [title, setTitle] = useState("生长与遍历实验");
  const [source, setSource] = useState("custom");
  const [provider, setProvider] = useState<Provider>("fixture");
  const [sharedSeed, setSharedSeed] = useState("一封没有署名的信，在两人的生活之间流转。");
  const [windows, setWindows] = useState<WindowDraft[]>([
    { id: "seed-1", label: "写信人", seed: "他留在旧屋里，一封信还没有写完。", direction: "" },
    { id: "seed-2", label: "读信人", seed: "她在归途收到那封信，尚不知道它来自何处。", direction: "" },
  ]);
  const [limits, setLimits] = useState<Limits>(GROWTH_DEFAULT_LIMITS);
  const counter = useRef(3);
  const customPresetId = useRef(`growth-${crypto.randomUUID().slice(0, 8)}`);
  const windowSource = source === "custom" || source.startsWith("graph:");
  const graphPreset = model.graphPresets.find((preset) => `graph:${preset.id}` === source);
  const disabled = model.booting || !!model.busy;
  const seedsValid = title.trim() && windows.every((window) => window.label.trim() && window.seed.trim());
  const valid = title.trim() && (!windowSource || seedsValid) &&
    limitsValid(limits, budgetBounds(model.config)) && (provider !== "deepseek" || model.config?.configured);
  const patchWindow = (id: string, field: keyof Omit<WindowDraft, "id">, value: string) =>
    setWindows((current) => current.map((window) => window.id === id ? { ...window, [field]: value } : window));
  return <aside className="sidebar growth-sidebar">
    <a className="brand" href="#growth-main"><span className="brand-mark">O<span>↗</span></span><span>OpenTale<small>GROWTH / READING</small></span></a>
    <form className="growth-create-form" aria-label="创建生长与遍历实验" onSubmit={(event) => {
      event.preventDefault();
      if (!valid || disabled) return;
      void model.create({ title: title.trim(), provider, limits, ...(windowSource
        ? { windows: windows.map((window) => ({ ...window, label: window.label.trim() })), shared_seed: sharedSeed, ...(graphPreset ? { preset_id: source } : {}) }
        : { preset_id: source }) });
    }}>
      <div className="sidebar-heading">新建图世界<span className="small-tag">OPT-IN</span></div>
      <label>实验名称<input value={title} onChange={(event) => setTitle(event.target.value)} disabled={disabled} /></label>
      <label>种子来源<select value={source} onChange={(event) => {
        const nextSource = event.target.value;
        setSource(nextSource);
        const saved = model.graphPresets.find((preset) => `graph:${preset.id}` === nextSource);
        if (saved) {
          setWindows(saved.windows.map((window) => ({ ...window, direction: window.direction ?? "" })));
          setSharedSeed(saved.shared_seed);
          setTitle(saved.name);
          counter.current = Math.max(1, ...saved.windows.map((window) => Number(window.id.replace(/^seed-/, "")) || 0)) + 1;
        }
      }} disabled={disabled}>
        <option value="custom">编辑种子与生成窗口</option>
        {model.graphPresets.map((preset) => <option key={`graph:${preset.id}`} value={`graph:${preset.id}`}>{preset.name} · 图种子预设</option>)}
        {model.presets.map((preset) => <option key={preset.id} value={preset.id}>{preset.name}{preset.origin === "custom" ? " · 自定义" : ""}</option>)}
      </select></label>
      {windowSource ? <>
        <p className="helper">种子是起点。每个窗口可长出新节点，也可再次进入已有节点；方向可以留空。</p>
        {windows.map((window, index) => <fieldset key={window.id} className="growth-window-draft" disabled={disabled}>
          <legend>生成窗口 {index + 1}</legend>
          <label>窗口名称 {index + 1}<input value={window.label} onChange={(event) => patchWindow(window.id, "label", event.target.value)} /></label>
          <label>种子 Markdown {index + 1}<textarea rows={7} value={window.seed} onChange={(event) => patchWindow(window.id, "seed", event.target.value)} /></label>
          <span className="helper">{Array.from(window.seed).length.toLocaleString("zh-CN")} 字 · 保留完整原文</span>
          <label>生长方向 {index + 1}（可选）<textarea rows={2} value={window.direction} onChange={(event) => patchWindow(window.id, "direction", event.target.value)} placeholder="可以留空，让窗口继续探索" /></label>
          {windows.length > 1 && <button type="button" className="text-button" onClick={() => setWindows((current) => current.filter((item) => item.id !== window.id))}>移除窗口 {index + 1}</button>}
        </fieldset>)}
        <button type="button" className="button secondary full" disabled={disabled} onClick={() => {
          const id = `seed-${counter.current++}`;
          setWindows((current) => [...current, { id, label: `窗口 ${current.length + 1}`, seed: "", direction: "" }]);
        }}>＋ 添加生成窗口</button>
        <label>共同种子（可选）<textarea rows={4} value={sharedSeed} disabled={disabled} onChange={(event) => setSharedSeed(event.target.value)} /></label>
        <p className="helper">共同种子只创建一个原始节点，供多个窗口作为入口。正文与每个窗口自己的种子分别保存。</p>
        <button type="button" className="button secondary full" disabled={disabled || !seedsValid} onClick={async () => {
          const saved = await model.savePreset({ id: graphPreset?.id ?? customPresetId.current, name: title.trim(), windows, shared_seed: sharedSeed, ...(graphPreset ? { expected_version: graphPreset.version } : {}) });
          if (saved) setSource(`graph:${saved.id}`);
        }}>{model.busy === "保存图种子预设" ? "正在保存预设…" : graphPreset ? "保存预设更新" : "保存为图种子预设"}</button>
        <p className="helper">以实验名称保存窗口、方向与完整 Markdown。保存预设不创建实验，也不调用模型。</p>
        {model.notice && <p className="helper" role="status">{model.notice}</p>}
      </> : <p className="helper">将所选预设的人物资料导入为生成窗口，共同表达导入为共享入口。原有预设保留。</p>}
      <label>生长生成方式<select value={provider} disabled={disabled} onChange={(event) => setProvider(event.target.value as Provider)}>
        <option value="fixture">离线夹具 · 不使用 API</option><option value="deepseek">DeepSeek · 已配置 API</option>
      </select></label>
      <p className="helper">{provider === "fixture" ? "夹具验证生长与遍历机制，其文字不代表模型的文学效果。" : "创建只保存种子。开始或单步才会调用已配置模型。"}</p>
      {provider === "deepseek" && !model.config?.configured && <p className="helper growth-warning">当前模型配置尚未就绪。可在共同生成工作台检查本地配置。</p>}
      <details className="growth-budget"><summary>本实验预算</summary><p className="helper">调用、Token 与窗口累计预算默认不设限，可按需填写有限上限。</p><div className="budget-grid">
        {LIMIT_FIELDS.map(([field, label]) => <label key={field}>{label}<input type="number" min={1} value={limits[field] ?? ""} disabled={disabled || limits[field] === null}
          onChange={(event) => setLimits((current) => ({ ...current, [field]: event.target.value === "" ? 0 : Number(event.target.value) }))} />
          <span className="growth-unlimited"><input type="checkbox" aria-label={`${label}不设限`} checked={limits[field] === null} disabled={disabled || budgetBounds(model.config)[field] !== null}
            onChange={(event) => setLimits((current) => ({ ...current, [field]: event.target.checked ? null : DEFAULT_LIMITS[field] }))} />不设限</span>
        </label>)}
      </div></details>
      <button className="button primary full" type="submit" disabled={disabled || !valid}>{model.busy === "创建图谱" ? "正在保存种子…" : "＋ 创建生长实验"}</button>
    </form>
    <section className="growth-library" aria-label="图世界存档">
      <label>选择图世界存档<select value={model.selectedRunId} disabled={!!model.busy} onChange={(event) => void model.selectRun(event.target.value)}>
        <option value="">选择已保存的实验</option>{model.runs.map((run) => <option key={run.id} value={run.id}>{run.title} · G{run.revision}</option>)}
      </select></label>
      <p className="helper">生长与阅读各自保存进度；暂停生长后，仍可在同一图版本上重复阅读。</p>
    </section>
  </aside>;
}
