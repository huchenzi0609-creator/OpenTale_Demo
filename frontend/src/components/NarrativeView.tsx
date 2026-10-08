import type { NarrativeView as View, ViewGroup } from "../types";

function Group({ group }: { group: ViewGroup }) {
  return <li>
    <strong>{group.label || "材料分组"}</strong>
    <p><code>{group.material_ids.join(" → ") || "此层未直接引用材料"}</code></p>
    {!!group.children.length && <ul>{group.children.map((child) => <Group key={child.id} group={child} />)}</ul>}
  </li>;
}

export default function NarrativeView({ view }: { view: View }) {
  const labels = new Map<string, string>();
  const pending = [...view.groups];
  while (pending.length) {
    const group = pending.pop()!;
    labels.set(group.id, group.label || group.id);
    pending.push(...group.children);
  }
  return <div className="narrative-view">
    <h3>材料的组织</h3>
    {view.groups.length ? <ul>{view.groups.map((group) => <Group key={group.id} group={group} />)}</ul>
      : <p className="helper">此视图未指定分组。</p>}
    {!!view.relations.length && <>
      <h3>组织之间的关系</h3>
      <ul>{view.relations.map((relation, index) => <li key={index}>
        <span>{labels.get(relation.source) ?? relation.source}</span>
        <span className="view-relation"> → {relation.label} → </span>
        <span>{labels.get(relation.target) ?? relation.target}</span>
      </li>)}</ul>
    </>}
  </div>;
}
