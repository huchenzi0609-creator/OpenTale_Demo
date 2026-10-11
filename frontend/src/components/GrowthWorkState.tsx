import type { GraphSession } from "../graphTypes";

/** Saved work and outstanding reviews are independent; a review debt is never a completed review. */
export function workPhaseText(session: GraphSession) {
  if (session.phase === "review_pending" || session.review_debt?.some((item) => item.status === "pending" || item.status === "failed")) return "评议待补";
  if (session.phase === "ready_to_publish") return "草稿已保存 · 可发布";
  if (session.phase === "draft_saved") return "草稿已保存";
  return "";
}

export default function GrowthWorkState({ session, diagnostics = false }: { session: GraphSession; diagnostics?: boolean }) {
  const phase = workPhaseText(session);
  const debt = session.review_debt?.filter((item) => item.status === "pending" || item.status === "failed") ?? [];
  if (!phase && !diagnostics) return null;
  return <div className="growth-work-state" aria-label="已保存工作与待补评议">
    {phase && <p className={debt.length || session.phase === "review_pending" ? "helper growth-warning" : "helper"} role="status">{session.saved_draft_ref && phase === "评议待补" ? "草稿已保存 · " : ""}{phase}{debt.length ? ` · ${debt.length} 项` : ""}</p>}
    {(debt.length > 0 || session.phase === "review_pending") && <p className="helper">已保存的结果仍保留，待补评议尚未完成。后续可补做评议，或由明确工作动作继续。</p>}
    {diagnostics && <>
      {session.saved_draft_ref && <p className="helper">保存草稿 · <code>{session.saved_draft_ref.object_id}</code> · {session.saved_draft_ref.version.slice(0, 10)}</p>}
      {session.last_invocation_ref && <p className="helper">最近模型方法调用 · <code>{session.last_invocation_ref.object_id}</code> · {session.last_invocation_ref.version.slice(0, 10)}</p>}
      {debt.length > 0 && <details><summary>待补评议明细 · {debt.length} 项</summary>{debt.map((item) => <div className="growth-record" key={item.id}><p className="helper">{item.status === "failed" ? "评议失败，等待后续工作" : "尚待评议"} · 阶段 {item.stage}</p>
        {item.error && <p className="growth-warning">{item.error.message}（{item.error.code}）</p>}
        <p className="helper">请求 <code>{item.request_id}</code></p>
        <ul>{item.targets.map((ref, index) => <li key={`${ref.kind}:${ref.object_id}:${ref.version}:${index}`}>{ref.kind} · <code>{ref.object_id}</code> · {ref.version.slice(0, 10)}</li>)}</ul>
      </div>)}</details>}
    </>}
  </div>;
}
