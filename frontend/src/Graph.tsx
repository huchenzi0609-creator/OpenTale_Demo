import { useMemo } from "react";
import type { Snapshot } from "./types";

interface Props {
  snapshot: Snapshot;
  selected: string;
  onSelect: (id: string) => void;
}

export default function Graph({ snapshot, selected, onSelect }: Props) {
  const nodes = useMemo(
    () =>
      snapshot.positions.map((position, index, all) => {
        const angle = (index / all.length) * Math.PI * 2 - Math.PI / 2;
        return {
          ...position,
          x: 230 + Math.cos(angle) * 138,
          y: 175 + Math.sin(angle) * 118,
        };
      }),
    [snapshot.positions],
  );
  const edges = snapshot.uses.flatMap((use) =>
    use.bindings
      .filter((binding) => binding.source_position_id !== use.position_id)
      .map((binding) => ({
        source: binding.source_position_id,
        target: use.position_id,
        expression: use.expression_id,
        role: use.role,
      })),
  );
  const uniqueEdges = edges.filter(
    (edge, index) =>
      edges.findIndex(
        (item) => item.source === edge.source && item.target === edge.target,
      ) === index,
  );
  return (
    <div className="graph-wrap">
      <svg
        viewBox="0 0 460 350"
        role="img"
        aria-label="当前位置之间的有效承接关系图"
      >
        <defs>
          <marker
            id="edge-arrow"
            viewBox="0 0 10 10"
            refX="26"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
        </defs>
        <circle className="graph-orbit" cx="230" cy="175" r="117" />
        <text x="230" y="169" textAnchor="middle" className="graph-center">
          共同表达
        </text>
        <text
          x="230"
          y="188"
          textAnchor="middle"
          className="graph-center-small"
        >
          局部共同构成
        </text>
        {uniqueEdges.map((edge) => {
          const from = nodes.find((node) => node.id === edge.source),
            to = nodes.find((node) => node.id === edge.target);
          if (!from || !to) return null;
          const active = edge.source === selected || edge.target === selected;
          return (
            <path
              key={`${edge.source}-${edge.target}`}
              className={`graph-edge ${active ? "active" : ""}`}
              markerEnd="url(#edge-arrow)"
              d={`M ${from.x} ${from.y} Q 230 175 ${to.x} ${to.y}`}
            >
              <title>
                {from.name} → {to.name}：{edge.role}
              </title>
            </path>
          );
        })}
        {nodes.map((node) => (
          <g
            key={node.id}
            className={`graph-node ${selected === node.id ? "selected" : ""}`}
            role="button"
            tabIndex={0}
            aria-label={`选择 ${node.name}`}
            aria-pressed={selected === node.id}
            onClick={() => onSelect(node.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(node.id);
              }
            }}
          >
            <circle cx={node.x} cy={node.y} r="27" />
            <text x={node.x} y={node.y + 5} textAnchor="middle">
              {node.name.slice(0, 2)}
            </text>
            <text
              className="graph-label"
              x={node.x}
              y={node.y + 47}
              textAnchor="middle"
            >
              {node.name}
            </text>
          </g>
        ))}
      </svg>
      <p className="graph-note">
        <span className="line-key" />
        箭头表示已安装的来源 → 使用位置；选择位置查看其当前条件。
      </p>
    </div>
  );
}
