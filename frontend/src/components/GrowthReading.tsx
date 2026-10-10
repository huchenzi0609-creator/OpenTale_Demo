import { useEffect, useRef, useState } from "react";
import type { GraphPassage, GraphReading, GraphSnapshot, GraphSourceSpan, GraphVisit } from "../graphTypes";
import { graphErrorText, type GrowthWorkbenchModel } from "../useGrowthWorkbench";
import { graphApi } from "../graphApi";
import { graphNodeLabel } from "./GraphNeighborhood";

export function graphStatusText(status: string) {
  return ({ ready: "可继续", idle: "待命", calling: "模型正在工作", running: "持续运行中", waiting: "等待可读节点", paused: "已暂停", error: "需要恢复", budget_exhausted: "预算已用尽" } as Record<string, string>)[status] ?? "可继续";
}
export function graphActionText(action: string) {
  return ({ ready: "准备开始", query: "已检索材料，可继续选择", read_page: "已读取原文片段", inspect: "已检查组织方法", apply: "已应用组织方法", revise: "已生成方法修订", adopt: "已采用组织方法", try_write: "候选已生成，等待评议与发布", publish: "候选已发布到图世界", respond: "已保存局部回应", reflect: "已评议自身工作方法", navigate: "已进入节点，等待组织阅读", render: "已保存阅读正文", wait: "等待下一步条件" } as Record<string, string>)[action] ?? "工作进度已保存";
}

export default function GrowthReading({ snapshot, model, selectedId, onSelect, onInspect }: {
  snapshot: GraphSnapshot;
  model: GrowthWorkbenchModel;
  selectedId: string;
  onSelect: (id: string) => void;
  onInspect: (nodeId: string, span?: GraphSourceSpan) => void;
}) {
  const [title, setTitle] = useState("新的阅读路径");
  const [entry, setEntry] = useState("");
  const [direction, setDirection] = useState("");
  const [history, setHistory] = useState<{ passages: GraphPassage[]; visits: GraphVisit[]; nextPassage?: number | null; nextVisit?: number | null } | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const historyEpoch = useRef(0);
  const entryNodes = snapshot.nodes.filter((node) => node.private_scope == null);
  const reading = snapshot.readings.find((item) => item.id === selectedId) ?? snapshot.readings[0];
  useEffect(() => {
    historyEpoch.current += 1;
    setHistory(null); setHistoryLoading(false); setHistoryError("");
    return () => { historyEpoch.current += 1; };
  }, [snapshot.run.id, reading?.id]);
  useEffect(() => {
    if (!entryNodes.some((node) => node.id === entry)) setEntry(snapshot.windows[0]?.entry_node_ids.find((id) => entryNodes.some((node) => node.id === id)) ?? entryNodes[0]?.id ?? "");
  }, [snapshot, entry]);
  const label = (nodeId: string) => {
    const node = snapshot.nodes.find((item) => item.id === nodeId);
    return node ? graphNodeLabel(node, snapshot) : "原始节点";
  };
  const session = snapshot.sessions.find((item) => item.scope_id === `reading:${reading?.id}`);
  const active = reading ? snapshot.active_scopes.includes(`reading:${reading.id}`) : false;
  const working = reading && (model.busyLabels.includes(`reading:${reading.id}:step`) || session?.status === "calling");
  const passages = reading ? [...new Map([...(history?.passages ?? []), ...reading.passages].map((passage) => [passage.id, passage])).values()].sort((a, b) => a.read_seq - b.read_seq) : [];
  const visits = reading ? [...new Map([...(history?.visits ?? []), ...reading.visits].map((visit) => [visit.id, visit])).values()].sort((a, b) => a.read_seq - b.read_seq) : [];
  const loadHistory = async () => {
    if (!reading || historyLoading) return;
    const current = historyEpoch.current;
    setHistoryLoading(true); setHistoryError("");
    try {
      const [passagePage, visitPage] = await Promise.all([
        history && history.nextPassage == null ? Promise.resolve(null) : graphApi.passages(snapshot.run.id, reading.id, history?.nextPassage ?? 0),
        history && history.nextVisit == null ? Promise.resolve(null) : graphApi.visits(snapshot.run.id, reading.id, history?.nextVisit ?? 0),
      ]);
      if (current !== historyEpoch.current) return;
      setHistory((previous) => ({ passages: [...(previous?.passages ?? []), ...(passagePage?.items ?? [])], visits: [...(previous?.visits ?? []), ...(visitPage?.items ?? [])], nextPassage: passagePage?.next_offset ?? null, nextVisit: visitPage?.next_offset ?? null }));
    } catch (cause) { if (current === historyEpoch.current) setHistoryError(graphErrorText(cause)); }
    finally { if (current === historyEpoch.current) setHistoryLoading(false); }
  };
  return <div className="growth-reading-layout">
    <section className="panel growth-reading-selector" aria-label="阅读路径管理">
      <div className="growth-section-heading"><div><p className="eyebrow">INDEPENDENT READING</p><h2>阅读路径</h2></div><span className="small-tag">{snapshot.readings.length} 条</span></div>
      <p className="helper">每条路径独立推进。阅读序号与世界版本分别计数，可在相同 G{snapshot.graph_commit} 上继续遍历。</p>
      <div className="growth-reading-tracks">{snapshot.readings.map((track) => <button key={track.id} className={track.id === reading?.id ? "chosen" : ""} aria-pressed={track.id === reading?.id} onClick={() => onSelect(track.id)}>
        <strong>{track.label}</strong><span>阅读 {track.read_seq} · 观察 G{track.observed_graph_commit}</span><small>{graphStatusText(track.status)}</small>
      </button>)}</div>
      <details className="growth-add-reading" open={!snapshot.readings.length}>
        <summary>＋ 添加独立阅读</summary>
        <form onSubmit={(event) => { event.preventDefault(); if (!title.trim() || !entry || model.busy) return; void model.addReading({ title: title.trim(), entry_node_ids: [entry], direction }); }}>
          <label>新阅读名称<input value={title} disabled={!!model.busy} onChange={(event) => setTitle(event.target.value)} /></label>
          <label>阅读起点<select value={entry} disabled={!!model.busy} onChange={(event) => setEntry(event.target.value)}>{entryNodes.map((node) => <option key={node.id} value={node.id}>{graphNodeLabel(node, snapshot)} · {node.id.slice(-6)}</option>)}</select></label>
          {!entryNodes.length && <p className="helper">当前尚无可供这条读轨进入的公开节点，发布叙事节点后可添加阅读。</p>}
          <label>阅读方向（可选）<textarea rows={3} value={direction} disabled={!!model.busy} onChange={(event) => setDirection(event.target.value)} placeholder="可以留空" /></label>
          <button className="button secondary" disabled={!!model.busy || !title.trim() || !entry}>保存阅读路径</button>
        </form>
      </details>
    </section>
    <section className="growth-reader panel" aria-label="连续阅读">
      {reading ? <>
        <div className="growth-section-heading"><div><p className="eyebrow">PATH / {reading.read_seq}</p><h2>{reading.label}</h2></div>
          <div className="controls">
            <button className="button secondary" disabled={model.isBusy(`reading:${reading.id}`) || active || snapshot.run.status === "budget_exhausted"} onClick={() => void model.reading(reading.id, "step")}>{working ? "正在阅读…" : "阅读单步"}</button>
            <button className="button primary" disabled={model.isBusy(`reading:${reading.id}`) || (!active && snapshot.run.status === "budget_exhausted")} onClick={() => void model.reading(reading.id, active ? "pause" : "start")}>{active ? "暂停阅读" : "开始阅读"}</button>
          </div>
        </div>
        <div className="growth-reading-progress" aria-label="阅读进度"><span>阅读序号 <b>{reading.read_seq}</b></span><span>观察世界 <b>G{reading.observed_graph_commit}</b></span><span>当前世界 <b>G{snapshot.graph_commit}</b></span></div>
        {reading.direction && <p className="helper">阅读方向：{reading.direction}</p>}
        <ReadingNotice reading={reading} working={!!working} active={active} />
        {session && !working && <p className="helper growth-work-action" role="status">{graphActionText(session.last_action)} · 本地工作 {session.work_seq}</p>}
        {session?.error && <p role="alert" className="message error">{session.error.message}（{session.error.code}）</p>}
        <div className="growth-passages">
          {passages.map((passage) => <article key={passage.id} className="growth-passage">
            <div className="growth-passage-heading"><span>阅读 {passage.read_seq}</span></div>
            <div className="growth-fiction" role="region" aria-label="生长阅读正文">{passage.text ?? "这段正文尚未载入。"}</div>
            <footer className="growth-passage-sources" aria-label={`阅读 ${passage.read_seq} 原文来源`}>
              {passage.source_spans.map((span, index) => <button key={`${span.node_id}-${index}`} className="text-button" onClick={() => onInspect(span.node_id, span)}>查看原文 · {label(span.node_id)} [{span.start}, {span.end})</button>)}
            </footer>
          </article>)}
        </div>
        {!!visits.length && <details className="growth-path" open><summary>已访问路径 · 已载入 {visits.length} 次</summary><ol>
          {visits.map((visit) => <li key={visit.id}><span>{visit.read_seq}</span><button className="text-button" onClick={() => onInspect(visit.node_id)}>{label(visit.node_id)}</button>
            <small>G{visit.observed_graph_commit} · {visit.via_link_id ? "沿图边进入" : "入口或再次进入"}{visit.transition ? ` · ${visit.transition}` : ""}</small></li>)}
        </ol></details>}
        {reading.cursor_node_id && <button className="text-button growth-cursor" onClick={() => onInspect(reading.cursor_node_id!)}>查看当前游标节点 ↗</button>}
        <div className="growth-history-controls"><p className="helper">当前显示已载入的 {passages.length} 段正文。阅读历史可分批读取，每页最多 100 条。</p>
          {historyError && <p className="message error" role="alert">{historyError}</p>}
          {(!history || history.nextPassage != null || history.nextVisit != null) && <button className="text-button" disabled={historyLoading} onClick={() => void loadHistory()}>{historyLoading ? "正在读取历史…" : history ? "载入更多阅读历史" : "读取阅读历史"}</button>}
        </div>
      </> : <div className="growth-empty"><h3>从一个节点开始阅读</h3><p>保存一条阅读路径后，单步或开始阅读。生长可以单独暂停。</p></div>}
    </section>
  </div>;
}

function ReadingNotice({ reading, working, active }: { reading: GraphReading; working: boolean; active: boolean }) {
  if (working) return <p className="growth-state-note" role="status"><i className="pulse" />模型正在选择节点并组织阅读，已保留此前正文。</p>;
  if (reading.status === "waiting") return <p className="growth-state-note" role="status">正在等待可读节点，阅读进度已保留。可继续生长后再读。</p>;
  if (active) return <p className="growth-state-note" role="status"><i className="pulse" />阅读正在独立推进。</p>;
  if (!reading.passages.length) return <p className="helper">这条路径尚未生成阅读正文。可单步探索当前图世界。</p>;
  return null;
}
