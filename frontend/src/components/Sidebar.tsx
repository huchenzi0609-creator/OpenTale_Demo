import type { Provider } from "../types";
import type { Workbench } from "../useWorkbench";
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
  const {
    config,
    presets,
    runs,
    presetId,
    setPresetId,
    provider,
    setProvider,
    limits,
    setLimits,
    selectedRunId,
    booting,
    busy,
    anyActive,
    preset,
    disabled,
    validLimits,
    createRun,
    reloadConfig,
    reloadLibrary,
    selectRun,
  } = model;
  return (
    <aside className="sidebar">
      <a className="brand" href="#main" aria-label="OpenTale 进入实验">
        <span className="brand-mark">
          O<span>↗</span>
        </span>
        <span>
          OpenTale<small>NARRATIVE LAB / 0.1</small>
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
              {item.name}
            </option>
          ))}
        </select>
        {preset && <p className="helper">{preset.description}</p>}
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
        <div className="budget-grid">
          {(
            [
              ["max_calls", "调用上限", 200],
              ["max_tokens", "Token 上限", 500000],
              ["max_parallel", "并行位置", 6],
              ["max_windows", "窗口上限", 100],
            ] as const
          ).map(([field, label, maximum]) => (
            <label key={field} htmlFor={field}>
              {label}
              <input
                id={field}
                type="number"
                min="1"
                max={maximum}
                value={limits[field]}
                disabled={disabled}
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
        {!validLimits && (
          <p className="helper">
            预算须为范围内的正整数：调用 ≤ 200，Token ≤ 500,000，并行 ≤ 6，窗口
            ≤ 100。
          </p>
        )}
        <button
          className="button primary full"
          disabled={
            disabled ||
            booting ||
            !presetId ||
            !validLimits ||
            (provider === "deepseek" && !config?.configured)
          }
          onClick={() => void createRun()}
        >
          {busy === "创建实验" ? "正在创建…" : "＋ 创建实验"}
        </button>
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
          <code className="file-path">{KEY_PATH}</code>
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
    </aside>
  );
}
