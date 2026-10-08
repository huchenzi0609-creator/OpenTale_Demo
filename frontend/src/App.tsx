import { useEffect, useRef } from "react";
import { useWorkbench } from "./useWorkbench";
import Sidebar from "./components/Sidebar";
import StoryReader from "./components/StoryReader";
import Relations from "./components/Relations";
import Diagnostics from "./components/Diagnostics";
import Verification from "./components/Verification";
import ReflectionPanel from "./components/ReflectionPanel";
import { readingTracks } from "./readings";
import { budgetText } from "./budgetLimits";

const STATUS: Record<string, string> = {
  idle: "待命",
  running: "运行中",
  paused: "已暂停",
  error: "需恢复",
  budget_exhausted: "预算已用尽",
};
const PHASE: Record<string, string> = {
  idle: "待命",
  drafting: "共同构想",
  realizing: "恢复续延",
  presenting: "承接呈现",
};
const number = (value: number) => new Intl.NumberFormat("zh-CN").format(value);

export default function App() {
  const workbench = useWorkbench();
  const {
    snapshot,
    selectedRunId,
    selectedPosition,
    setSelectedPosition,
    tab,
    setTab,
    booting,
    busy,
    error,
    setError,
    notice,
    pollError,
    verification,
    setVerification,
    deleteArmed,
    setDeleteArmed,
    active,
    disabled,
    frozen,
    exhausted,
    tokenUsage,
    forkRun,
    deleteRun,
    verify,
    reconnect,
    runAction,
    runControlFocusRequest,
  } = workbench;
  const runControls = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!runControlFocusRequest || !runControls.current) return;
    runControls.current.focus({ preventScroll: true });
    runControls.current.scrollIntoView?.({ block: "start", behavior: "auto" });
  }, [runControlFocusRequest]);
  return (
    <div className="app-shell">
      <Sidebar model={workbench} />

      <main id="main" className="workspace">
        <header className="page-header">
          <div>
            <p className="eyebrow">EXPERIMENT / SHARED EXPRESSION</p>
            <h1>
              让世界共同发生<span>。</span>
            </h1>
            <p className="subtitle">
              沿正在生效的关系继续，而非把独立故事拼在一起。
            </p>
          </div>
          <div className="version-pill">
            <span />
            可验证 Demo
          </div>
        </header>
        {booting && (
          <div className="message" role="status">
            正在连接本地实验服务…
          </div>
        )}
        {error && (
          <div className="message error" role="alert">
            <strong>操作未完成</strong>
            <span>{error}</span>
            <button
              className="text-button"
              disabled={disabled}
              onClick={() => void reconnect()}
            >
              重新连接
            </button>
            <button aria-label="关闭错误" onClick={() => setError("")}>
              ×
            </button>
          </div>
        )}
        {pollError && (
          <div className="message warning" role="status">
            {pollError}
          </div>
        )}
        {notice && (
          <div className="message" role="status">
            {notice}
          </div>
        )}
        {snapshot?.run.last_error && (
          <div className="message warning" role="status">
            <strong>实验保留了恢复点</strong>
            <span>
              {snapshot.run.last_error.message}（{snapshot.run.last_error.code}
              ）。
              {snapshot.pending
                ? "已有阶段结果保留，单步或开始将继续未完成阶段。"
                : "请检查后再继续。"}
            </span>
          </div>
        )}

        {!snapshot ? (
          <section className="welcome panel">
            <div className="welcome-symbol">
              <span />
              <span />
              <span />
            </div>
            <p className="eyebrow">BEGIN WITH A RELATION</p>
            <h2>{selectedRunId ? "正在加载实验" : "从一个共同表达开始"}</h2>
            <p>
              选择初始叙事，创建一个离线实验。观察同一份表达如何在不同位置取得不同作用，再验证来源的变化是否真正改变下一次生成。
            </p>
            <div className="welcome-steps">
              <div>
                <b>01</b>
                <strong>共同构想</strong>
                <span>同一版本的局部窗口</span>
              </div>
              <div>
                <b>02</b>
                <strong>改变条件</strong>
                <span>多方变化共同安装</span>
              </div>
              <div>
                <b>03</b>
                <strong>继续与呈现</strong>
                <span>从更新后的关系展开</span>
              </div>
            </div>
            <button
              className="button secondary"
              disabled={disabled}
              onClick={() => void verify()}
            >
              {busy === "离线验证" ? "正在验证…" : "运行离线机制验证"}
            </button>
          </section>
        ) : (
          <>
            <section className="run-toolbar panel" aria-label="运行控制" tabIndex={-1} ref={runControls}>
              <div className="run-identity">
                <p className="eyebrow">
                  {snapshot.run.provider === "fixture"
                    ? "OFFLINE FIXTURE"
                    : "LIVE MODEL"}
                  {snapshot.run.fork_of ? " / FORK" : ""}
                </p>
                <h2>{snapshot.run.title}</h2>
                <span className="small-tag run-mode" aria-label="当前实验模式">
                  {snapshot.run.mode === "reflexive_v2" ? "反身实验 v2" : snapshot.run.mode === "reflexive" ? "反身实验" : "经典模式"}
                </span>
                <span className={`status status-${snapshot.run.status}`}>
                  <i />
                  {STATUS[snapshot.run.status]}
                </span>
              </div>
              <div className="controls">
                <button
                  className="button secondary"
                  disabled={frozen || exhausted}
                  onClick={() => void runAction("step")}
                >
                  {busy === "执行局部窗口"
                    ? "正在执行…"
                    : snapshot.pending
                      ? "恢复一个窗口"
                      : "单步"}
                </button>
                {active ? (
                  <button
                    className="button primary"
                    disabled={disabled}
                    onClick={() => void runAction("pause")}
                  >
                    暂停
                  </button>
                ) : (
                  <button
                    className="button primary"
                    disabled={disabled || exhausted}
                    onClick={() => void runAction("start")}
                  >
                    开始运行
                  </button>
                )}
                <button
                  className="icon-button"
                  title="独立分支"
                  aria-label="创建独立分支"
                  disabled={frozen}
                  onClick={() => void forkRun()}
                >
                  ⑂
                </button>
                <a
                  className="icon-button"
                  title="导出实验"
                  aria-label="导出实验"
                  href={`/api/runs/${encodeURIComponent(snapshot.run.id)}/export`}
                  download
                >
                  ↓
                </a>
              </div>
            </section>
            <section className="metrics" aria-label="运行状态">
              <div>
                <span>当前版本</span>
                <strong>R{snapshot.run.revision}</strong>
              </div>
              <div>
                <span>运行阶段</span>
                <strong className="phase-value">
                  {PHASE[snapshot.run.phase]}
                  {active && <i className="pulse" />}
                </strong>
              </div>
              <div>
                <span>窗口预算</span>
                <strong>
                  {snapshot.run.usage.windows ?? 0}
                  <small> / {budgetText(snapshot.run.limits.max_windows)}</small>
                </strong>
              </div>
              <div>
                <span>调用预算</span>
                <strong>
                  {snapshot.run.usage.calls}
                  <small> / {budgetText(snapshot.run.limits.max_calls)}</small>
                </strong>
                {snapshot.run.limits.max_calls !== null && <progress
                  value={snapshot.run.usage.calls}
                  max={snapshot.run.limits.max_calls}
                  aria-label="调用预算使用量"
                />}
              </div>
              <div>
                <span>Token 预算占用</span>
                <strong>
                  {number(tokenUsage)}
                  <small> / {budgetText(snapshot.run.limits.max_tokens)}</small>
                </strong>
              </div>
            </section>
            <p className="budget-note">
              <a href="#experiment-budget">调整本实验预算 ↗</a>
              {active && <span> · 请先暂停运行。</span>}
            </p>
            {!!(
              (snapshot.run.usage.reserved_tokens ?? 0) +
              (snapshot.run.usage.unknown_tokens ?? 0) +
              (snapshot.run.usage.failed_calls ?? 0)
            ) && (
              <p className="budget-note">
                已知用量{" "}
                {number(
                  snapshot.run.usage.input_tokens +
                    snapshot.run.usage.output_tokens,
                )}{" "}
                · 进行中预留 {number(snapshot.run.usage.reserved_tokens ?? 0)} ·
                未知用量保守记账{" "}
                {number(snapshot.run.usage.unknown_tokens ?? 0)} · 失败请求{" "}
                {snapshot.run.usage.failed_calls ?? 0}
                。预算占用包含预留与未知用量。
              </p>
            )}
            {snapshot.pending && (
              <div className="pending-bar" role="status">
                <span className="pending-mark">◌</span>
                <span>
                  局部窗口 R{snapshot.pending.base_revision} 正在处理中
                </span>
                <small>
                  构想 {Object.keys(snapshot.pending.proposals).length}/
                  {snapshot.positions.length} · 续延{" "}
                  {Object.keys(snapshot.pending.realizations).length}/
                  {snapshot.positions.length} ·{" "}
                  {snapshot.pending.committed ? "已共同安装" : "等待共同安装"}
                  {snapshot.pending.committed && <> · 呈现 {readingTracks(snapshot).filter((reading) => reading.enabled && Object.hasOwn(snapshot.pending!.presentations ?? {}, reading.id)).length}/{readingTracks(snapshot).filter((reading) => reading.enabled).length}</>}
                </small>
              </div>
            )}
            <nav className="tabs" aria-label="实验视图">
              {(
                [
                  ["story", "连续阅读"],
                  ["structure", "共同关系"],
                  ["diagnostics", "验证与诊断"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  className={tab === value ? "selected" : ""}
                  aria-pressed={tab === value}
                  onClick={() => setTab(value)}
                >
                  {label}
                  {value === "story" && <span>{readingTracks(snapshot).reduce((total, reading) => total + reading.story.length, 0)}</span>}
                </button>
              ))}
            </nav>

            {(snapshot.run.mode === "reflexive" || snapshot.run.mode === "reflexive_v2") && <nav className="reflection-tab" aria-label="反身实验技术视图">
              <button className="button secondary" aria-pressed={tab === "reflection"}
                onClick={() => setTab("reflection")}>生成方法与实际选择</button>
              <span className="helper">技术证据与连续阅读分开呈现。</span>
            </nav>}

            {tab === "story" && <StoryReader snapshot={snapshot} model={workbench} />}
            {tab === "structure" && (
              <Relations
                snapshot={snapshot}
                selectedPosition={selectedPosition}
                onSelect={setSelectedPosition}
              />
            )}
            {tab === "diagnostics" && (
              <Diagnostics snapshot={snapshot} model={workbench} />
            )}
            {tab === "reflection" && (snapshot.run.mode === "reflexive" || snapshot.run.mode === "reflexive_v2") &&
              <ReflectionPanel key={snapshot.run.id} snapshot={snapshot} model={workbench} />}
            <section className="run-footer">
              <button
                className="text-button"
                disabled={disabled}
                onClick={() => void verify()}
              >
                运行离线机制验证 ↗
              </button>
              <div className="delete-controls">
                {deleteArmed ? (
                  <>
                    <span>将永久删除这份本地存档。</span>
                    <button
                      className="text-button"
                      onClick={() => setDeleteArmed(false)}
                      disabled={disabled}
                    >
                      取消
                    </button>
                    <button
                      className="text-button danger"
                      disabled={frozen}
                      onClick={() => void deleteRun()}
                    >
                      确认删除
                    </button>
                  </>
                ) : (
                  <button
                    className="text-button danger"
                    disabled={frozen}
                    onClick={() => setDeleteArmed(true)}
                  >
                    删除本地存档
                  </button>
                )}
              </div>
            </section>
          </>
        )}
        {verification && (
          <Verification
            verification={verification}
            onClose={() => setVerification(null)}
          />
        )}
        <footer className="page-footer">
          <span>OpenTale · 可执行的共同生成</span>
          <span>本地存储 / 公开正文与技术诊断分离</span>
        </footer>
      </main>
    </div>
  );
}
