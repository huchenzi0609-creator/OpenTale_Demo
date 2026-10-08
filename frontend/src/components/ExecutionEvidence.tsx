import { useState } from "react";
import type { Snapshot } from "../types";

const STAGES: Record<string, string> = { propose: "构想", realize: "续延", present: "阅读" };
const PAGE_SIZE = 10;
export default function ExecutionEvidence({ snapshot }: { snapshot: Snapshot }) {
  const [requestedPage, setPage] = useState(0);
  const requests = [...(snapshot.requests ?? [])].reverse();
  const page = Math.min(requestedPage, Math.max(0, Math.ceil(requests.length / PAGE_SIZE) - 1));
  return <>
    <section className="panel" aria-label="请求输入证据">
      <h2>请求输入证据</h2>
      <p className="helper">在调用前冻结输入并记录重试。精确投影标识实际发送的模型输入；仅输入帧证据保留本地组织信息。此处只展示摘要与用量。</p>
      {requests.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map((record) =>
        <details className="reflection-record" key={record.id}>
          <summary><strong>{record.scope_id} · {STAGES[record.stage] ?? record.stage}</strong><small>R{record.input_world_revision} · 策略 {record.policy_revision}</small></summary>
          <dl className="reflection-fields">
            <dt>证据类型</dt><dd>{record.evidence_level === "exact_projection" ? "精确投影" : "仅输入帧"}</dd>
            <dt>投影版本</dt><dd>{record.projection_version}</dd>
            <dt>输入帧摘要</dt><dd><code className="fingerprint">{record.frame_hash}</code></dd>
            <dt>请求摘要</dt><dd><code className="fingerprint">{record.request_hash || "未记录"}</code></dd>
            <dt>采纳结果摘要</dt><dd><code className="fingerprint">{record.accepted_result_hash || "尚无采纳结果"}</code></dd>
          </dl>
          {record.context_compaction && <section aria-label="上下文整理记录">
            <h3>上下文整理</h3>
            <p className="helper">{record.context_compaction.lossy
              ? "本次含有出处的摘录。部分原文未送入模型，完整材料仍保存在原输入与存档中。"
              : record.context_compaction.structure_excerpted
              ? "本次部分组织结构未展开，完整结构保存在原输入与存档中，可按来源继续读取。"
              : "本次合并重复文本并精简候选预览，选中材料正文完整保留。"}</p>
            <dl className="reflection-fields">
              <dt>请求字符数</dt><dd>{record.context_compaction.input_chars.toLocaleString()} → {record.context_compaction.output_chars.toLocaleString()}（基础请求）</dd>
              <dt>材料出现位置 / 文本池条目</dt><dd>{record.context_compaction.material_occurrences.toLocaleString()} / {record.context_compaction.text_entries.toLocaleString()}</dd>
              <dt>正文与历史省略字符</dt><dd>{record.context_compaction.omitted_text_chars.toLocaleString()}</dd>
              <dt>阅读历史摘录段数</dt><dd>{record.context_compaction.history_segments_excerpted.toLocaleString()} / {record.context_compaction.history_segments.toLocaleString()}</dd>
              <dt>整理目标</dt><dd>{record.context_compaction.target_chars.toLocaleString()} 字符 · {record.context_compaction.within_target ? "已达到" : "超过软目标；已通过单次容量检查"}</dd>
              {record.context_compaction.material_occurrences_visible != null && <>
                <dt>本次呈现的材料位置</dt><dd>{record.context_compaction.material_occurrences_visible.toLocaleString()} / {record.context_compaction.material_occurrences.toLocaleString()}</dd>
                <dt>本次呈现的候选对象</dt><dd>{record.context_compaction.candidate_objects_visible?.toLocaleString()} / {record.context_compaction.candidate_objects.toLocaleString()}</dd>
                <dt>本次呈现的阅读历史</dt><dd>{record.context_compaction.history_segments_visible?.toLocaleString()} / {record.context_compaction.history_segments.toLocaleString()}</dd>
              </>}
            </dl>
            {!!record.context_compaction.pages?.length && <details><summary>结构与历史分页记录</summary>
              <ul>{record.context_compaction.pages.map((item) => <li key={item.path}>
                <code>{item.path}</code> · 总数 {item.total.toLocaleString()} · 未展开 {item.omitted_count.toLocaleString()}
              </li>)}</ul>
            </details>}
            <p className="helper">原文片段及对应范围随请求证据保存。摘录保留来源，不保证保留全部意义；生成者可再次查询或读取有关原文范围。</p>
          </section>}
          <h3>请求尝试</h3>
          <ul className="request-attempts">{record.attempts.map((attempt) => <li key={attempt.number}>
            第 {attempt.number} 次 · {attempt.status}
            {attempt.repair_category && <span> · 修复类别 {attempt.repair_category}</span>}
            {(attempt.input_tokens != null || attempt.output_tokens != null) && <span> · 输入 {attempt.input_tokens ?? "未知"} / 输出 {attempt.output_tokens ?? "未知"} Token</span>}
            {attempt.request_chars != null && <span> · 请求 {attempt.request_chars.toLocaleString()} 字符</span>}
          </li>)}</ul>
        </details>)}
      {!requests.length && <p className="helper">尚无请求证据。旧记录不会自动补造实际请求。</p>}
      <nav className="reflection-pager" aria-label="请求证据分页">
        <button className="text-button" disabled={!page} onClick={() => setPage(page - 1)}>上一页</button>
        <span>第 {page + 1} / {Math.max(1, Math.ceil(requests.length / PAGE_SIZE))} 页 · {requests.length} 条</span>
        <button className="text-button" disabled={(page + 1) * PAGE_SIZE >= requests.length} onClick={() => setPage(page + 1)}>下一页</button>
      </nav>
    </section>
    {snapshot.schedule && <section className="panel" aria-label="影子调度计划">
      <h2>依赖调度观察</h2>
      <p className="helper">当前处于影子调度：完整阶段照常执行，下表记录依赖机制对任务的判断。输入未变仍可有继续叙述的理由。</p>
      {snapshot.schedule.jobs.map((job) => <details className="reflection-record" key={job.key}>
        <summary><strong>{job.scope_id} · {STAGES[job.stage] ?? job.stage}</strong><small>{job.changed ? "依赖变化" : "依赖未变"}</small></summary>
        <p className="helper">{job.reason}</p>
        <p><code>{job.dependencies.join("、") || "未记录依赖"}</code></p>
      </details>)}
    </section>}
  </>;
}
