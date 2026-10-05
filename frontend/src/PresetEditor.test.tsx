import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import PresetEditor from "./components/PresetEditor";
import { makeDraft, validateDraft } from "./presetDraft";
import type { Preset, Snapshot } from "./types";

const builtin: Preset = {
  id: "letters",
  name: "信的往返",
  description: "几个位置共同承接一封信。",
  positions: [1, 2, 3].map((number) => ({ id: `pos_${number}`, name: `人物${number}`, seed: `# 私人背景\n\n人物${number}有自己的过往。` })),
  expressions: [{ id: "e-letter", text: "一封尚未写完的信。", role: "当前记忆" }],
  origin: "builtin",
  editable: false,
  version: null,
};
const custom: Preset = {
  ...builtin, id: "custom_story", name: "私人实验", origin: "custom", editable: true,
  version: "version-one", storage_path: "/local/runtime/presets/custom_story/versions/version-one",
};
const response = (data: unknown, status = 200) => Promise.resolve({ ok: status < 400, status, json: async () => data } as Response);
let catalog: Preset[];
let fetchMock: ReturnType<typeof vi.fn>;
let savingError: { code: string; message: string } | null;
let catalogErrors: { id: string; code: string; message: string }[];

const snapshot = (): Snapshot => ({
  schema_version: 1,
  run: { id: "run-one", title: "已有实验", provider: "fixture", status: "idle", revision: 0,
    created_at: "", updated_at: "", usage: { calls: 0, input_tokens: 0, output_tokens: 0 }, last_error: null,
    limits: { max_calls: 24, max_tokens: 40000, max_parallel: 3, max_windows: 8 }, phase: "idle", fork_of: null },
  positions: builtin.positions, expressions: [], uses: [], continuations: [], events: [], story: [],
  reading: { focus_position_id: null, open_relations: [], interpretations: [] }, pending: null,
  audit: { shared_expressions: [], effective_edges: [], joint_targets: [], last_batch: [] },
});

beforeEach(() => {
  catalog = [builtin];
  savingError = null;
  catalogErrors = [];
  fetchMock = vi.fn((url: string, options?: RequestInit) => {
    if (url === "/api/config") return response({ configured: false, model: "deepseek-flash" });
    if (url === "/api/presets" && (!options?.method || options.method === "GET")) return response({ presets: catalog, errors: catalogErrors });
    if (url.startsWith("/api/presets") && options?.body) {
      if (savingError) return response({ error: { ...savingError, retryable: false } }, 409);
      const payload = JSON.parse(String(options.body)) as Preset;
      const preset = { ...payload, origin: "custom", editable: true, version: "version-two", storage_path: `/local/runtime/presets/${payload.id}/versions/version-two` };
      catalog = [...catalog.filter((item) => item.id !== preset.id), preset as Preset];
      return response({ preset });
    }
    if (url === "/api/runs" && options?.method === "POST") return response(snapshot());
    if (url === "/api/runs") return response({ runs: [] });
    return response({ error: { code: "NOT_FOUND", message: "测试路由不存在。" } }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

async function open(mode: "create" | "copy" | "edit" = "create") {
  render(<App />);
  const entry = await screen.findByRole("button", { name: "＋ 自定义实验" });
  await waitFor(() => expect(entry).toBeEnabled());
  const button = screen.getByRole("button", { name: mode === "create" ? "＋ 自定义实验" : mode === "copy" ? "复制编辑" : "编辑当前预设" });
  button.focus();
  fireEvent.click(button);
  return screen.getByRole("dialog");
}

function fillBlank() {
  fireEvent.change(screen.getByLabelText("预设名称"), { target: { value: "共用的梦" } });
  for (let number = 1; number <= 3; number++) {
    fireEvent.change(screen.getByLabelText(`人物 ${number} 名称`), { target: { value: `梦者${number}` } });
    fireEvent.change(screen.getByLabelText(`人物 ${number} 私人背景（Markdown）`), { target: { value: `# 梦者${number}\n\n他记得共同的生活。` } });
  }
  fireEvent.change(screen.getByLabelText("共同表达 1 内容"), { target: { value: "一段尚未归属于任何人的共同生活。" } });
}

describe("custom experiment authoring", () => {
  it("provides a discoverable entry and accessible dialog without a credential field", async () => {
    const dialog = await open();
    expect(dialog).toHaveAccessibleName("自定义实验");
    expect(within(dialog).getByText(/保存本身不调用模型/)).toBeInTheDocument();
    expect(screen.getByLabelText("预设名称")).toHaveFocus();
    expect(within(dialog).getAllByLabelText(/私人背景（Markdown）/)).toHaveLength(3);
    expect(within(dialog).queryByLabelText(/API|密钥|key/i)).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "删除人物 1" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "编辑当前预设" })).not.toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "＋ 自定义实验" })).toHaveFocus();
  });

  it("saves long private Markdown and shared expressions, then selects the preset without creating a run", async () => {
    await open();
    fillBlank();
    const markdown = `# 过往\n\n${"他记得这段私人生活。".repeat(700)}`;
    fireEvent.change(screen.getByLabelText("人物 1 私人背景（Markdown）"), { target: { value: markdown } });
    fireEvent.click(screen.getByRole("button", { name: "保存预设" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const saveCall = fetchMock.mock.calls.find(([url, options]) => url === "/api/presets" && options?.method === "POST")!;
    const payload = JSON.parse(saveCall[1].body);
    expect(payload.id).toMatch(/^custom_[a-z0-9]+$/);
    expect(payload.positions[0].seed).toBe(markdown);
    expect(payload.positions).toHaveLength(3);
    expect(payload.expressions).toEqual([{ id: "e_shared_1", text: "一段尚未归属于任何人的共同生活。", role: "共同生活" }]);
    expect(screen.getByLabelText("初始叙事")).toHaveValue(payload.id);
    expect(fetchMock.mock.calls.filter(([url, options]) => url === "/api/runs" && options?.method === "POST")).toHaveLength(0);
    expect(fetchMock.mock.calls.some(([url]) => /\/(step|start)$/.test(url))).toBe(false);
    expect(screen.getByText(`/local/runtime/presets/${payload.id}/versions/version-two`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "＋ 创建实验" }));
    await screen.findByRole("heading", { name: "已有实验" });
    const createCall = fetchMock.mock.calls.find(([url, options]) => url === "/api/runs" && options?.method === "POST")!;
    expect(JSON.parse(createCall[1].body).preset_id).toBe(payload.id);
  });

  it("copies an immutable builtin with all profiles and a fresh ID", async () => {
    await open("copy");
    expect(screen.getByLabelText("预设名称")).toHaveValue("信的往返 · 副本");
    expect(screen.getByLabelText("预设 ID")).not.toHaveValue("letters");
    expect(screen.getByLabelText("人物 2 私人背景（Markdown）")).toHaveValue(builtin.positions[1].seed);
    expect(screen.getByLabelText("共同表达 1 初始用法")).toHaveValue("当前记忆");
    fireEvent.change(screen.getByLabelText("人物 2 名称"), { target: { value: "新人物" } });
    fireEvent.click(screen.getByRole("button", { name: "保存预设" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(builtin.positions[1].name).toBe("人物2");
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
  });

  it("edits custom versions with optimistic concurrency and keeps the draft on conflict", async () => {
    catalog = [custom];
    savingError = { code: "VERSION_CONFLICT", message: "当前预设已被修改。" };
    await open("edit");
    expect(screen.getByLabelText("预设 ID")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("人物 1 私人背景（Markdown）"), { target: { value: "# 新记忆\n\n填写内容需要保留。" } });
    fireEvent.click(screen.getByRole("button", { name: "保存预设" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("VERSION_CONFLICT");
    expect(screen.getByLabelText("人物 1 私人背景（Markdown）")).toHaveValue("# 新记忆\n\n填写内容需要保留。");
    expect(screen.getByRole("button", { name: "保存预设" })).toBeEnabled();
    const saveCall = fetchMock.mock.calls.find(([url, options]) => url === "/api/presets/custom_story" && options?.method === "PUT")!;
    expect(JSON.parse(saveCall[1].body).expected_version).toBe("version-one");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    savingError = null;
    fireEvent.click(screen.getByRole("button", { name: "保留填写，另存为新预设" }));
    expect(screen.getByRole("dialog")).toHaveAccessibleName("复制为自定义实验");
    expect(screen.getByLabelText("人物 1 私人背景（Markdown）")).toHaveValue("# 新记忆\n\n填写内容需要保留。");
    fireEvent.click(screen.getByRole("button", { name: "保存预设" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const copyCall = fetchMock.mock.calls.find(([url, options]) => url === "/api/presets" && options?.method === "POST")!;
    expect(JSON.parse(copyCall[1].body).id).not.toBe(custom.id);
    expect(JSON.parse(copyCall[1].body).expected_version).toBeUndefined();
  });

  it("validates empty profiles and duplicate/reserved IDs before saving", async () => {
    await open();
    fireEvent.click(screen.getByRole("button", { name: "保存预设" }));
    expect(screen.getByRole("alert")).toHaveTextContent("人物 1 的私人背景");
    fillBlank();
    fireEvent.change(screen.getByLabelText("人物 2 ID"), { target: { value: "pos_1" } });
    fireEvent.change(screen.getByLabelText("共同表达 1 ID"), { target: { value: "seed-pos_1" } });
    fireEvent.click(screen.getByRole("button", { name: "保存预设" }));
    expect(screen.getByRole("alert")).toHaveTextContent("重复");
    expect(screen.getByRole("alert")).toHaveTextContent("保留前缀");
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  });

  it("bounds positions and shared expressions and preserves remaining fields after removal", async () => {
    await open();
    fireEvent.change(screen.getByLabelText("人物 1 名称"), { target: { value: "保留下来的姓名" } });
    for (let count = 3; count < 8; count++) fireEvent.click(screen.getByRole("button", { name: "＋ 添加人物" }));
    expect(screen.getByRole("button", { name: "＋ 添加人物" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "删除人物 4" }));
    expect(screen.getByRole("button", { name: "＋ 添加人物" })).toBeEnabled();
    expect(screen.getByLabelText("人物 1 名称")).toHaveValue("保留下来的姓名");
    for (let count = 1; count < 6; count++) fireEvent.click(screen.getByRole("button", { name: "＋ 添加共同表达" }));
    expect(screen.getByRole("button", { name: "＋ 添加共同表达" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "删除共同表达 3" }));
    fireEvent.click(screen.getByRole("button", { name: "＋ 添加共同表达" }));
    const ids = screen.getAllByLabelText(/共同表达 \d+ ID/).map((item) => (item as HTMLInputElement).value);
    expect(new Set(ids).size).toBe(6);
  });

  it("keeps an existing run untouched when a preset is saved", async () => {
    render(<App />);
    const create = await screen.findByRole("button", { name: "＋ 创建实验" });
    await waitFor(() => expect(create).toBeEnabled());
    fireEvent.click(create);
    await screen.findByRole("heading", { name: "已有实验" });
    fireEvent.click(screen.getByRole("button", { name: "复制编辑" }));
    fireEvent.click(screen.getByRole("button", { name: "保存预设" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "已有实验" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url, options]) => url === "/api/runs" && options?.method === "POST")).toHaveLength(1);
  });

  it("retains private drafts after connection failure and allows retry", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("network"));
    render(<PresetEditor mode="copy" source={builtin} onSave={onSave} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("预设名称"), { target: { value: "断线后保留" } });
    fireEvent.click(screen.getByRole("button", { name: "保存预设" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("填写内容已保留");
    expect(screen.getByLabelText("预设名称")).toHaveValue("断线后保留");
    expect(screen.getByRole("button", { name: "保存预设" })).toBeEnabled();
  });

  it("uses code-point bounds and distinct position ID constraints", () => {
    const draft = makeDraft("copy", builtin);
    draft.positions[0].seed = "😀".repeat(12000);
    expect(validateDraft(draft)).toEqual([]);
    draft.positions[0].seed += "字";
    draft.positions[1].id = "pos-two";
    expect(validateDraft(draft).join(" ")).toMatch(/12,000/);
    expect(validateDraft(draft).join(" ")).toMatch(/最多 40/);
  });

  it("keeps valid presets usable when another custom group is damaged", async () => {
    catalogErrors = [{ id: "broken_preset", code: "PRESET_INVALID", message: "无法读取这份自定义预设。" }];
    await open("copy");
    expect(screen.getByText("有 1 份自定义预设未能载入")).toBeInTheDocument();
    expect(screen.getByLabelText("预设名称")).toHaveValue("信的往返 · 副本");
  });

  it("keeps keyboard focus within the modal", async () => {
    await open();
    const save = screen.getByRole("button", { name: "保存预设" });
    const close = screen.getByRole("button", { name: "关闭自定义实验编辑器" });
    save.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(save).toHaveFocus();
  });
});
