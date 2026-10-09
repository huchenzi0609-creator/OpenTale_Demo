import { useMemo, useState } from "react";
import type { GraphLink, GraphNode, GraphSnapshot } from "../graphTypes";

/** A bounded undirected neighborhood preserves directed edges, cycles and shared parents. */
export function graphNeighborhood(nodes: GraphNode[], links: GraphLink[], center: string, radius: number, limit = 48) {
  const available = new Set(nodes.map((node) => node.id));
  const included = new Set<string>();
  if (available.has(center)) included.add(center);
  let frontier = new Set(included);
  for (let depth = 0; depth < radius; depth += 1) {
    const next = new Set<string>();
    for (const link of links) {
      if (frontier.has(link.source_id) && available.has(link.target_id) && !included.has(link.target_id)) next.add(link.target_id);
      if (frontier.has(link.target_id) && available.has(link.source_id) && !included.has(link.source_id)) next.add(link.source_id);
    }
    for (const id of [...next].sort()) {
      if (included.size >= limit) break;
      included.add(id);
    }
    frontier = new Set([...next].filter((id) => included.has(id)));
  }
  if (!included.size) nodes.slice(0, limit).forEach((node) => included.add(node.id));
  return {
    nodes: nodes.filter((node) => included.has(node.id)),
    links: links.filter((link) => included.has(link.source_id) && included.has(link.target_id)),
  };
}

export function graphNodeLabel(node: GraphNode, snapshot: GraphSnapshot) {
  const origins = snapshot.windows.filter((window) => window.entry_node_ids.includes(node.id));
  if (origins.length > 1) return "共同种子";
  if (origins.length === 1) return `${origins[0].label} · 种子`;
  const window = snapshot.windows.find((item) => node.scope_id === `window:${item.id}`);
  return `${window?.label ?? "世界节点"} · G${node.created_commit}`;
}

export default function GraphNeighborhood({ snapshot, nodes, links, selectedId, visitedIds, visitedLinkIds, onSelect }: {
  snapshot: GraphSnapshot;
  nodes: GraphNode[];
  links: GraphLink[];
  selectedId: string;
  visitedIds: string[];
  visitedLinkIds: string[];
  onSelect: (id: string) => void;
}) {
  const [radius, setRadius] = useState(1);
  const neighborhood = useMemo(() => graphNeighborhood(nodes, links, selectedId, radius), [nodes, links, selectedId, radius]);
  const visited = new Set(visitedIds);
  const traversed = new Set(visitedLinkIds);
  const width = 760;
  const height = Math.max(330, Math.ceil(neighborhood.nodes.length / 8) * 70 + 180);
  const positions = new Map(neighborhood.nodes.map((node, index) => {
    if (node.id === selectedId) return [node.id, { x: width / 2, y: height / 2 }] as const;
    const angle = index * Math.PI * 2 / Math.max(neighborhood.nodes.length, 1);
    const layer = Math.floor(index / 16);
    return [node.id, { x: width / 2 + Math.cos(angle) * (275 - layer * 55), y: height / 2 + Math.sin(angle) * (height / 2 - 60 - layer * 35) }] as const;
  }));
  return <section className="growth-graph panel" aria-label="世界图谱">
    <div className="growth-section-heading">
      <div><p className="eyebrow">NARRATIVE WORLD / G{snapshot.graph_commit}</p><h2>节点邻域</h2></div>
      <label>邻域范围<select aria-label="节点邻域范围" value={radius} onChange={(event) => setRadius(Number(event.target.value))}>
        <option value={1}>一步相连</option><option value={2}>两步相连</option><option value={3}>三步相连</option>
      </select></label>
    </div>
    <p className="helper">显示 {neighborhood.nodes.length} / {snapshot.counts?.nodes ?? nodes.length} 个节点，保留生长边、关联边、回环与共同父节点。每个邻域最多显示 48 个节点。</p>
    <div className="graph-legend"><span className="growth-line" />生长<span className="association-line" />关联<span className="visited-dot" />当前阅读已访问</div>
    <svg viewBox={`0 0 ${width} ${height}`} className="growth-world-svg" role="img" aria-label="世界节点与有向关系">
      <defs>
        <marker id="growth-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker>
        <marker id="association-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker>
      </defs>
      {neighborhood.links.map((link, index) => {
        const from = positions.get(link.source_id)!;
        const to = positions.get(link.target_id)!;
        const same = link.source_id === link.target_id;
        const distance = Math.hypot(to.x - from.x, to.y - from.y) || 1;
        const endX = to.x - (to.x - from.x) * 25 / distance;
        const endY = to.y - (to.y - from.y) * 25 / distance;
        const bend = 14 + (index % 3) * 13;
        const path = same
          ? `M ${from.x - 12} ${from.y - 19} C ${from.x - 65} ${from.y - 80}, ${from.x + 65} ${from.y - 80}, ${from.x + 17} ${from.y - 17}`
          : `M ${from.x} ${from.y} Q ${(from.x + to.x) / 2 + bend} ${(from.y + to.y) / 2 - bend} ${endX} ${endY}`;
        return <path key={link.id} d={path} className={`world-edge ${link.kind} ${traversed.has(link.id) ? "traversed" : ""}`} markerEnd={`url(#${link.kind}-arrow)`}><title>{link.kind === "growth" ? "生长" : "关联"}：{link.relation}{traversed.has(link.id) ? " · 当前读轨已经过" : ""}</title></path>;
      })}
      {neighborhood.nodes.map((node) => {
        const position = positions.get(node.id)!;
        const label = graphNodeLabel(node, snapshot);
        return <g key={node.id} transform={`translate(${position.x}, ${position.y})`} className={`world-node ${node.id === selectedId ? "selected" : ""} ${visited.has(node.id) ? "visited" : ""}`}
          role="button" aria-label={`查看节点 ${label} ${node.id}`} tabIndex={0} onClick={() => onSelect(node.id)}
          onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(node.id); } }}>
          <circle r="23" /><text textAnchor="middle" y="5">{snapshot.windows.some((window) => window.entry_node_ids.includes(node.id)) ? "种" : node.created_commit}</text>
          <text className="node-label" textAnchor="middle" y="44">{label.length > 18 ? `${label.slice(0, 18)}…` : label}</text><title>{label} · {node.id}</title>
        </g>;
      })}
    </svg>
    <div className="growth-node-list" aria-label="邻域节点列表">
      {neighborhood.nodes.map((node) => <button key={node.id} className={`small-tag ${node.id === selectedId ? "chosen" : ""}`} aria-pressed={node.id === selectedId} onClick={() => onSelect(node.id)}>{graphNodeLabel(node, snapshot)}</button>)}
    </div>
    <details className="growth-edge-details"><summary>邻域关系 · {neighborhood.links.length} 条</summary>
      <ul>{neighborhood.links.map((link) => <li key={link.id}><b>{link.kind === "growth" ? "生长" : "关联"}</b> <code>{link.source_id}</code> → <code>{link.target_id}</code><span>{link.relation}</span></li>)}</ul>
    </details>
  </section>;
}
