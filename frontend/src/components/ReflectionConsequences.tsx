import { useState } from "react";
import { readingTracks } from "../readings";
import type { Snapshot } from "../types";
import NarrativeView from "./NarrativeView";

const PAGE_SIZE = 10;
export default function ReflectionConsequences({ snapshot }: { snapshot: Snapshot }) {
  const [selected, setSelected] = useState<"responses" | "effects" | "views">("responses");
  const [requestedPage, setPage] = useState(0);
  const state = snapshot.reflection;
  const records = [...(state?.[selected] ?? [])].reverse();
  const page = Math.min(requestedPage, Math.max(0, Math.ceil(records.length / PAGE_SIZE) - 1));
  const ids = new Set(records.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map((record) => record.id));
  const scopeName = (id: string) => snapshot.positions.find((position) =>
    id === `actor:${position.id}` || id === `position:${position.id}`)?.name
    ?? readingTracks(snapshot).find((track) => id === `reading:${track.id}`)?.title ?? id;
  return <section className="panel reflection-consequences" aria-label="组织的后果与回应">
    <h2>组织的后果与回应</h2>
    <p className="helper">执行后果记录实际输入的变化。局部回应是叙事位置对经历的解释，可以彼此不同，也可以成为下一次生成的材料。</p>
    <nav className="tabs" aria-label="后果记录类型">
      {([["responses", "局部回应"], ["effects", "执行后果"], ["views", "已保存视图"]] as const).map(([value, label]) =>
        <button key={value} aria-pressed={selected === value} className={selected === value ? "selected" : ""}
          onClick={() => { setSelected(value); setPage(0); }}>{label}</button>)}
    </nav>
    {selected === "responses" && [...(state?.responses ?? [])].reverse().filter((item) => ids.has(item.id)).map((response) =>
      <details key={response.id} className="reflection-record">
        <summary><strong>{scopeName(response.source_scope)}的局部回应</strong><small>R{response.created_revision}</small></summary>
        <p className="material-text">{response.text}</p>
        <dl className="reflection-fields">
          <dt>可见范围</dt><dd>{response.visibility === "scope" ? "仅此叙事位置" : "共享回应"}</dd>
          <dt>回应对象</dt><dd><code>{response.about.join("、") || "未指定"}</code></dd>
          <dt>引用依据</dt><dd><code>{response.evidence.join("、") || "未指定"}</code></dd>
          <dt>记录</dt><dd><code>{response.id}</code></dd>
        </dl>
      </details>)}
    {selected === "effects" && [...(state?.effects ?? [])].reverse().filter((item) => ids.has(item.id)).map((effect) =>
      <details key={effect.id} className="reflection-record">
        <summary><strong>{scopeName(effect.scope_id)} · {effect.before_fingerprint === null ? "初次输入记录" : effect.changed ? "输入发生变化" : "输入未变"}</strong><small>R{effect.revision}</small></summary>
        <dl className="reflection-fields">
          <dt>采用方法</dt><dd><code>{effect.method_ids.join("、") || "默认选择"}</code></dd>
          <dt>增加材料</dt><dd><code>{effect.selected_added.join("、") || "无"}</code></dd>
          <dt>移除材料</dt><dd><code>{effect.selected_removed.join("、") || "无"}</code></dd>
          <dt>此前输入</dt><dd><code className="fingerprint">{effect.before_fingerprint ?? "无此前记录"}</code></dd>
          <dt>当前输入</dt><dd><code className="fingerprint">{effect.after_fingerprint}</code></dd>
          <dt>关联回应</dt><dd><code>{effect.response_ids.join("、") || "暂无回应"}</code></dd>
          <dt>选择记录</dt><dd><code>{effect.choice_id}</code></dd>
        </dl>
      </details>)}
    {selected === "views" && [...(state?.views ?? [])].reverse().filter((item) => ids.has(item.id)).map((record) =>
      <details key={record.id} className="reflection-record">
        <summary><strong>{scopeName(record.scope_id)}的组织视图</strong><small>R{record.created_revision}</small></summary>
        <NarrativeView view={record.view} /><code>{record.id}</code>
      </details>)}
    {!records.length && <p className="helper">尚无此类记录。执行窗口后查看组织与反馈。</p>}
    <nav className="reflection-pager" aria-label="后果记录分页">
      <button className="text-button" disabled={!page} onClick={() => setPage(page - 1)}>上一页</button>
      <span>第 {page + 1} / {Math.max(1, Math.ceil(records.length / PAGE_SIZE))} 页 · {records.length} 条</span>
      <button className="text-button" disabled={(page + 1) * PAGE_SIZE >= records.length} onClick={() => setPage(page + 1)}>下一页</button>
    </nav>
  </section>;
}
