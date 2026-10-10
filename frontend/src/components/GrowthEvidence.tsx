import { useEffect, useRef, useState } from "react";
import { graphApi } from "../graphApi";
import type { GraphObjectRef, GraphRecord, GraphRecordPage, GraphSnapshot } from "../graphTypes";
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
    <section className="panel growth-evidence-intro"><p className="eyebrow">METHOD / TRIAL / RESPONSE</p><h2>组织方法也可以生长</h2><p className="helper">起步方法提供初始方向。修改会生成独立版本，试用与正式采用分别记录。方法版本的增加需要结合实际选材、试写与采用结果判断。</p></section>
    <RecordPanel runId={snapshot.run.id} kind="methods" title="方法版本" refreshKey={refreshKey} snapshot={snapshot} />
    <MethodActions snapshot={snapshot} />
    <RecordPanel runId={snapshot.run.id} kind="trials" title="候选试验" refreshKey={refreshKey} />
    <RecordPanel runId={snapshot.run.id} kind="responses" title="局部回应" refreshKey={refreshKey} />
  </div>;
}

function RecordPanel({ runId, kind, title, refreshKey, snapshot }: { runId: string; kind: "methods" | "trials" | "responses" | "events"; title: string; refreshKey: string; snapshot?: GraphSnapshot }) {
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
    {kind === "methods" && snapshot && <CurrentMethods snapshot={snapshot} records={[...snapshot.methods, ...(page?.items ?? [])]} />}
    {loading && !page && <p className="helper" role="status">正在读取记录…</p>}
    {error && <p className="message error" role="alert">{error}</p>}
    {page?.items.length === 0 && <p className="helper">当前没有{title}记录。</p>}
    {page?.items.map((record) => <EvidenceRecord key={`${record.id}:${record.version ?? ""}`} record={record} kind={kind} adoptedBy={snapshot ? methodOwners(snapshot, record) : []} />)}
    {page?.next_offset != null && <button className="text-button" disabled={loading} onClick={() => void more()}>{loading ? "正在读取…" : "载入更多记录"}</button>}
  </section>;
}

function EvidenceRecord({ record, kind, adoptedBy = [] }: { record: GraphRecord; kind: string; adoptedBy?: string[] }) {
  const instruction = typeof record.instruction === "string" ? record.instruction : record.purpose;
  return <article className="growth-record">
    <strong>{record.title ?? record.label ?? (kind === "events" && typeof record.action === "string" ? record.action : kind === "events" && typeof record.type === "string" ? record.type : record.id)}</strong>
    <p className="helper">{record.scope_id}{record.status ? ` · ${graphStatusText(record.status)}` : ""}{record.version ? ` · 版本 ${record.version.slice(0, 10)}` : ""}</p>
    {kind === "methods" && <div className="growth-method-tags"><span className="small-tag">{record.parent_ref ? "修改版本" : "起步版本"}</span><span className="small-tag">{adoptedBy.length ? `当前采用 · ${adoptedBy.join("、")}` : "未被当前工作位置采用"}</span></div>}
    {instruction && <p className="growth-record-body">{instruction}</p>}
    {record.text && <p className="growth-record-body">{record.text}</p>}
    {kind === "trials" && <span className="small-tag">{record.adopted === true ? "已采用" : "候选试验"}</span>}
    {record.method_ref && <p className="helper">方法来源 <code>{record.method_ref.object_id}</code> · {record.method_ref.version.slice(0, 10)}</p>}
    {record.evaluation_method_ref && <p className="helper">实际评议方法 <code>{record.evaluation_method_ref.object_id}</code> · {record.evaluation_method_ref.version.slice(0, 10)}</p>}
    {kind === "methods" && record.parent_ref && <p className="helper">修改来路 <code>{record.parent_ref.object_id}</code> · {record.parent_ref.version.slice(0, 10)}</p>}
    {kind === "methods" && record.source_refs?.length ? <details><summary>方法依据 · {record.source_refs.length} 个精确对象</summary><ul>{record.source_refs.map((ref, index) => <li key={`${ref.kind}:${ref.object_id}:${ref.version}:${index}`}>{ref.kind} · <code>{ref.object_id}</code> · {ref.version.slice(0, 10)}</li>)}</ul></details> : null}
    {record.program != null && <MethodProgram value={record.program} />}
  </article>;
}

function positions(snapshot: GraphSnapshot) {
  return [...snapshot.windows, ...snapshot.readings].map((actor) => ({ label: actor.label, ref: actor.method_ref }));
}

function sameMethod(ref: GraphObjectRef | null | undefined, record: GraphRecord) {
  return ref?.kind === "method" && ref.object_id === record.id && ref.version === record.version;
}

function methodOwners(snapshot: GraphSnapshot, record: GraphRecord) {
  return positions(snapshot).filter((actor) => sameMethod(actor.ref, record)).map((actor) => actor.label);
}

function CurrentMethods({ snapshot, records }: { snapshot: GraphSnapshot; records: GraphRecord[] }) {
  return <div className="growth-current-methods" aria-label="当前采用方法"><h3>当前采用方法</h3>{positions(snapshot).map(({ label, ref }, index) => {
    const record = records.find((item) => sameMethod(ref, item));
    return <div className="growth-record" key={`${ref?.object_id ?? "none"}:${index}`}><strong>{label}</strong>
      {ref ? <><p className="helper"><code>{ref.object_id}</code> · {ref.version.slice(0, 10)}</p>
        {record ? <><span className="small-tag">{record.parent_ref ? "已采用修改版本" : "仍使用起步版本"}</span>{typeof record.instruction === "string" && <p className="growth-record-body">{record.instruction}</p>}<MethodProgram value={record.program} /></> : <p className="helper">该精确版本正文尚未载入，可继续载入方法历史。</p>}
      </> : <p className="helper">尚未采用方法。</p>}
    </div>;
  })}</div>;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function MethodProgram({ value }: { value: unknown }) {
  const program = objectValue(value);
  if (!program) return null;
  const nodes = Array.isArray(program.node_ids) ? program.node_ids.filter((id): id is string => typeof id === "string") : [];
  const selection = Object.hasOwn(program, "node_ids") ? `选材 ${nodes.length} 个节点` : "动态选材 · 当前来处与前沿";
  const groups = objectValue(program.groups);
  const relations = Array.isArray(program.relations) ? program.relations.map(objectValue).filter((row): row is Record<string, unknown> => row !== null) : [];
  const occurrences = Array.isArray(program.occurrences) ? program.occurrences.length : 0;
  return <div className="growth-method-program"><p className="helper">{selection} · 分组 {Object.keys(groups ?? {}).length} 个 · 关系 {relations.length} 条 · 重复出现 {occurrences} 处</p>
    {typeof program.purpose === "string" && program.purpose && <p className="growth-record-body">本次修改目的：{program.purpose}</p>}
    {groups && Object.keys(groups).length > 0 && <p className="helper">分组：{Object.keys(groups).join("、")}</p>}
    {relations.length > 0 && <ul>{relations.map((relation, index) => <li key={index}>{typeof relation.relation === "string" ? relation.relation : "组织关系"}{typeof relation.source_id === "string" && typeof relation.target_id === "string" ? ` · ${relation.source_id} → ${relation.target_id}` : ""}</li>)}</ul>}
    <details><summary>方法程序</summary><pre>{JSON.stringify(program, null, 2)}</pre></details>
  </div>;
}

function MethodActions({ snapshot }: { snapshot: GraphSnapshot }) {
  const actions = snapshot.events.filter((record) => ["revise", "apply", "adopt"].includes(String(record.action)));
  return <section className="panel" aria-label="近期方法动作"><h2>近期方法动作</h2><p className="helper">区分修改、临时试用与正式采用。此处展示当前快照保留的近期执行记录。</p>
    {actions.length ? actions.map((record) => {
      const details = objectValue(record.details) ?? {};
      const scope = typeof details.scope_id === "string" ? details.scope_id : "";
      const label = snapshot.windows.find((actor) => scope === `window:${actor.id}`)?.label ?? snapshot.readings.find((actor) => scope === `reading:${actor.id}`)?.label ?? scope;
      const action = record.action === "revise" ? "修改方法" : record.action === "apply" ? "临时试用" : "正式采用";
      return <article className="growth-record" key={record.id}><strong>{label} · {action}</strong><p className="helper">G{record.graph_commit ?? record.created_commit ?? 0}{typeof details.work_seq === "number" ? ` · 工作序号 ${details.work_seq}` : ""}</p>
        {typeof details.method_id === "string" && <p className="helper"><code>{details.method_id}</code>{typeof details.version === "string" ? ` · ${details.version.slice(0, 10)}` : ""}</p>}
      </article>;
    }) : <p className="helper">近期没有方法修改、试用或采用动作。</p>}
  </section>;
}
