import { useState } from "react";
import { DEFAULT_LIMITS, LIMIT_FIELDS, budgetRangeText } from "../budgetLimits";
import type { EditorMode } from "../presetDraft";
import type { Preset } from "../types";
import type { Provider } from "../types";
import type { Workbench } from "../useWorkbench";
import PresetEditor from "./PresetEditor";
const KEY_PATH =
  "/Users/huchenzi/Ready4AI/Project_OpenTale/Opentale_Demo/.env.local";

const STATUS: Record<string, string> = {
  idle: "待命",
  running: "运行中",
  paused: "已暂停",
  error: "需恢复",
  budget_exhausted: "预算已用尽",
};
export default function Sidebar({ model }: { model: Workbench }) {
  const [editor, setEditor] = useState<{ mode: EditorMode; source?: Preset } | null>(null);
  const {
    config,
    presets,
    presetErrors,
    runs,
    presetId,
    setPresetId,
    provider,
    setProvider,
    limits,
    setLimits,
    snapshot,
    maximumLimits,
    minimumRunLimits,
    validRunLimits,
    limitsChanged,
    budgetError,
    updateRunLimits,
    resetRunLimits,
    selectedRunId,
    booting,
    busy,
    anyActive,
    preset,
    disabled,
    validLimits,
    createError,
    createRun,
    savePreset,
    reloadConfig,
    reloadLibrary,
    selectRun,
  } = model;
  const needsConfig = provider === "deepseek" && !config?.configured;
  const diagnostics = config?.diagnostics;
  const keyPath = diagnostics?.config_file_path ?? KEY_PATH;
  const configBlockReason = !config
    ? "尚未取得模型配置状态，暂时无法创建实时实验。"
    : diagnostics?.environment_api_key_empty_override
      ? "OPENTALE_API_KEY 的空环境变量覆盖了本机文件，DeepSeek 尚未配置。"
      : diagnostics && !diagnostics.config_file_exists
        ? "当前服务未找到模型配置文件，DeepSeek 尚未配置。"
        : diagnostics && !diagnostics.file_api_key_present
          ? "配置文件中未读取到非空的 OPENTALE_API_KEY，DeepSeek 尚未配置。"
          : "DeepSeek 尚未配置，暂时无法创建实时实验。";
  const configHelp = diagnostics?.environment_api_key_empty_override
    ? <>启动本地服务时移除 <code>OPENTALE_API_KEY</code> 的空环境覆盖，再刷新模型配置。本机配置文件为 <code>{keyPath}</code>。</>
    : diagnostics && !diagnostics.config_file_exists
      ? <>请在当前服务读取的本机路径 <code>{keyPath}</code> 创建配置文件，填写 <code>OPENTALE_API_KEY</code> 并保存，再刷新模型配置。</>
      : diagnostics && !diagnostics.file_api_key_present
        ? <>请检查当前服务读取的本机文件 <code>{keyPath}</code> 中是否填写了 <code>OPENTALE_API_KEY</code>，并确认已保存，再刷新模型配置。</>
        : <>请检查本机文件 <code>{keyPath}</code> 的 <code>OPENTALE_API_KEY</code> 配置，保存后刷新模型配置。</>;
  const createDisabledReason = booting
    ? "正在读取本地服务与预设，完成后即可创建实验。"
    : disabled
      ? `正在${busy}，完成后可创建实验。`
      : !preset
        ? "尚无可用的初始叙事，请选择或保存一份预设。"
        : !validLimits
          ? budgetRangeText(maximumLimits)
          : needsConfig
            ? configBlockReason
            : "";
  return (
    <aside className="sidebar">
      <a className="brand" href="#main" aria-label="OpenTale 进入实验">
        <span className="brand-mark">
          O<span>↗</span>
        </span>
        <span>
          OpenTale<small>NARRATIVE LAB / 0.2</small>
        </span>
      </a>
      <div className="sidebar-heading">
        实验工作台<span className="small-tag">LOCAL</span>
      </div>
      <section className="setup-panel" aria-label="创建实验">
        <label htmlFor="preset">初始叙事</label>
        <select
          id="preset"
          value={presetId}
          onChange={(event) => setPresetId(event.target.value)}
          disabled={disabled || booting}
        >
          {presets.map((item) => (
            <option value={item.id} key={item.id}>
              {item.name}{item.origin === "custom" ? " · 自定义" : ""}
            </option>
          ))}
        </select>
        {preset && <p className="helper">{preset.description}</p>}
        {presetErrors.length > 0 && <details className="preset-catalog-errors">
          <summary>有 {presetErrors.length} 份自定义预设未能载入</summary>
          {presetErrors.map((item) => <p className="helper" key={item.id}>{item.id}：{item.message}（{item.code}）</p>)}
        </details>}
        <div className="preset-actions">
          <button className="button secondary full" disabled={disabled || booting}
            onClick={() => setEditor({ mode: "create" })}>＋ 自定义实验</button>
          <div className="preset-action-row">
            <button className="text-button" disabled={disabled || booting || !preset}
              onClick={() => preset && setEditor({ mode: "copy", source: preset })}>复制编辑</button>
            {preset?.editable && <button className="text-button" disabled={disabled || booting}
              onClick={() => setEditor({ mode: "edit", source: preset })}>编辑当前预设</button>}
          </div>
          <p className="helper">自定义人物 Markdown 与共同表达；内置预设可复制编辑。</p>
          {preset?.storage_path && <details><summary>预设文件与版本</summary><code>{preset.storage_path}</code>
            <p className="helper">当前版本：{preset.version}。私人文件仅保存在本地，不纳入 Git。</p></details>}
        </div>
        <label htmlFor="provider">生成方式</label>
        <select
          id="provider"
          value={provider}
          onChange={(event) => setProvider(event.target.value as Provider)}
          disabled={disabled}
        >
          <option value="fixture">离线夹具 · 不使用 API</option>
          <option value="deepseek">DeepSeek · 使用已配置 API</option>
        </select>
        <p className="helper">
          {provider === "fixture"
            ? "确定性文本用于验证结构机制，不代表模型的叙事效果。"
            : "创建后开始或单步才会调用模型。下方预算是本次实验的硬上限。"}
        </p>
        <div id="experiment-budget" role="group" aria-label="实验预算">
          <p className="helper">新实验默认 Token 预算为 {DEFAULT_LIMITS.max_tokens.toLocaleString("zh-CN")}，上限 {maximumLimits.max_tokens.toLocaleString("zh-CN")}（按 Token 数计）。加载存档后，下方显示该实验已保存的预算。</p>
          <div className="budget-grid">
          {LIMIT_FIELDS.map(([field, label]) => (
            <label key={field} htmlFor={field}>
              {label}
              <input
                id={field}
                type="number"
                min="1"
                max={maximumLimits[field]}
                value={limits[field]}
                disabled={disabled || anyActive}
                onChange={(event) =>
                  setLimits((current) => ({
                    ...current,
                    [field]: Number(event.target.value),
                  }))
                }
              />
            </label>
          ))}
          </div>
          {snapshot && <div className="run-budget-actions">
            <p className="helper">当前存档：{snapshot.run.title}。更新只修改本实验上限，保留已有用量、结果与恢复点，不调用模型。</p>
            <button className="button secondary full"
              disabled={disabled || anyActive || !validRunLimits || !limitsChanged}
              onClick={() => void updateRunLimits()}>
              {busy === "更新实验预算" ? "正在更新…" : "更新本实验预算"}
            </button>
            <button className="text-button" disabled={disabled || anyActive || !limitsChanged}
              onClick={resetRunLimits}>恢复已保存预算</button>
            {!validRunLimits && !anyActive && <p className="helper creation-feedback" role="status">
              {validLimits
                ? `上限不能低于本档已用量。本档最低上限：调用 ${minimumRunLimits.max_calls}，Token ${minimumRunLimits.max_tokens.toLocaleString("zh-CN")}，窗口 ${minimumRunLimits.max_windows}；各上限须为正整数。`
                : budgetRangeText(maximumLimits)}
            </p>}
            {budgetError && <p className="creation-feedback creation-error" role="alert">
              <strong>预算未能更新</strong><span>{budgetError}</span>
            </p>}
          </div>}
          {anyActive && <p className="helper creation-feedback" role="status">请先暂停运行中的实验，再修改预算。</p>}
        </div>
        {provider === "deepseek" && preset && <p className="helper budget-estimate">
          {preset.positions.length} 位人物的完整窗口通常需要 {preset.positions.length * 2 + 1} 次调用，重试另计。请先用少量窗口验证。
        </p>}
        <button
          className="button primary full"
          disabled={!!createDisabledReason}
          aria-describedby={[
            createDisabledReason && "create-disabled-reason",
            needsConfig && "create-config-help",
            createError && "create-error",
          ].filter(Boolean).join(" ") || undefined}
          onClick={() => void createRun()}
        >
          {busy === "创建实验" ? "正在创建…" : "＋ 创建实验"}
        </button>
        {createDisabledReason && <p className="helper creation-feedback" id="create-disabled-reason" role="status">
          {createDisabledReason}
        </p>}
        {needsConfig && <div className="creation-feedback creation-config-help" id="create-config-help">
          <p className="helper">{configHelp} 也可选择离线夹具，直接创建实验。</p>
          <button className="text-button" disabled={disabled || anyActive}
            onClick={() => void reloadConfig()}>刷新模型配置</button>
          {anyActive && <p className="helper">请先暂停运行中的实验，再刷新配置。</p>}
        </div>}
        {createError && <p className="creation-feedback creation-error" id="create-error" role="alert">
          <strong>实验未能创建</strong><span>{createError}</span>
        </p>}
      </section>
      <section className="config-panel" aria-label="API 配置状态">
        <div className="panel-row">
          <strong>模型连接</strong>
          <span
            className={`connection-dot ${config?.configured ? "ready" : ""}`}
          />
          {!config ? "未连接" : config.configured ? "已配置" : "未配置"}
        </div>
        <p className="helper">
          {config?.configured
            ? `${config.model} · 密钥仅保存在服务端`
            : "离线实验可直接运行。实时实验请在本地文件填写密钥。"}
        </p>
        <details>
          <summary>查看安全配置方式</summary>
          <p className="helper">
            将 API key 填写到此文件的 <code>OPENTALE_API_KEY</code>{" "}
            字段。此处不提供密钥输入，也不读取密钥内容。
          </p>
          <code className="file-path">{keyPath}</code>
        </details>
        <button
          className="text-button"
          disabled={disabled || anyActive}
          onClick={() => void reloadConfig()}
        >
          重新加载配置
        </button>
        {anyActive && <p className="helper">请暂停运行后再重新加载配置。</p>}
      </section>
      <section className="library" aria-label="实验存档">
        <div className="panel-row">
          <strong>实验存档</strong>
          <button
            className="text-button"
            disabled={disabled}
            onClick={() => void reloadLibrary()}
          >
            刷新
          </button>
        </div>
        <select
          aria-label="选择存档"
          value={selectedRunId}
          disabled={disabled}
          onChange={(event) => void selectRun(event.target.value)}
        >
          <option value="">选择已有实验</option>
          {runs.map((run) => (
            <option value={run.id} key={run.id}>
              {run.title} · R{run.revision} · {STATUS[run.status]}
            </option>
          ))}
        </select>
        <p className="helper">
          {runs.length
            ? `${runs.length} 份本地实验。分支保存各自的关系与读法。`
            : "还没有存档。创建后自动保存在本地。"}
        </p>
      </section>
      <div className="sidebar-footer">
        一份表达，多处生活。
        <br />
        <span>共同构成 · 条件回流 · 承接呈现</span>
      </div>
      {editor && <PresetEditor mode={editor.mode} source={editor.source}
        onClose={() => setEditor(null)} onSave={savePreset} />}
    </aside>
  );
}
