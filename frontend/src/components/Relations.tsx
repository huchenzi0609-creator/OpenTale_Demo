import Graph from "../Graph";
import type { Snapshot } from "../types";
interface Props {
  snapshot: Snapshot;
  selectedPosition: string;
  onSelect: (id: string) => void;
}
export default function Relations({
  snapshot,
  selectedPosition,
  onSelect,
}: Props) {
  const positionName = (id: string) =>
    snapshot.positions.find((item) => item.id === id)?.name ?? id;
  const chosenUses = snapshot.uses.filter(
    (item) => item.position_id === selectedPosition,
  );
  const continuation = snapshot.continuations.find(
    (item) => item.position_id === selectedPosition,
  );
  return (
    <div className="structure-grid">
      <section className="panel relation-panel">
        <div className="section-header">
          <div>
            <p className="eyebrow">OPERATIVE DEPENDENCIES</p>
            <h2>谁参与了这里的继续</h2>
          </div>
          <span className="count-pill">{snapshot.positions.length} 个位置</span>
        </div>
        <Graph
          snapshot={snapshot}
          selected={selectedPosition}
          onSelect={onSelect}
        />
        <div className="position-buttons">
          {snapshot.positions.map((position) => (
            <button
              key={position.id}
              aria-pressed={selectedPosition === position.id}
              className={selectedPosition === position.id ? "selected" : ""}
              onClick={() => onSelect(position.id)}
            >
              {position.name}
            </button>
          ))}
        </div>
        <div className="relation-counts">
          <div>
            <strong>{snapshot.audit.shared_expressions.length}</strong>
            <span>共享表达</span>
          </div>
          <div>
            <strong>{snapshot.audit.effective_edges.length}</strong>
            <span>有效依赖</span>
          </div>
          <div>
            <strong>{snapshot.audit.joint_targets.length}</strong>
            <span>多方共同目标</span>
          </div>
        </div>
      </section>
      <section className="panel condition-panel">
        <div className="section-header">
          <div>
            <p className="eyebrow">SCOPED CONDITIONS</p>
            <h2>{positionName(selectedPosition)}的当前条件</h2>
          </div>
        </div>
        {chosenUses.length ? (
          chosenUses.map((use) => (
            <article className="use-card" key={use.id}>
              <div className="use-heading">
                <strong>{use.role}</strong>
                <small>R{use.revision}</small>
              </div>
              <p className="expression-text">
                {snapshot.expressions.find(
                  (expression) => expression.id === use.expression_id,
                )?.text ?? "表达暂不可用"}
              </p>
              <div className="binding-values">
                {use.bindings.length ? (
                  use.bindings.map((binding, index) => (
                    <p key={`${binding.source_position_id}-${index}`}>
                      {binding.value}
                    </p>
                  ))
                ) : (
                  <p className="helper">当前尚无外部绑定。</p>
                )}
              </div>
              <details>
                <summary>使用关系与来源</summary>
                <code>
                  {use.id} · {use.expression_id}
                </code>
                {use.bindings.map((binding, index) => (
                  <dl key={index}>
                    <dt>来源位置</dt>
                    <dd>{positionName(binding.source_position_id)}</dd>
                    <dt>来源表达</dt>
                    <dd>
                      <code>{binding.source_expression_id}</code>
                    </dd>
                    <dt>锚点</dt>
                    <dd>{binding.anchor}</dd>
                  </dl>
                ))}
              </details>
            </article>
          ))
        ) : (
          <div className="empty-state">
            <p>此位置没有活跃使用关系。</p>
          </div>
        )}
        {continuation && (
          <div className="continuation">
            <h3>续延状态</h3>
            <p>
              恢复 {continuation.resume_count} 次 · 条件版本 R
              {continuation.frame_revision}
            </p>
            <label>
              实际条件指纹
              <code className="fingerprint">
                {continuation.condition_fingerprint}
              </code>
            </label>
            <details>
              <summary>查看当前续延</summary>
              <p>{continuation.surface || continuation.seed_text}</p>
              <p className="helper">
                依赖：{continuation.dependencies.join("、") || "无"}
              </p>
            </details>
          </div>
        )}
      </section>
    </div>
  );
}
