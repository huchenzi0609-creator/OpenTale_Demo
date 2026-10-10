import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GrowthWorkbench, { sourceTextParts } from "./components/GrowthWorkbench";
import { graphNeighborhood } from "./components/GraphNeighborhood";
import { growthFixture } from "./growthFixture";
import type { GraphPreset, GraphSnapshot, GraphSourceSpan } from "./graphTypes";

let snapshot: GraphSnapshot;
let overrides: Record<string, () => Response | Promise<Response>>;
let graphPresets: GraphPreset[];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const originalText = "他🌱又进了雨中的街道。";
const span: GraphSourceSpan = { node_id: "node-a", node_version: "version-a", text_ref: { sha256: "text-a", length: Array.from(originalText).length }, start: 1, end: 3, passage_start: 0, passage_end: 2 };
const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
  const method = options?.method ?? "GET";
  const custom = overrides[`${method} ${url}`];
  if (custom) return custom();
  if (url === "/api/config") return json({ configured: true, provider: "deepseek", model: "dummy-model", timeout_seconds: 60, max_output_tokens: 2048 });
  if (url === "/api/presets") return json({ presets: [{ id: "preset-1", name: "原有初始叙事", description: "synthetic", positions: [], expressions: [] }] });
  if (url === "/api/graph-runs" && method === "GET") return json({ items: [{ id: snapshot.run.id, title: snapshot.run.title, graph_commit: snapshot.graph_commit, status: snapshot.run.status }] });
  if (url === "/api/graph-presets" && method === "GET") return json({ presets: graphPresets });
  if (url === "/api/graph-presets" && method === "POST") {
    const body = JSON.parse(options!.body as string);
    const saved = { ...body, version: "saved-preset-version" };
    graphPresets = [saved, ...graphPresets.filter((preset) => preset.id !== saved.id)];
    return json(saved);
  }
  if (url === "/api/graph-runs" && method === "POST") return json(snapshot);
  if (url === "/api/graph-runs/graph-1") return json(snapshot);
  if (url === "/api/graph-runs/graph-1/graph?offset=0") return json({ nodes: snapshot.nodes, links: snapshot.links, graph_commit: snapshot.graph_commit, next_offset: null });
  if (url.startsWith("/api/graph-runs/graph-1/nodes/")) return json({ node: snapshot.nodes.find((node) => node.id === url.split("/").at(-1)!.split("?")[0]), text: originalText });
  if (/\/growth\/(start|pause|step)$/.test(url)) {
    const action = url.split("/").at(-1);
    snapshot.active_scopes = action === "start" ? ["growth"] : [];
    snapshot.run.status = action === "start" ? "running" : "idle";
    return json(snapshot);
  }
  if (url === "/api/graph-runs/graph-1/readings" && method === "POST") {
    const body = JSON.parse(options!.body as string);
    snapshot.readings.push({ ...snapshot.readings[0], id: "reader-2", label: body.title, read_seq: 0, entry_node_ids: body.entry_node_ids, visits: [], passages: [] });
    return json(snapshot);
  }
  const readingAction = url.match(/\/readings\/([^/]+)\/(step|start|pause)$/);
  if (readingAction) {
    const track = snapshot.readings.find((reading) => reading.id === readingAction[1])!;
    if (readingAction[2] === "step") {
      track.read_seq += 1;
      track.visits.push({ id: `visit-${track.id}-${track.read_seq}`, track_id: track.id, read_seq: track.read_seq, node_id: "node-a", node_version: "version-a", observed_graph_commit: 2, from_node_id: track.cursor_node_id, via_link_id: "cycle", transition: "再次进入", source_spans: [span] });
      track.passages.push({ id: `passage-${track.id}-${track.read_seq}`, track_id: track.id, read_seq: track.read_seq, text_ref: { sha256: "passage-text", length: 15 }, text: "他在街道上重新读那封信。", visit_ids: [track.visits.at(-1)!.id], source_spans: [span] });
    } else {
      track.status = readingAction[2] === "start" ? "running" : "ready";
      snapshot.active_scopes = snapshot.active_scopes.filter((scope) => scope !== `reading:${track.id}`);
      if (readingAction[2] === "start") snapshot.active_scopes.push(`reading:${track.id}`);
    }
    return json(snapshot);
  }
  if (/\/(methods|trials|responses|events)\?offset=0$/.test(url)) return json({ items: [], next_offset: null });
  throw new Error(`Unexpected fixture request: ${method} ${url}`);
});

beforeEach(() => { snapshot = growthFixture(); overrides = {}; graphPresets = []; vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

async function create() {
  render(<GrowthWorkbench />);
  const button = await screen.findByRole("button", { name: "＋ 创建生长实验" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await screen.findByRole("region", { name: "世界生长控制" });
}

describe("growth and independent reading workbench", () => {
  it("offers bundled cross-sections and saves edited copies under fresh custom IDs", async () => {
    const bundled: GraphPreset = { id: "summer-opening", name: "远山淡影 · 河岸的夏天", version: "builtin-version",
      origin: "builtin", windows: [{ id: "river", label: "河岸", seed: "她刚搬到河岸。", direction: "" }], shared_seed: "夏日午后。" };
    graphPresets = [bundled];
    render(<GrowthWorkbench />);
    await screen.findByRole("option", { name: "远山淡影 · 河岸的夏天 · 内置截面示例" });
    fireEvent.change(screen.getByLabelText("种子来源"), { target: { value: "graph:summer-opening" } });
    expect(screen.getByLabelText("种子 Markdown 1")).toHaveValue("她刚搬到河岸。");
    expect(screen.getByLabelText("生长方向 1（可选）")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("种子 Markdown 1"), { target: { value: "她在河岸遇见新的邻居。" } });
    fireEvent.click(screen.getByRole("button", { name: "另存为我的预设" }));
    await screen.findByRole("button", { name: "保存预设更新" });
    const first = JSON.parse(fetchMock.mock.calls.filter(([url, options]) => url === "/api/graph-presets" && options?.method === "POST").at(-1)![1]!.body as string);
    expect(first.id).not.toBe(bundled.id);
    expect(first.expected_version).toBeUndefined();
    expect(first.windows[0].seed).toBe("她在河岸遇见新的邻居。");
    expect(graphPresets.find((preset) => preset.id === bundled.id)?.windows[0].seed).toBe("她刚搬到河岸。");
    fireEvent.change(screen.getByLabelText("种子来源"), { target: { value: "graph:summer-opening" } });
    fireEvent.click(screen.getByRole("button", { name: "另存为我的预设" }));
    await screen.findByRole("button", { name: "保存预设更新" });
    const second = JSON.parse(fetchMock.mock.calls.filter(([url, options]) => url === "/api/graph-presets" && options?.method === "POST").at(-1)![1]!.body as string);
    expect(second.id).not.toBe(first.id);
    expect(second.expected_version).toBeUndefined();
    expect(fetchMock.mock.calls.some(([url, options]) => url === "/api/graph-runs" && options?.method === "POST")).toBe(false);
  });

  it("retains complete Markdown, optional directions and one shared seed on creation without generating", async () => {
    render(<GrowthWorkbench />);
    await waitFor(() => expect(screen.getByRole("button", { name: "＋ 创建生长实验" })).toBeEnabled());
    const markdown = `# 种子\n\n${"她🌱保留了很长的原始经历。\n".repeat(250)}`;
    fireEvent.change(screen.getByLabelText("种子 Markdown 1"), { target: { value: markdown } });
    fireEvent.change(screen.getByLabelText("共同种子（可选）"), { target: { value: "共同的完整原文" } });
    fireEvent.click(screen.getByRole("button", { name: "＋ 创建生长实验" }));
    await screen.findByRole("region", { name: "世界生长控制" });
    const body = JSON.parse(fetchMock.mock.calls.find(([url, options]) => url === "/api/graph-runs" && options?.method === "POST")![1]!.body as string);
    expect(body.windows[0].seed).toBe(markdown);
    expect(body.windows[0].direction).toBe("");
    expect(body.shared_seed).toBe("共同的完整原文");
    expect(body.provider).toBe("fixture");
    expect(body.limits).toEqual({ max_calls: null, max_tokens: null, max_windows: null, max_parallel: 3 });
    expect(fetchMock.mock.calls.some(([url]) => /\/(start|step)$/.test(url))).toBe(false);
    expect(screen.getAllByRole("button", { name: "共同种子原文 ↗" })).toHaveLength(2);
  });

  it("saves and reloads complete graph seed presets without creating a run or consuming model calls", async () => {
    const view = render(<GrowthWorkbench />);
    await waitFor(() => expect(screen.getByRole("button", { name: "保存为图种子预设" })).toBeEnabled());
    const markdown = `# 原文\n${"她🌱保留这段经历。\n".repeat(250)}`;
    fireEvent.change(screen.getByLabelText("实验名称"), { target: { value: "完整种子预设" } });
    fireEvent.change(screen.getByLabelText("种子 Markdown 1"), { target: { value: markdown } });
    fireEvent.click(screen.getByRole("button", { name: "保存为图种子预设" }));
    await screen.findByText(/“完整种子预设”已保存/);
    expect(graphPresets[0].windows[0].seed).toBe(markdown);
    expect(fetchMock.mock.calls.some(([url, options]) => url === "/api/graph-runs" && options?.method === "POST")).toBe(false);
    expect(fetchMock.mock.calls.some(([url]) => /\/(start|step)$/.test(url))).toBe(false);
    view.unmount();
    render(<GrowthWorkbench />);
    await screen.findByRole("option", { name: "完整种子预设 · 图种子预设" });
    fireEvent.change(screen.getByLabelText("种子来源"), { target: { value: `graph:${graphPresets[0].id}` } });
    expect(screen.getByLabelText("种子 Markdown 1")).toHaveValue(markdown);
    fireEvent.change(screen.getByLabelText("生长方向 1（可选）"), { target: { value: "沿着此前的生活继续" } });
    fireEvent.click(screen.getByRole("button", { name: "保存预设更新" }));
    await screen.findByText(/“完整种子预设”已保存/);
    const saveCalls = fetchMock.mock.calls.filter(([url, options]) => url === "/api/graph-presets" && options?.method === "POST");
    expect(JSON.parse(saveCalls.at(-1)![1]!.body as string).expected_version).toBe("saved-preset-version");
    expect(graphPresets[0].windows[0].direction).toBe("沿着此前的生活继续");
  });

  it("imports an existing preset through the graph API", async () => {
    render(<GrowthWorkbench />);
    await waitFor(() => expect(screen.getByRole("button", { name: "＋ 创建生长实验" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("种子来源"), { target: { value: "preset-1" } });
    fireEvent.click(screen.getByRole("button", { name: "＋ 创建生长实验" }));
    await screen.findByRole("region", { name: "世界生长控制" });
    const body = JSON.parse(fetchMock.mock.calls.find(([url, options]) => url === "/api/graph-runs" && options?.method === "POST")![1]!.body as string);
    expect(body.preset_id).toBe("preset-1");
    expect(body.windows).toBeUndefined();
  });

  it("can read while growth is active and repeatedly traverse a paused graph without changing its commit", async () => {
    await create();
    fireEvent.click(screen.getByRole("button", { name: "开始生长" }));
    await screen.findByRole("button", { name: "暂停生长" });
    expect(screen.getByRole("button", { name: "阅读单步" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "阅读单步" }));
    await screen.findByText("他在街道上重新读那封信。");
    expect(snapshot.graph_commit).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "暂停生长" }));
    await screen.findByRole("button", { name: "开始生长" });
    fireEvent.click(screen.getByRole("button", { name: "阅读单步" }));
    await waitFor(() => expect(snapshot.readings[0].read_seq).toBe(2));
    expect(snapshot.graph_commit).toBe(2);
    expect(snapshot.readings[0].visits).toHaveLength(2);
    expect(screen.getAllByRole("region", { name: "生长阅读正文" })).toHaveLength(2);
    expect(within(screen.getAllByRole("region", { name: "生长阅读正文" })[0]).queryByText(/version-a|Token|text-a/)).not.toBeInTheDocument();
  });

  it("keeps growth controls independent while a reader's single action is awaiting a model result", async () => {
    let finishReading!: (response: Response) => void;
    overrides["POST /api/graph-runs/graph-1/readings/reader-1/step"] = () => new Promise((resolve) => { finishReading = resolve; });
    await create();
    fireEvent.click(screen.getByRole("button", { name: "开始生长" }));
    await screen.findByRole("button", { name: "暂停生长" });
    fireEvent.click(screen.getByRole("button", { name: "阅读单步" }));
    await screen.findByText(/模型正在选择节点并组织阅读/);
    expect(screen.getByRole("button", { name: "暂停生长" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "暂停生长" }));
    await screen.findByRole("button", { name: "开始生长" });
    expect(snapshot.active_scopes).toEqual([]);
    await act(async () => { finishReading(json(snapshot)); });
    await waitFor(() => expect(screen.getByRole("button", { name: "阅读单步" })).toBeEnabled());
  });

  it("saves multiple reading paths and opens exact Unicode source ranges without generation", async () => {
    await create();
    fireEvent.click(screen.getByText("＋ 添加独立阅读"));
    fireEvent.change(screen.getByLabelText("新阅读名称"), { target: { value: "另一条路径" } });
    fireEvent.click(screen.getByRole("button", { name: "保存阅读路径" }));
    await screen.findByRole("button", { name: /另一条路径/ });
    fireEvent.click(screen.getByRole("button", { name: /另一条路径/ }));
    fireEvent.click(screen.getByRole("button", { name: "阅读单步" }));
    await screen.findByText("他在街道上重新读那封信。");
    expect(snapshot.readings[0].read_seq).toBe(0);
    expect(snapshot.readings[1].read_seq).toBe(1);
    const before = fetchMock.mock.calls.filter(([url]) => /\/(start|step)$/.test(url)).length;
    fireEvent.click(screen.getByRole("button", { name: /查看原文.*\[1, 3\)/ }));
    const original = await screen.findByRole("region", { name: "节点原文" });
    await waitFor(() => expect(original.querySelector("mark")).toHaveTextContent("🌱又"));
    expect(original).toHaveTextContent(originalText);
    expect(fetchMock.mock.calls.filter(([url]) => /\/(start|step)$/.test(url))).toHaveLength(before);
  });

  it("preserves cyclic and shared-parent edges in the world graph instead of flattening them into a tree", async () => {
    const local = graphNeighborhood(snapshot.nodes, snapshot.links, "node-grown", 1);
    expect(local.nodes.map((node) => node.id)).toEqual(["node-a", "node-b", "node-grown"]);
    expect(local.links.map((link) => link.id)).toEqual(["growth-a", "growth-b", "cycle", "self"]);
    await create();
    fireEvent.click(screen.getByRole("button", { name: "世界图谱" }));
    await screen.findByRole("img", { name: "世界节点与有向关系" });
    expect(document.querySelectorAll(".world-edge.growth")).toHaveLength(2);
    expect(document.querySelectorAll(".world-edge.association")).toHaveLength(2);
  });

  it("reads archived passages and visits through bounded history pages without running a provider", async () => {
    overrides["GET /api/graph-runs/graph-1/readings/reader-1/passages?offset=0"] = () => json({ items: [{ id: "archived-passage", track_id: "reader-1", read_seq: 1, text_ref: { sha256: "archive-text", length: 8 }, text: "曾经走过的旧路。", visit_ids: ["archived-visit"], source_spans: [span] }], next_offset: null });
    overrides["GET /api/graph-runs/graph-1/readings/reader-1/visits?offset=0"] = () => json({ items: [{ id: "archived-visit", track_id: "reader-1", read_seq: 1, node_id: "node-a", node_version: "version-a", observed_graph_commit: 2, from_node_id: null, via_link_id: null, transition: "entry", source_spans: [span] }], next_offset: null });
    await create();
    fireEvent.click(screen.getByRole("button", { name: "读取阅读历史" }));
    expect(await screen.findByText("曾经走过的旧路。")).toBeInTheDocument();
    expect(screen.getByText("已访问路径 · 已载入 1 次")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "载入更多阅读历史" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => /\/(start|step)$/.test(url))).toBe(false);
  });

  it("refreshes a safe retained checkpoint after a failed action while keeping existing prose readable", async () => {
    await create();
    fireEvent.click(screen.getByRole("button", { name: "阅读单步" }));
    await screen.findByText("他在街道上重新读那封信。");
    overrides["POST /api/graph-runs/graph-1/readings/reader-1/step"] = () => {
      snapshot.readings[0].status = "error";
      snapshot.sessions = [{ id: "reader-session", scope_id: "reading:reader-1", work_seq: 2, last_action: "render", status: "error", error: { code: "MODEL_TIMEOUT", message: "模型请求超时，进度已保留。", retryable: true } }];
      return json({ error: { code: "MODEL_TIMEOUT", message: "模型请求超时，进度已保留。", retryable: true } }, 422);
    };
    fireEvent.click(screen.getByRole("button", { name: "阅读单步" }));
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getByText("他在街道上重新读那封信。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /第一条阅读/ })).toHaveTextContent("需要恢复");
    expect(screen.getByRole("button", { name: "阅读单步" })).toBeEnabled();
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/graph-runs/graph-1")).toBe(true);
  });

  it("shows model work and a waiting reader as distinct recoverable states", async () => {
    snapshot.windows[0].status = "calling";
    snapshot.readings[0].status = "waiting";
    await create();
    expect(screen.getByText("模型正在工作")).toBeInTheDocument();
    expect(screen.getByText(/模型正在窗口内组织下一步/)).toBeInTheDocument();
    expect(screen.getByText(/正在等待可读节点，阅读进度已保留/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "阅读单步" })).toBeEnabled();
  });

  it("keeps methods, candidate text and call diagnostics out of reading prose", async () => {
    overrides["GET /api/graph-runs/graph-1/methods?offset=0"] = () => json({ items: [{ id: "method-1", version: "method-version", scope_id: "window:a", instruction: "先读共同来路，再选新节点。", program: { steps: [] }, payload: "private-provider-payload" }] });
    overrides["GET /api/graph-runs/graph-1/trials?offset=0"] = () => json({ items: [{ id: "trial-1", text: "这是候选试验里的文本。", adopted: false }] });
    await create();
    fireEvent.click(screen.getByRole("button", { name: "方法、试验与回应" }));
    expect(await screen.findByText("先读共同来路，再选新节点。")).toBeInTheDocument();
    expect(await screen.findByText("这是候选试验里的文本。")).toBeInTheDocument();
    expect(screen.queryByText("private-provider-payload")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "运行诊断" }));
    expect(screen.getByRole("region", { name: "生长运行预算" })).toHaveTextContent("已知 Token140");
    fireEvent.click(screen.getByRole("button", { name: "连续阅读" }));
    expect(screen.queryByText("这是候选试验里的文本。")).not.toBeInTheDocument();
  });

  it("polls only while independent work scopes are active and stops after pause", async () => {
    await create();
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "开始阅读" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const before = fetchMock.mock.calls.filter(([url]) => url === "/api/graph-runs/graph-1").length;
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/graph-runs/graph-1")).toHaveLength(before + 1);
    fireEvent.click(screen.getByRole("button", { name: "暂停阅读" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/graph-runs/graph-1")).toHaveLength(before + 1);
  });
});

it("uses Unicode code-point offsets for exact original source highlighting", () => {
  expect(sourceTextParts(originalText, span)).toEqual({ before: "他", selected: "🌱又", after: "进了雨中的街道。" });
});
