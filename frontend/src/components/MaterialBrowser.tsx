import { useEffect, useRef, useState } from "react";
import { ApiError, request } from "../api";
import { readingTracks } from "../readings";
import type { NarrativeObject, ObjectCatalog, Snapshot } from "../types";
import NarrativeView from "./NarrativeView";

const kindName: Record<string, string> = {
  content: "材料", expression: "表达", seed: "初始材料", surface: "续延", method: "方法",
  choice: "选择记录", view: "视图", response: "局部回应", effect: "执行后果",
};
const safeError = (error: unknown) => error instanceof ApiError
  ? `${error.message}（${error.code}）` : "材料读取未完成，请重试。";

/** Scope is a permission boundary; stale search/full-object responses never cross it. */
export default function MaterialBrowser({ snapshot }: { snapshot: Snapshot }) {
  const [requestedScope, setScope] = useState("");
  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [catalog, setCatalog] = useState<ObjectCatalog | null>(null);
  const [selected, setSelected] = useState<NarrativeObject | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [opening, setOpening] = useState("");
  const fullRequest = useRef<AbortController | null>(null);
  const scopes = [
    ...snapshot.positions.map((position) => ({ id: `position:${position.id}`, name: position.name })),
    ...readingTracks(snapshot).map((track) => ({ id: `reading:${track.id}`, name: track.title })),
  ];
  const scopeId = scopes.some((scope) => scope.id === requestedScope) ? requestedScope : scopes[0]?.id ?? "";
  const base = `/runs/${encodeURIComponent(snapshot.run.id)}`;

  useEffect(() => {
    const controller = new AbortController();
    fullRequest.current?.abort();
    setSelected(null);
    setCatalog(null);
    setOpening("");
    setError("");
    if (!scopeId) return;
    setLoading(true);
    const params = new URLSearchParams({ scope_id: scopeId, query: submittedQuery });
    void request<ObjectCatalog>(`${base}/objects?${params}`, { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setCatalog(result); })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setError(safeError(cause)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); fullRequest.current?.abort(); };
  }, [base, scopeId, submittedQuery, refresh, snapshot.run.revision]);

  async function openObject(id: string) {
    fullRequest.current?.abort();
    const controller = new AbortController();
    fullRequest.current = controller;
    setOpening(id);
    setSelected(null);
    setError("");
    try {
      const params = new URLSearchParams({ scope_id: scopeId, object_id: id });
      const result = await request<{ scope_id: string; object: NarrativeObject }>(`${base}/object?${params}`,
        { signal: controller.signal });
      if (!controller.signal.aborted) setSelected(result.object);
    } catch (cause) {
      if (!controller.signal.aborted) setError(safeError(cause));
    } finally {
      if (!controller.signal.aborted) setOpening("");
    }
  }

  return <section className="panel material-browser" aria-label="完整材料检索">
    <div className="section-header"><h2>检索与读取完整材料</h2><span className="small-tag">本地读取</span></div>
    <p className="helper">按叙事位置检索其可见对象，包括旧材料与组织记录。搜索结果是预览，点击后读取完整对象；此操作不调用模型。</p>
    <form aria-label="检索可见材料" className="material-search" onSubmit={(event) => {
      event.preventDefault(); setSubmittedQuery(query.trim()); setRefresh((value) => value + 1);
    }}>
      <label>读取位置<select aria-label="材料读取位置" value={scopeId} onChange={(event) => setScope(event.target.value)}>
        {scopes.map((scope) => <option value={scope.id} key={scope.id}>{scope.name}</option>)}
      </select></label>
      <label>查找材料<input type="search" aria-label="材料关键词" value={query}
        placeholder="输入关键词，或留空浏览" onChange={(event) => setQuery(event.target.value)} /></label>
      <button className="button secondary" type="submit" disabled={loading || !scopeId}>{loading ? "读取中…" : "检索材料"}</button>
    </form>
    {error && <p className="helper creation-feedback" role="alert">{error}</p>}
    <div className="material-browser-grid">
      <div className="material-results" aria-label="材料搜索结果">
        {catalog && <p className="helper">找到 {catalog.total} 项 · 当前显示 {catalog.objects.length} 项</p>}
        {catalog?.objects.map((object) => <article className="reflection-record" key={object.id}>
          <div className="section-header"><strong>{object.title || kindName[object.kind] || object.kind}</strong>
            <button className="text-button" disabled={opening === object.id}
              aria-label={`读取完整对象 ${object.id}`} onClick={() => void openObject(object.id)}>
              {opening === object.id ? "读取中…" : "读取完整对象"}
            </button></div>
          {object.text && <p className="material-preview">{object.text}</p>}
          <small>{object.preview_truncated ? "预览已截断" : "对象预览"} · <code>{object.id}</code></small>
        </article>)}
        {catalog && !catalog.objects.length && <p className="helper">当前读取位置未找到匹配的材料，可更换关键词。</p>}
      </div>
      <div className="material-full" aria-label="完整对象">
        {selected ? <>
          <h3>{selected.title || kindName[selected.kind] || selected.kind}</h3>
          <code>{selected.id}</code>
          {selected.text && <p className="material-text">{selected.text}</p>}
          {typeof selected.instruction === "string" && <p className="material-text">{selected.instruction}</p>}
          {selected.view && <NarrativeView view={selected.view} />}
          {typeof selected.version === "string" && <p className="helper">对象版本：<code>{selected.version}</code></p>}
          {["program", "inputs", "groups", "relations", "about", "evidence"].filter((field) => selected[field] !== undefined)
            .map((field) => <details key={field}><summary>{field}</summary><pre>{JSON.stringify(selected[field], null, 2)}</pre></details>)}
        </> : <p className="helper">选择左侧对象，在此读取全文或方法定义。</p>}
      </div>
    </div>
  </section>;
}
