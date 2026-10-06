import { useEffect, useRef, useState } from "react";
import { readingTracks, worldReadyForPresentation } from "../readings";
import { budgetExhausted } from "../budgetLimits";
import type { ReadingTrack, Snapshot } from "../types";
import type { Workbench } from "../useWorkbench";

type ReadingControls = Pick<Workbench, "selectedReadingId" | "setSelectedReadingId" |
  "presentingReadingId" | "busy" | "anyActive" | "config" |
  "createReading" | "setReadingEnabled" | "presentReading">;

function ReadingColumn({ track, snapshot, model }: {
  track: ReadingTrack; snapshot: Snapshot; model: ReadingControls;
}) {
  const bottom = useRef<HTMLDivElement>(null);
  const name = (id: string | null) =>
    snapshot.positions.find((position) => position.id === id)?.name ?? "由关系展开";
  const presented = track.story.some((segment) => segment.revision === snapshot.run.revision);
  const waiting = model.anyActive && snapshot.run.phase === "presenting" &&
    track.enabled && !presented;
  const generating = model.presentingReadingId === track.id;
  const blocked = !!model.busy || model.anyActive;
  const ready = worldReadyForPresentation(snapshot);
  const usage = snapshot.run.usage;
  const usedTokens = usage.input_tokens + usage.output_tokens + (usage.reserved_tokens ?? 0) + (usage.unknown_tokens ?? 0);
  const presentationBudgetExhausted = budgetExhausted(usage.calls, snapshot.run.limits.max_calls) ||
    budgetExhausted(usedTokens, snapshot.run.limits.max_tokens);
  const needsConfig = snapshot.run.provider === "deepseek" && !model.config?.configured;
  const reason = model.anyActive ? "请先暂停运行，再调整阅读或单独生成。"
    : !ready ? "请先完成人物续延；当前世界的所有人物续延就绪后即可呈现。"
    : presentationBudgetExhausted ? "调用或 Token 预算已用尽，请先更新预算。"
    : needsConfig ? "实时模型尚未配置，请先刷新模型配置。"
    : "";
  return <section className="reading-column" aria-label={`阅读：${track.title}`}>
    <div className="reading-column-heading">
      <div>
        <h3>{track.title}</h3>
        <p className="helper">起始位置：{name(track.start_position_id)} · 创建于 R{track.created_revision} · {track.story.length} 段</p>
      </div>
      <button className="text-button" disabled={!track.story.length}
        onClick={() => bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" })}>
        至最新段落 ↓
      </button>
    </div>
    <div className="reading-column-controls">
      <label className="reading-checkbox">
        <input type="checkbox" checked={track.enabled} disabled={blocked}
          aria-label={`${track.title}自动续写`}
          onChange={(event) => void model.setReadingEnabled(track.id, event.target.checked)} />
        随世界窗口自动续写
      </label>
      <button className="button secondary" disabled={blocked || !ready || presentationBudgetExhausted || needsConfig || presented}
        onClick={() => void model.presentReading(track.id)}>
        {generating ? "正在生成…" : presented ? "本版本已呈现" : "从当前世界生成一段"}
      </button>
    </div>
    {reason && <p className="helper" role="status">{reason}</p>}
    {(waiting || generating) && <p className="reading-progress" role="status">{generating ? "正在呈现当前世界；人物阶段不会重跑。" : "本条阅读等待或正在呈现，其他阅读已有的结果会保留。"}</p>}
    {track.last_error && <p className="reading-error" role="status">此阅读保留了恢复点：{track.last_error.message}（{track.last_error.code}）。可继续未完成的呈现。</p>}
    {track.story.length ? <div className="story-text">
      {track.story.map((segment, index) => <article key={segment.id} className="story-segment">
        <div className="segment-heading">
          <span>{String(index + 1).padStart(2, "0")}</span>
          <small>R{segment.revision} · {name(segment.focus_position_id)}</small>
        </div>
        {segment.text.split(/\n\s*\n/).filter(Boolean).map((paragraph, index) =>
          <p key={index}>{paragraph}</p>)}
      </article>)}
      <div ref={bottom} />
    </div> : <div className="empty-state">
      <span>∿</span><h3>正文尚未开始</h3>
      <p>{track.id === "main"
        ? "执行一个世界窗口后，呈现者将从更新后的共同关系展开第一段。"
        : "这条阅读尚未生成；它将从自己的起点承接当前世界，保留独立前文，不复制主阅读的历史。"}</p>
    </div>}
  </section>;
}

export default function StoryReader({ snapshot, model }: { snapshot: Snapshot; model: ReadingControls }) {
  const tracks = readingTracks(snapshot);
  const selected = tracks.find((track) => track.id === model.selectedReadingId) ?? tracks[0];
  const [compare, setCompare] = useState(false);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [start, setStart] = useState(snapshot.positions[0]?.id ?? "");
  const [enabled, setEnabled] = useState(true);
  const previousRun = useRef(snapshot.run.id);
  const previousCount = useRef(tracks.length);
  useEffect(() => {
    if (previousRun.current !== snapshot.run.id) {
      setCompare(false); setCreating(false); setTitle("");
      setStart(snapshot.positions[0]?.id ?? ""); setEnabled(true);
      previousRun.current = snapshot.run.id;
    } else if (tracks.length > previousCount.current) {
      setCreating(false); setTitle("");
    }
    previousCount.current = tracks.length;
  }, [snapshot.run.id, snapshot.positions, tracks.length]);
  const blocked = !!model.busy || model.anyActive;
  const visible = compare ? tracks : [selected];
  const activeCount = tracks.filter((track) => track.enabled).length;
  return <section className="readings-panel panel" aria-label="叙事正文">
    <div className="section-header">
      <div><p className="eyebrow">READING / SHARED WORLD, DISTINCT CONTINUITIES</p>
        <h2>同一世界的多条阅读</h2></div>
      <button className="button secondary" disabled={blocked}
        onClick={() => setCreating((value) => !value)}>＋ 新建阅读</button>
    </div>
    <p className="helper reading-explanation">不同起点影响承接顺序，每条阅读保留自己的前文；共同关系与人物续延共享，不复制或分叉世界。</p>
    <div className="reading-selector">
      <label>当前阅读<select aria-label="选择阅读" value={selected.id}
        onChange={(event) => model.setSelectedReadingId(event.target.value)}>
        {tracks.map((track) => <option key={track.id} value={track.id}>{track.title} · {track.story.length} 段{track.enabled ? "" : " · 已停用自动续写"}</option>)}
      </select></label>
      <button className="text-button" aria-pressed={compare} disabled={tracks.length < 2}
        onClick={() => setCompare((value) => !value)}>{compare ? "返回单条阅读" : "并排对照"}</button>
    </div>
    <p className="helper">{activeCount} 条阅读自动续写；每个完整窗口通常需要 {snapshot.positions.length * 2 + activeCount} 次调用，重试另计。阅读共享本实验的调用与 Token 预算。</p>
    {model.anyActive && <p className="helper" role="status">请先暂停运行，再新建或调整阅读。</p>}
    {creating && <form className="new-reading-form" aria-label="新建独立阅读"
      onSubmit={(event) => { event.preventDefault(); void model.createReading(title, start, enabled); }}>
      <label>阅读名称<input aria-label="阅读名称" maxLength={120} value={title} disabled={blocked}
        onChange={(event) => setTitle(event.target.value)} placeholder="例如：母亲的阅读" /></label>
      <label>起始人物<select aria-label="阅读起始人物" value={start} disabled={blocked}
        onChange={(event) => setStart(event.target.value)}>
        {snapshot.positions.map((position) => <option key={position.id} value={position.id}>{position.name}</option>)}
      </select></label>
      <label className="reading-checkbox"><input type="checkbox" checked={enabled} disabled={blocked}
        onChange={(event) => setEnabled(event.target.checked)} />随以后世界窗口自动续写</label>
      <p className="helper">新增只保存阅读设置，不调用模型。起始人物限定首段焦点；以后可随关系换焦点。从当前世界单独生成一段需要一次呈现调用，不重跑人物阶段。</p>
      <div className="new-reading-actions">
        <button className="button primary" type="submit" disabled={blocked || !title.trim() || !start}>
          {model.busy === "新建阅读" ? "正在创建…" : "创建阅读（不调用模型）"}</button>
        <button className="text-button" type="button" disabled={!!model.busy} onClick={() => setCreating(false)}>取消</button>
      </div>
    </form>}
    <div className={`reading-columns${compare ? " comparing" : ""}`}>
      {visible.map((track) => <ReadingColumn key={track.id} track={track} snapshot={snapshot} model={model} />)}
    </div>
    <footer className="story-footer">{snapshot.run.provider === "fixture"
      ? "离线文本是机制夹具，不代表真实模型表现。"
      : "仅呈现叙事正文；不同阅读路径都承接同一世界的共同变化，内部来源保留在诊断视图。"}</footer>
  </section>;
}
