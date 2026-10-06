import type { Snapshot } from "../types";
import type { Workbench } from "../useWorkbench";
const EVENTS: Record<string, string> = {
  created: "创建",
  window_started: "打开局部窗口",
  proposal_ready: "构想就绪",
  format_retry: "格式纠错重试",
  batch_installed: "共同安装",
  continuation_resumed: "恢复续延",
  presented: "呈现",
  paused: "暂停",
  error: "安全中止",
  budget_exhausted: "达到预算",
  forked: "独立分支",
  reading_created: "创建阅读路径",
  reading_updated: "更新阅读设置",
};

export default function Diagnostics({
  snapshot,
  model,
}: {
  snapshot: Snapshot;
  model: Workbench;
}) {
  const {
    frozen,
    active,
    selectedPosition,
    setSelectedPosition,
    counterSource,
    setCounterSource,
    compare,
    busy,
    counterfactual,
    replayVerification,
    replay,
  } = model;
  return (
    <div className="diagnostic-grid">
      <section className="panel">
        <div className="section-header">
          <div>
            <p className="eyebrow">COUNTERFACTUAL / OFFLINE ONLY</p>
            <h2>移除来源，条件会改变吗？</h2>
          </div>
        </div>
        <p className="helper">
          在独立副本中移除一个来源，不修改原存档，不调用付费模型。比较输入条件指纹及夹具续延；这不是实际模型语义验收。
        </p>
        <div className="counter-controls">
          <label>
            目标位置
            <select
              aria-label="反事实目标位置"
              value={selectedPosition}
              disabled={frozen}
              onChange={(event) => setSelectedPosition(event.target.value)}
            >
              {snapshot.positions.map((position) => (
                <option key={position.id} value={position.id}>
                  {position.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            移除来源
            <select
              aria-label="反事实来源位置"
              value={counterSource}
              disabled={frozen}
              onChange={(event) => setCounterSource(event.target.value)}
            >
              {snapshot.positions
                .filter((position) => position.id !== selectedPosition)
                .map((position) => (
                  <option key={position.id} value={position.id}>
                    {position.name}
                  </option>
                ))}
            </select>
          </label>
        </div>
        <button
          className="button secondary"
          disabled={frozen || !counterSource || !selectedPosition}
          onClick={() => void compare()}
        >
          {busy === "反事实检验" ? "正在比较…" : "执行离线反事实"}
        </button>
        {active && (
          <p className="helper">暂停实验后，可针对一个稳定快照进行离线比较。</p>
        )}
        {counterfactual && (
          <div className="counter-result">
            <div className="check-status">
              {counterfactual.structural_effect
                ? "✓ 条件发生变化"
                : "○ 条件未发生变化"}
              <span>
                {counterfactual.semantic_fixture_effect
                  ? "夹具续延也发生变化"
                  : "夹具续延没有变化"}
              </span>
            </div>
            <p className="helper">{counterfactual.note}</p>
            <details>
              <summary>输入指纹与受影响关系</summary>
              <code className="fingerprint">
                原始：{counterfactual.baseline_fingerprint}
              </code>
              <code className="fingerprint">
                移除后：{counterfactual.changed_fingerprint}
              </code>
              <p>
                {counterfactual.changed_use_ids.join("、") ||
                  "没有受影响的使用关系"}
              </p>
            </details>
            <div className="comparison">
              <div>
                <h3>原始续延</h3>
                <p>{counterfactual.baseline_text}</p>
              </div>
              <div>
                <h3>移除来源后</h3>
                <p>{counterfactual.changed_text}</p>
              </div>
            </div>
          </div>
        )}
      </section>
      <section className="panel">
        <div className="section-header">
          <div>
            <p className="eyebrow">EXECUTION TRACE</p>
            <h2>执行记录</h2>
          </div>
          <span className="count-pill">{snapshot.events.length}</span>
        </div>
        <p className="helper">诊断视图保留技术证据；正文不混入这些来源标记。</p>
        <div className="replay-panel" aria-label="记录重执行验证">
          <h3>重执行已记录的表达程序</h3>
          <p className="helper">
            在独立状态中重执行存档记录，核对关系、续延和阅读状态。不修改原存档，不调用模型。
          </p>
          <button
            className="button secondary"
            disabled={frozen}
            onClick={() => void replay()}
          >
            {busy === "记录重执行" ? "正在重执行…" : "验证记录重执行"}
          </button>
          {active && <p className="helper">暂停实验后，可验证当前稳定版本。</p>}
          {replayVerification && (
            <div className="replay-result" role="status">
              <strong
                className={
                  replayVerification.ok ? "replay-pass" : "replay-fail"
                }
              >
                {replayVerification.ok
                  ? "✓ 记录重执行一致"
                  : "× 记录重执行发现差异"}
              </strong>
              <p className="helper">{replayVerification.note}</p>
              <code className="fingerprint">
                状态摘要：{replayVerification.digest?.slice(0, 16) ?? "不可用"}
              </code>
              <ul className="replay-checks">
                {replayVerification.checks.map((check) => (
                  <li
                    key={check.name}
                    className={check.passed ? "replay-pass" : "replay-fail"}
                  >
                    <span>{check.passed ? "✓" : "×"}</span>
                    {check.name}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
        <div className="event-list">
          {[...snapshot.events].reverse().map((event) => (
            <details className="event" key={event.id}>
              <summary>
                <span className="event-dot" />
                <strong>{EVENTS[event.type] ?? event.type}</strong>
                <small>R{event.revision}</small>
              </summary>
              <pre>{JSON.stringify(event.data, null, 2)}</pre>
            </details>
          ))}
        </div>
        <details className="audit-details">
          <summary>共同安装审计</summary>
          <pre>{JSON.stringify(snapshot.audit, null, 2)}</pre>
        </details>
      </section>
    </div>
  );
}
