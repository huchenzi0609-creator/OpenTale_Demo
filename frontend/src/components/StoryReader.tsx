import { useRef } from "react";
import type { Snapshot } from "../types";
export default function StoryReader({ snapshot }: { snapshot: Snapshot }) {
  const storyBottom = useRef<HTMLDivElement>(null);
  const positionName = (id: string) =>
    snapshot.positions.find((item) => item.id === id)?.name ?? id;
  return (
    <section className="story-panel panel" aria-label="叙事正文">
      <div className="section-header">
        <div>
          <p className="eyebrow">READING / A LOCAL CONTINUITY</p>
          <h2>一条正在形成的阅读</h2>
        </div>
        <button
          className="text-button"
          disabled={!snapshot.story.length}
          onClick={() =>
            storyBottom.current?.scrollIntoView({
              behavior: "smooth",
              block: "end",
            })
          }
        >
          至最新段落 ↓
        </button>
      </div>
      {snapshot.story.length ? (
        <div className="story-text">
          {snapshot.story.map((segment, index) => (
            <article key={segment.id} className="story-segment">
              <div className="segment-heading">
                <span>{String(index + 1).padStart(2, "0")}</span>
                <small>
                  R{segment.revision} ·{" "}
                  {positionName(segment.focus_position_id)}
                </small>
              </div>
              {segment.text
                .split(/\n\s*\n/)
                .filter(Boolean)
                .map((paragraph, i) => (
                  <p key={i}>{paragraph}</p>
                ))}
            </article>
          ))}
          <div ref={storyBottom} />
        </div>
      ) : (
        <div className="empty-state">
          <span>∿</span>
          <h3>正文尚未开始</h3>
          <p>
            执行一个局部窗口后，呈现者将从更新后的共同关系展开第一段。技术来源与操作记录保留在诊断视图。
          </p>
        </div>
      )}
      <footer className="story-footer">
        {snapshot.run.provider === "fixture"
          ? "离线文本是机制夹具，不代表真实模型表现。"
          : "此处仅呈现叙事正文，内部引用与来源留在诊断视图。"}
      </footer>
    </section>
  );
}
