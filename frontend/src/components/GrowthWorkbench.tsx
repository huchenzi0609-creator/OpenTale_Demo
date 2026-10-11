import { useEffect, useRef, useState } from "react";
import { graphApi } from "../graphApi";
import type { GraphNode, GraphProjection, GraphSourceSpan } from "../graphTypes";
import { graphErrorText, useGrowthWorkbench } from "../useGrowthWorkbench";
import GraphNeighborhood from "./GraphNeighborhood";
import GrowthSetup from "./GrowthSetup";
import GrowthReading, { graphActionText, graphStatusText } from "./GrowthReading";
import GrowthEvidence from "./GrowthEvidence";
import GrowthWorkState, { workPhaseText } from "./GrowthWorkState";

/** Source offsets use Unicode code points, matching the immutable backend body. */
export function sourceTextParts(text: string, span?: GraphSourceSpan) {
  if (!span) return { before: text, selected: "", after: "" };
  const points = Array.from(text);
  return { before: points.slice(0, span.start).join(""), selected: points.slice(span.start, span.end).join(""), after: points.slice(span.end).join("") };
}

export default function GrowthWorkbench() {
  const model = useGrowthWorkbench();
  const { snapshot } = model;
  const [tab, setTab] = useState<"reading" | "graph" | "methods" | "diagnostics">("reading");
  const [readingId, setReadingId] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [graph, setGraph] = useState<GraphProjection | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphError, setGraphError] = useState("");
  const [original, setOriginal] = useState<{ node: GraphNode; text: string; span?: GraphSourceSpan } | null>(null);
  const [originalLoading, setOriginalLoading] = useState(false);
  const [originalError, setOriginalError] = useState("");
  const originalEpoch = useRef(0);
  const graphEpoch = useRef(0);
  const sourceHighlight = useRef<HTMLElement>(null);
  const runId = snapshot?.run.id;
  useEffect(() => { sourceHighlight.current?.scrollIntoView?.({ block: "center", behavior: "auto" }); }, [original]);
  useEffect(() => () => { originalEpoch.current += 1; graphEpoch.current += 1; }, []);

  useEffect(() => {
    originalEpoch.current += 1;
    setOriginal(null);
    setOriginalLoading(false);
    setOriginalError("");
    setGraph(null);
    setNodeId("");
  }, [runId]);
  useEffect(() => {
    if (!snapshot) return;
    if (!snapshot.readings.some((reading) => reading.id === readingId)) setReadingId(snapshot.readings[0]?.id ?? "");
    if (!snapshot.nodes.some((node) => node.id === nodeId) && !graph?.nodes.some((node) => node.id === nodeId)) setNodeId(snapshot.windows[0]?.working_node_id ?? snapshot.nodes[0]?.id ?? "");
  }, [snapshot, readingId, nodeId, graph]);

  useEffect(() => {
    if (!runId || tab !== "graph") return;
    let live = true;
    const current = ++graphEpoch.current;
    setGraphLoading(true);
    setGraphError("");
    void graphApi.graph(runId).then((next) => { if (live && current === graphEpoch.current) setGraph(next); })
      .catch((cause) => { if (live && current === graphEpoch.current) setGraphError(graphErrorText(cause)); })
      .finally(() => { if (live && current === graphEpoch.current) setGraphLoading(false); });
    return () => { live = false; graphEpoch.current += 1; };
  }, [runId, snapshot?.graph_commit, tab]);

  const inspect = async (id: string, span?: GraphSourceSpan) => {
    if (!runId) return;
    const current = ++originalEpoch.current;
    setNodeId(id);
    setOriginal(null);
    setOriginalError("");
    setOriginalLoading(true);
    try {
      const next = await graphApi.node(runId, id, span?.node_version);
      if (current === originalEpoch.current) setOriginal({ ...next, span });
    } catch (cause) {
      if (current === originalEpoch.current) setOriginalError(graphErrorText(cause));
    } finally { if (current === originalEpoch.current) setOriginalLoading(false); }
  };
  const moreGraph = async () => {
    if (!runId || graph?.next_offset == null || graphLoading) return;
    const current = graphEpoch.current;
    setGraphLoading(true);
    try {
      const next = await graphApi.graph(runId, graph.next_offset);
      if (current === graphEpoch.current) setGraph((previous) => ({ ...next,
        nodes: [...new Map([...(previous?.nodes ?? []), ...next.nodes].map((node) => [node.id, node])).values()],
        links: [...new Map([...(previous?.links ?? []), ...next.links].map((link) => [link.id, link])).values()],
      }));
    } catch (cause) { if (current === graphEpoch.current) setGraphError(graphErrorText(cause)); }
    finally { if (current === graphEpoch.current) setGraphLoading(false); }
  };
  const growthActive = snapshot?.active_scopes.includes("growth") ?? false;
  const growthWorking = model.busyLabels.includes("growth:step") || snapshot?.windows.some((window) => window.status === "calling");
  const selectedReading = snapshot?.readings.find((reading) => reading.id === readingId);
  const blockedWindows = snapshot?.windows.filter((window) => window.status === "error" || snapshot.sessions.some((session) => session.scope_id === `window:${window.id}` && session.error)) ?? [];
  const parts = original ? sourceTextParts(original.text, original.span) : null;

  return <div className="app-shell growth-shell">
    <GrowthSetup model={model} />
    <main className="workspace growth-workspace" id="growth-main">
      <header className="page-header"><div><p className="eyebrow">GROW A WORLD / READ A PATH</p><h1>世界生长，阅读穿行<span>。</span></h1><p className="subtitle">多个窗口共同长出图世界，每条阅读沿自己的路径继续。</p></div><div className="version-pill"><span />实验模式</div></header>
      {model.booting && <p className="message" role="status">正在连接生长与遍历服务…</p>}
      {model.error && <div className="message error" role="alert"><span>{model.error}</span><button aria-label="关闭生长错误" onClick={() => model.setError("")}>×</button></div>}
      {model.pollError && <p className="message warning" role="status">{model.pollError}</p>}
      {!snapshot ? <section className="panel growth-welcome"><p className="eyebrow">START FROM SEEDS</p><h2>{model.selectedRunId ? "正在加载图世界" : "让种子成为共同的世界"}</h2><p>编辑一个或多个生成窗口，或导入已有初始叙事。创建后，分别控制生长与阅读，查看原始节点和实际经过的关系。</p><div className="welcome-steps"><div><b>01</b><strong>保存种子</strong><span>完整 Markdown 与共同入口</span></div><div><b>02</b><strong>世界生长</strong><span>节点、关联与多方来路</span></div><div><b>03</b><strong>独立遍历</strong><span>多个游标与阅读历史</span></div></div></section> : <>
        <section className="run-toolbar panel" aria-label="世界生长控制">
          <div className="run-identity"><p className="eyebrow">{snapshot.run.provider === "fixture" ? "OFFLINE FIXTURE" : "LIVE MODEL"} / GRAPH WORLD</p><h2>{snapshot.run.title}</h2><span className="small-tag">世界 G{snapshot.graph_commit}</span><span className="small-tag">{snapshot.counts?.nodes ?? snapshot.nodes.length} 节点 · {snapshot.counts?.links ?? snapshot.links.length} 条边</span><span className="small-tag">整体状态 · {graphStatusText(snapshot.run.status)}</span></div>
          <div className="controls"><button className="button secondary" disabled={model.isBusy("growth") || growthActive || snapshot.run.status === "budget_exhausted"} onClick={() => void model.growth("step")}>{growthWorking ? "正在生长…" : "生长单步"}</button>
            <button className="button primary" disabled={model.isBusy("growth") || (!growthActive && snapshot.run.status === "budget_exhausted")} onClick={() => void model.growth(growthActive ? "pause" : "start")}>{growthActive ? "暂停生长" : "开始生长"}</button>
            <button className="text-button" disabled={!!model.busy} onClick={() => void model.refresh()}>刷新状态</button>
          </div>
        </section>
        {blockedWindows.length > 0 && <p className="message warning" role="status">{blockedWindows.length} 个生长窗口需要恢复。各窗口已保存的工作仍保留，阅读沿独立路径继续；阅读成功不会消除窗口的暂停。</p>}
        {growthWorking && <p role="status" className="growth-state-note"><i className="pulse" />模型正在窗口内组织下一步，已有节点与阅读保留。</p>}
        {growthActive && !growthWorking && <p role="status" className="growth-state-note">世界正在独立生长。阅读可同时开始、暂停或单步。</p>}
        <section className="growth-windows" aria-label="生成窗口状态">{snapshot.windows.map((window) => {
          const session = snapshot.sessions.find((item) => item.scope_id === `window:${window.id}`);
          return <article key={window.id} className="panel growth-window-card"><div><h3>{window.label}</h3><span>{graphStatusText(session?.error ? "error" : window.status)}</span></div><p className="helper">本地版本 {window.revision} · {window.entry_node_ids.length} 个入口 · {window.frontier_node_ids.length} 个前沿</p>{session && <>{!workPhaseText(session) && <p className="growth-work-action">{graphActionText(session.last_action)}</p>}<GrowthWorkState session={session} /></>}{session?.error && <p className="growth-warning" role="alert">{session.error.message}（{session.error.code}）</p>}{window.direction && <p className="growth-window-direction">{window.direction}</p>}<div className="growth-entry-nodes">{window.entry_node_ids.map((id) => <button key={id} className="text-button" onClick={() => void inspect(id)}>{snapshot.windows.filter((other) => other.entry_node_ids.includes(id)).length > 1 ? "共同种子原文" : "窗口种子原文"} ↗</button>)}</div>{window.working_node_id && <button className="text-button" onClick={() => void inspect(window.working_node_id!)}>当前工作节点 ↗</button>}</article>;
        })}</section>
        <nav className="tabs" aria-label="生长实验视图">{([ ["reading", "连续阅读"], ["graph", "世界图谱"], ["methods", "方法、试验与回应"], ["diagnostics", "运行诊断"] ] as const).map(([value, label]) => <button key={value} aria-pressed={tab === value} className={tab === value ? "selected" : ""} onClick={() => setTab(value)}>{label}</button>)}</nav>
        {tab === "reading" && <GrowthReading snapshot={snapshot} model={model} selectedId={readingId} onSelect={setReadingId} onInspect={(id, span) => void inspect(id, span)} />}
        {tab === "graph" && <>
          {graphError && <p className="message error" role="alert">{graphError}</p>}
          <GraphNeighborhood snapshot={snapshot} nodes={graph?.nodes ?? snapshot.nodes} links={graph?.links ?? snapshot.links} selectedId={nodeId} visitedIds={selectedReading?.visits.map((visit) => visit.node_id) ?? []} visitedLinkIds={selectedReading?.visits.flatMap((visit) => visit.via_link_id ? [visit.via_link_id] : []) ?? []} onSelect={(id) => void inspect(id)} />
          {graph?.next_offset != null && <button className="button secondary" disabled={graphLoading} onClick={() => void moreGraph()}>{graphLoading ? "正在读取节点…" : "载入更多图节点"}</button>}
        </>}
        {tab === "methods" && <GrowthEvidence snapshot={snapshot} />}
        {tab === "diagnostics" && <GrowthEvidence snapshot={snapshot} diagnostics />}
        {(original || originalLoading || originalError) && <section className="panel growth-original" aria-label="节点原文">
          <div className="growth-section-heading"><div><p className="eyebrow">ORIGINAL NODE / SOURCE</p><h2>原始节点全文</h2></div><button className="text-button" onClick={() => { originalEpoch.current += 1; setOriginal(null); setOriginalError(""); setOriginalLoading(false); }}>关闭原文</button></div>
          {originalLoading && <p role="status" className="helper">正在读取完整节点…</p>}{originalError && <p role="alert" className="message error">{originalError}</p>}
          {original && <><p className="helper"><code>{original.node.id}</code> · 初次安装 G{original.node.created_commit} · {original.node.text_ref.length.toLocaleString("zh-CN")} 字{original.span ? ` · 来源范围 [${original.span.start}, ${original.span.end})` : ""}</p><div className="growth-original-body">{parts?.before}{parts?.selected && <mark ref={sourceHighlight}>{parts.selected}</mark>}{parts?.after}</div></>}
        </section>}
      </>}
      <footer className="page-footer"><span>OpenTale · 生长与遍历</span><span>世界提交与阅读序号独立 / 原文可追溯</span></footer>
    </main>
  </div>;
}
