import { useEffect, useState } from "react";
import type { Snapshot } from "../types";
import type { Workbench } from "../useWorkbench";
import { readingTracks } from "../readings";

const PAGE_SIZE = 10;
const STAGES = { propose: "构想", realize: "续延", present: "阅读" };

function Pager({ page, total, label, onChange }: {
  page: number; total: number; label: string; onChange: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  return <nav className="reflection-pager" aria-label={`${label}分页`}>
    <button className="text-button" disabled={page === 0}
      onClick={() => onChange(page - 1)}>上一页</button>
    <span>第 {page + 1} / {pages} 页 · {total} 条</span>
    <button className="text-button" disabled={page + 1 >= pages}
      onClick={() => onChange(page + 1)}>下一页</button>
  </nav>;
}

export default function ReflectionPanel({ snapshot, model }: {
  snapshot: Snapshot; model: Workbench;
}) {
  const reflection = snapshot.reflection;
  const [methodPage, setMethodPage] = useState(0);
  const [choicePage, setChoicePage] = useState(0);
  const [requestedScope, setScopeId] = useState("");
  const [requestedMethod, setMethodId] = useState("");
  const scopes = Object.entries(reflection?.adoptions ?? {}).filter(([, ids]) => ids.length > 0);
  // A release can invalidate both selections at once; derive a valid pair in one render.
  const scopeId = scopes.some(([id]) => id === requestedScope) ? requestedScope : scopes[0]?.[0] ?? "";
  const adopted = reflection?.adoptions[scopeId] ?? [];
  const methodId = adopted.includes(requestedMethod) ? requestedMethod : adopted[0] ?? "";
  const { frozen, busy, reflectionAblation, setReflectionAblation, ablateReflection, releaseReflection } = model;
  const methods = [...(reflection?.methods ?? [])].reverse();
  const choices = [...(reflection?.choices ?? [])].reverse();
  const scopeName = (id: string) => snapshot.positions.find((position) =>
    id === position.id || id === `actor:${position.id}` || id === `position:${position.id}`)?.name
    ?? readingTracks(snapshot).find((reading) => id === `reading:${reading.id}`)?.title
    ?? id;
  const methodName = (id: string) => reflection?.methods.find((method) => method.id === id)?.title ?? id;

  useEffect(() => {
    setMethodPage(0);
    setChoicePage(0);
    setScopeId("");
    setMethodId("");
  }, [snapshot.run.id]);
  useEffect(() => {
    setMethodPage((page) => Math.min(page, Math.max(0, Math.ceil(methods.length / PAGE_SIZE) - 1)));
    setChoicePage((page) => Math.min(page, Math.max(0, Math.ceil(choices.length / PAGE_SIZE) - 1)));
  }, [methods.length, choices.length]);

  return <div className="reflection-layout" aria-label="反身生成技术面板">
    <section className="panel reflection-intro">
      <p className="eyebrow">REFLEXIVE / EXECUTABLE METHODS</p>
      <h2>方法与实际选择</h2>
      <p className="helper">方法可以采用内容、已有方法与实际选择记录，改变后续输入。以下是软件执行证据，不是模型的内心推理；正文仍在“连续阅读”中独立呈现。</p>
      {!reflection && <p className="helper" role="status">当前快照没有反身记录。已有正文仍可阅读；刷新存档后查看最新记录。</p>}
    </section>
    <section className="panel" aria-label="已生成方法">
      <div className="section-header"><h2>方法版本</h2><span className="count-pill">{methods.length}</span></div>
      <p className="helper">方法版本保留原记录。修改关系与采用作用域分别显示，修改并不自动覆盖所有位置。暂停或技术错误后可停止某个作用域的采用，仅改变后续采用关系；已冻结的调用输入保留，模型可以再次采用。</p>
      {methods.slice(methodPage * PAGE_SIZE, (methodPage + 1) * PAGE_SIZE).map((method) => <details className="reflection-record" key={method.id}>
        <summary><strong>{method.title}</strong><small>R{method.created_revision} · {method.id}</small></summary>
        <dl className="reflection-fields">
          <dt>组织指令</dt><dd>{method.instruction || "仅执行输入选择与排序。"}</dd>
          <dt>输入对象</dt><dd><code>{method.inputs.join(" → ")}</code></dd>
          <dt>排序与选材</dt><dd>{method.order === "reverse" ? "倒序" : "列出顺序"} · {method.limit === null ? "全部材料" : `最多 ${method.limit} 项`}</dd>
          <dt>修改关系</dt><dd>{method.revises ? <>修改 {methodName(method.revises)} <code>{method.revises}</code></> : "初始版本"}</dd>
          <dt>创建作用域</dt><dd>{scopeName(method.source_scope)} <code>{method.source_scope}</code></dd>
          <dt>当前采用处</dt><dd>{Object.entries(reflection?.adoptions ?? {}).filter(([, ids]) => ids.includes(method.id)).length
            ? Object.entries(reflection?.adoptions ?? {}).filter(([, ids]) => ids.includes(method.id)).map(([id]) =>
              <div className="reflection-adoption" key={id}>
                <span>{scopeName(id)}（{id}）</span>
                <button className="text-button" disabled={frozen}
                  aria-label={`停止采用 ${method.title} · ${scopeName(id)}（${id}）`}
                  onClick={() => void releaseReflection(id, method.id)}>
                  {busy === "停止采用方法" ? "正在停止…" : "停止采用"}
                </button>
              </div>) : "当前未被采用"}</dd>
        </dl>
      </details>)}
      {!methods.length && <p className="helper">尚未生成方法。执行窗口后查看创建与采用结果。</p>}
      <Pager page={methodPage} total={methods.length} label="方法" onChange={setMethodPage} />
    </section>
    <section className="panel" aria-label="实际选择历史">
      <div className="section-header"><h2>实际选择历史</h2><span className="count-pill">{choices.length}</span></div>
      <p className="helper">按最新记录在前排列，每页至多 {PAGE_SIZE} 条。指纹记录实际输入；变化不等于文学效果改善。</p>
      {choices.slice(choicePage * PAGE_SIZE, (choicePage + 1) * PAGE_SIZE).map((choice) => <details className="reflection-record" key={choice.id}>
        <summary><strong>{scopeName(choice.scope_id)} · {STAGES[choice.stage]}</strong><small>R{choice.revision} · {choice.id}</small></summary>
        <dl className="reflection-fields">
          <dt>采用方法</dt><dd>{choice.methods.map((id) => `${methodName(id)}（${id}）`).join("、") || "默认材料选择"}</dd>
          <dt>提供对象</dt><dd><code>{choice.offered.join("、") || "无"}</code></dd>
          <dt>实际输入</dt><dd><code>{choice.selected.join("、") || "无"}</code></dd>
          <dt>输入分组</dt><dd>{choice.groups.length ? choice.groups.map((group, index) =>
            <p key={index}>组 {index + 1}：<code>{group.join(" → ") || "空组"}</code></p>) : "默认分组"}</dd>
          <dt>输入指纹</dt><dd><code className="fingerprint">{choice.input_fingerprint}</code></dd>
        </dl>
        <details><summary>查看冻结的输入材料（{choice.materials.length} 项）</summary>
          {choice.materials.map((material, index) => <div className="reflection-material" key={`${material.id}-${index}`}>
            <code>{material.id} · {material.role}</code><p>{material.text}</p>
          </div>)}
        </details>
      </details>)}
      {!choices.length && <p className="helper">尚无选择记录。记录由运行时根据实际输入生成。</p>}
      <Pager page={choicePage} total={choices.length} label="选择记录" onChange={setChoicePage} />
    </section>
    <section className="panel reflection-ablation" aria-label="方法离线消融">
      <h2>移除方法，输入会改变吗？</h2>
      <p className="helper">仅移除所选作用域当前采用的一项方法，在独立副本中比较输入。不会调用模型，也不会修改原存档。</p>
      <div className="counter-controls">
        <label>采用作用域<select aria-label="消融作用域" value={scopeId} disabled={frozen || !!snapshot.pending || !scopes.length}
          onChange={(event) => { setScopeId(event.target.value); setMethodId(""); setReflectionAblation(null); }}>
          {!scopes.length && <option value="">尚无已采用方法</option>}
          {scopes.map(([id]) => <option key={id} value={id}>{scopeName(id)} · {id}</option>)}
        </select></label>
        <label>移除方法<select aria-label="消融方法" value={methodId} disabled={frozen || !!snapshot.pending || !adopted.length}
          onChange={(event) => { setMethodId(event.target.value); setReflectionAblation(null); }}>
          {!adopted.length && <option value="">请选择采用作用域</option>}
          {adopted.map((id) => <option key={id} value={id}>{methodName(id)} · {id}</option>)}
        </select></label>
      </div>
      <button className="button secondary" disabled={frozen || !!snapshot.pending || !scopeId || !methodId}
        onClick={() => void ablateReflection(scopeId, methodId)}>
        {busy === "方法离线消融" ? "正在比较…" : "执行方法离线消融"}
      </button>
      {(frozen || snapshot.pending) && <p className="helper">请先暂停并完成当前恢复窗口，针对稳定快照比较。</p>}
      {reflectionAblation && <div className="counter-result" role="status">
        <strong>{reflectionAblation.structural_effect ? "✓ 实际输入发生变化" : "○ 实际输入未发生变化"}</strong>
        <p className="helper">{reflectionAblation.note}</p>
        <dl className="reflection-fields">
          <dt>原始指纹</dt><dd><code className="fingerprint">{reflectionAblation.before_fingerprint}</code></dd>
          <dt>移除后指纹</dt><dd><code className="fingerprint">{reflectionAblation.after_fingerprint}</code></dd>
          <dt>原始选择</dt><dd><code>{reflectionAblation.before_selected.join("、") || "无"}</code></dd>
          <dt>移除后选择</dt><dd><code>{reflectionAblation.after_selected.join("、") || "无"}</code></dd>
        </dl>
      </div>}
    </section>
  </div>;
}
