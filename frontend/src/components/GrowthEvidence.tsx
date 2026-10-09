import { useEffect, useRef, useState } from "react";
import { graphApi } from "../graphApi";
import type { GraphRecord, GraphRecordPage, GraphSnapshot } from "../graphTypes";
import { budgetText } from "../budgetLimits";
import { graphErrorText } from "../useGrowthWorkbench";
import { graphStatusText } from "./GrowthReading";

export default function GrowthEvidence({ snapshot, diagnostics = false }: { snapshot: GraphSnapshot; diagnostics?: boolean }) {
  const refreshKey = `${snapshot.graph_commit}:${snapshot.readings.map((reading) => reading.read_seq).join(",")}:${snapshot.sessions.map((session) => session.work_seq).join(",")}`;
  if (diagnostics) return <div className="growth-evidence-layout">
    <section className="panel" aria-label="生长运行预算"><p className="eyebrow">USAGE / DIAGNOSTICS</p><h2>调用与预算</h2>
      <dl className="growth-diagnostic-fields">
        <dt>调用</dt><dd>{snapshot.run.usage.calls} / {budgetText(snapshot.run.limits.max_calls)}</dd>
        <dt>预算占用 Token</dt><dd>{(snapshot.run.usage.input_tokens + snapshot.run.usage.output_tokens + (snapshot.run.usage.reserved_tokens ?? 0) + (snapshot.run.usage.unknown_tokens ?? 0)).toLocaleString("zh-CN")} / {budgetText(snapshot.run.limits.max_tokens)}</dd>
        <dt>已知 Token</dt><dd>{(snapshot.run.usage.input_tokens + snapshot.run.usage.output_tokens).toLocaleString("zh-CN")}</dd>
        <dt>输入 / 输出</dt><dd>{snapshot.run.usage.input_tokens.toLocaleString("zh-CN")} / {snapshot.run.usage.output_tokens.toLocaleString("zh-CN")}</dd>
        <dt>进行中预留</dt><dd>{snapshot.run.usage.reserved_tokens ?? 0}</dd>
        <dt>未知用量记账</dt><dd>{snapshot.run.usage.unknown_tokens ?? 0}</dd>
        <dt>失败调用</dt><dd>{snapshot.run.usage.failed_calls ?? 0}</dd>
        <dt>生长窗口</dt><dd>{snapshot.run.usage.windows ?? 0} / {budgetText(snapshot.run.limits.max_windows)}</dd>
      </dl>
    </section>
    <section className="panel" aria-label="独立工作会话"><p className="eyebrow">WORK SESSIONS</p><h2>工作会话</h2>
      {snapshot.sessions.length ? snapshot.sessions.map((session) => <div className="growth-record" key={session.id}><strong>{session.scope_id}</strong><p className="helper">本地工作序号 {session.work_seq} · {graphStatusText(session.status)} · {session.last_action}</p>
        {session.error && <p className="growth-warning">{session.error.message}（{session.error.code}）</p>}
      </div>) : <p className="helper">尚未启动工作会话。</p>}
    </section>
    <RecordPanel runId={snapshot.run.id} kind="events" title="执行事件" refreshKey={refreshKey} />
    <section className="panel" aria-label="动作回执"><p className="eyebrow">ACTION RECEIPTS</p><h2>实际动作</h2>
      {snapshot.receipts.length ? snapshot.receipts.slice(-30).map((record) => <div className="growth-record" key={record.id}><code>{record.id}</code><p>{typeof record.action === "string" ? record.action : record.kind ?? "动作回执"}</p><span className="helper">{record.scope_id} · G{record.created_commit ?? record.graph_commit ?? snapshot.graph_commit}</span></div>) : <p className="helper">尚无动作回执。</p>}
    </section>
  </div>;
  return <div className="growth-evidence-layout">
    <section className="panel growth-evidence-intro"><p className="eyebrow">METHOD / TRIAL / RESPONSE</p><h2>组织方法也可以生长</h2><p className="helper">方法定义、候选试验与局部回应分别保存，记录它们如何改变下一次组织。此处的技术证据与阅读正文分开呈现。</p></section>
    <RecordPanel runId={snapshot.run.id} kind="methods" title="方法版本" refreshKey={refreshKey} />
    <RecordPanel runId={snapshot.run.id} kind="trials" title="候选试验" refreshKey={refreshKey} />
    <RecordPanel runId={snapshot.run.id} kind="responses" title="局部回应" refreshKey={refreshKey} />
  </div>;
}

function RecordPanel({ runId, kind, title, refreshKey }: { runId: string; kind: "methods" | "trials" | "responses" | "events"; title: string; refreshKey: string }) {
  const [page, setPage] = useState<GraphRecordPage | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const epoch = useRef(0);
  useEffect(() => {
    let live = true;
    const current = ++epoch.current;
    setLoading(true);
    setPage(null);
    setError("");
    void graphApi.records(runId, kind).then((next) => { if (live && current === epoch.current) setPage(next); })
      .catch((cause) => { if (live && current === epoch.current) setError(graphErrorText(cause)); })
      .finally(() => { if (live && current === epoch.current) setLoading(false); });
    return () => { live = false; epoch.current += 1; };
  }, [runId, kind, refreshKey]);
  const more = async () => {
    if (page?.next_offset == null || loading) return;
    const current = epoch.current;
    setLoading(true);
    try {
      const next = await graphApi.records(runId, kind, page.next_offset);
      if (current === epoch.current) setPage((previous) => ({ ...next, items: [...(previous?.items ?? []), ...next.items] }));
    } catch (cause) {
      if (current === epoch.current) setError(graphErrorText(cause));
    } finally { if (current === epoch.current) setLoading(false); }
  };
  return <section className="panel growth-record-panel" aria-label={title}><h2>{title}</h2>
    {loading && !page && <p className="helper" role="status">正在读取记录…</p>}
    {error && <p className="message error" role="alert">{error}</p>}
    {page?.items.length === 0 && <p className="helper">当前没有{title}记录。</p>}
    {page?.items.map((record) => <EvidenceRecord key={`${record.id}:${record.version ?? ""}`} record={record} kind={kind} />)}
    {page?.next_offset != null && <button className="text-button" disabled={loading} onClick={() => void more()}>{loading ? "正在读取…" : "载入更多记录"}</button>}
  </section>;
}

function EvidenceRecord({ record, kind }: { record: GraphRecord; kind: string }) {
  const instruction = typeof record.instruction === "string" ? record.instruction : record.purpose;
  return <article className="growth-record">
    <strong>{record.title ?? record.label ?? (kind === "events" && typeof record.action === "string" ? record.action : kind === "events" && typeof record.type === "string" ? record.type : record.id)}</strong>
    <p className="helper">{record.scope_id}{record.status ? ` · ${graphStatusText(record.status)}` : ""}{record.version ? ` · 版本 ${record.version.slice(0, 10)}` : ""}</p>
    {instruction && <p className="growth-record-body">{instruction}</p>}
    {record.text && <p className="growth-record-body">{record.text}</p>}
    {kind === "trials" && <span className="small-tag">{record.adopted === true ? "已采用" : "候选试验"}</span>}
    {record.method_ref && <p className="helper">方法来源 <code>{record.method_ref.object_id}</code> · {record.method_ref.version.slice(0, 10)}</p>}
    {record.evaluation_method_ref && <p className="helper">实际评议方法 <code>{record.evaluation_method_ref.object_id}</code> · {record.evaluation_method_ref.version.slice(0, 10)}</p>}
    {record.program != null && <details><summary>方法程序</summary><pre>{JSON.stringify(record.program, null, 2)}</pre></details>}
  </article>;
}
