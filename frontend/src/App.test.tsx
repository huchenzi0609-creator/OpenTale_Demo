import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { ReadingTrack, Segment, Snapshot } from "./types";

const fixture = (): Snapshot => ({
  schema_version: 1,
  run: {
    id: "run-1",
    title: "共同表达实验",
    provider: "fixture",
    status: "idle",
    revision: 0,
    created_at: "2026-10-05T00:00:00Z",
    updated_at: "2026-10-05T00:00:00Z",
    usage: { calls: 0, input_tokens: 0, output_tokens: 0 },
    last_error: null,
    limits: {
      max_calls: 24,
      max_tokens: 40000,
      max_parallel: 3,
      max_windows: 8,
    },
    phase: "idle",
    fork_of: null,
  },
  positions: [
    { id: "a", name: "写信人", seed: "他的初始生活" },
    { id: "b", name: "读信人", seed: "她的初始生活" },
    { id: "c", name: "旁观者", seed: "另一个位置" },
  ],
  expressions: [
    {
      id: "shared",
      text: "一封尚未写完的信。",
      created_revision: 0,
      source_position_id: null,
    },
  ],
  uses: [
    {
      id: "use-a",
      position_id: "a",
      expression_id: "shared",
      role: "当前记忆",
      revision: 0,
      bindings: [
        {
          source_position_id: "b",
          source_expression_id: "internal-source-239",
          value: "已经有人读过这封信。",
          anchor: "读过",
          revision: 0,
        },
      ],
    },
  ],
  continuations: [
    {
      position_id: "a",
      frame_revision: 0,
      status: "ready",
      seed_text: "未完成文本",
      condition_fingerprint: "fingerprint-original",
      dependencies: ["shared"],
      surface: "",
      resume_count: 0,
    },
  ],
  events: [
    {
      id: "event-1",
      type: "created",
      revision: 0,
      at: "",
      data: { debug: "internal-source-239" },
    },
  ],
  story: [],
  reading: { focus_position_id: null, open_relations: [], interpretations: [] },
  pending: null,
  audit: {
    shared_expressions: [],
    effective_edges: [],
    joint_targets: [],
    last_batch: [],
  },
});

const response = (data: unknown, status = 200) =>
  Promise.resolve({
    ok: status < 400,
    status,
    json: async () => data,
  } as Response);
let fetchMock: ReturnType<typeof vi.fn>;
let snapshot: Snapshot;
let requestOverrides: Record<string, () => Promise<Response>>;

beforeEach(() => {
  snapshot = fixture();
  requestOverrides = {};
  fetchMock = vi.fn((url: string, options?: RequestInit) => {
    const key = `${options?.method ?? "GET"} ${url}`;
    if (requestOverrides[key]) return requestOverrides[key]();
    if (url === "/api/config")
      return response({
        provider: "deepseek",
        configured: false,
        model: "deepseek-flash",
        timeout_seconds: 120,
        max_output_tokens: 2048,
      });
    if (url === "/api/presets")
      return response({
        presets: [
          {
            id: "letters",
            name: "信的往返",
            description: "三个位置共同承接一封信。",
            positions: snapshot.positions,
            expressions: snapshot.expressions,
          },
        ],
      });
    if (url === "/api/runs" && options?.method === "POST")
      return response(snapshot);
    if (url === "/api/runs") return response({ runs: [snapshot.run] });
    if (url === "/api/runs/run-1/step")
      return response({
        ...snapshot,
        run: { ...snapshot.run, revision: 1 },
        story: [
          {
            id: "segment-1",
            revision: 1,
            text: "他拿起信，窗外的雨停了。",
            focus_position_id: "a",
            expression_ids: ["shared"],
            reinterprets: [],
          },
        ],
      });
    if (url === "/api/runs/run-1/start")
      return response({
        ...snapshot,
        run: { ...snapshot.run, status: "running", phase: "drafting" },
      });
    if (url === "/api/runs/run-1/pause")
      return response({
        ...snapshot,
        run: { ...snapshot.run, status: "paused" },
      });
    if (url === "/api/runs/run-1") return response(snapshot);
    return response(
      {
        error: {
          code: "NOT_FOUND",
          message: "测试路由不存在。",
          retryable: false,
        },
      },
      404,
    );
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function createRun() {
  render(<App />);
  await screen.findByRole("button", { name: "＋ 创建实验" });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "＋ 创建实验" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "＋ 创建实验" }));
  await screen.findByRole("heading", { name: "共同表达实验" });
}

const segment = (id: string, text: string, focus = "a", revision = 1): Segment => ({
  id, revision, text, focus_position_id: focus, expression_ids: ["shared"], reinterprets: [],
});
const reading = (id: string, title: string, start: string | null,
  story: Segment[] = [], enabled = true): ReadingTrack => ({
  id, title, start_position_id: start, created_revision: 0, enabled, story,
  reading: { focus_position_id: story.at(-1)?.focus_position_id ?? null, open_relations: [], interpretations: [] },
  last_error: null,
});

describe("parallel readings", () => {
  beforeEach(() => {
    snapshot.continuations = snapshot.positions.map((position) => ({
      position_id: position.id, frame_revision: 1, status: "ready", seed_text: "",
      condition_fingerprint: "private-frame", dependencies: ["shared"],
      surface: `${position.name}当前的续延`, resume_count: 1,
    }));
  });
  it("creates a different starting reading without generating or copying the primary history", async () => {
    snapshot.schema_version = 2;
    snapshot.run.revision = 1;
    snapshot.story = [segment("primary", "主阅读已经写下的历史。")];
    snapshot.reading_tracks = [reading("main", "主阅读", "a", snapshot.story)];
    requestOverrides["POST /api/runs/run-1/readings"] = () => response({ ...snapshot,
      reading_tracks: [...snapshot.reading_tracks!, reading("secondary", "读信人的阅读", "b")] });
    await createRun();
    fireEvent.click(screen.getByRole("button", { name: "＋ 新建阅读" }));
    const form = screen.getByRole("form", { name: "新建独立阅读" });
    expect(within(form).getByText(/新增只保存阅读设置，不调用模型/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText("阅读名称"), { target: { value: "读信人的阅读" } });
    fireEvent.change(within(form).getByLabelText("阅读起始人物"), { target: { value: "b" } });
    fireEvent.click(within(form).getByRole("button", { name: "创建阅读（不调用模型）" }));
    await screen.findByRole("region", { name: "阅读：读信人的阅读" });
    expect(screen.getByLabelText("选择阅读")).toHaveValue("secondary");
    expect(screen.queryByText("主阅读已经写下的历史。")).not.toBeInTheDocument();
    expect(screen.getByText(/起始位置：读信人/)).toBeInTheDocument();
    expect(screen.getByText(/完整窗口通常需要 8 次调用/)).toBeInTheDocument();
    const create = fetchMock.mock.calls.find(([url]) => url === "/api/runs/run-1/readings");
    expect(JSON.parse(create![1]!.body as string)).toEqual({ title: "读信人的阅读", start_position_id: "b", enabled: true });
    expect(fetchMock.mock.calls.some(([url]) => /\/(step|start|present)$/.test(url))).toBe(false);
    await waitFor(() => expect(screen.queryByRole("form", { name: "新建独立阅读" })).not.toBeInTheDocument());
  });

  it("manually presents just the selected reading and prevents a duplicate for the same revision", async () => {
    snapshot.run.revision = 1;
    snapshot.reading_tracks = [reading("main", "主阅读", "a", [segment("first", "主阅读的正文。")]),
      reading("secondary", "读信人的阅读", "b")];
    requestOverrides["POST /api/runs/run-1/readings/secondary/present"] = () => response({ ...snapshot,
      run: { ...snapshot.run, usage: { calls: 1, input_tokens: 100, output_tokens: 40 } },
      reading_tracks: [snapshot.reading_tracks![0], reading("secondary", "读信人的阅读", "b",
        [segment("second", "她从回信的背面，想起窗外的雨。", "b")])] });
    await createRun();
    fireEvent.change(screen.getByLabelText("选择阅读"), { target: { value: "secondary" } });
    fireEvent.click(screen.getByRole("button", { name: "从当前世界生成一段" }));
    await screen.findByText("她从回信的背面，想起窗外的雨。");
    expect(screen.getByRole("button", { name: "本版本已呈现" })).toBeDisabled();
    expect(screen.queryByText("主阅读的正文。")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith("/present"))).toHaveLength(1);
    expect(fetchMock.mock.calls.some(([url]) => /\/(step|start)$/.test(url))).toBe(false);
    expect(screen.getByRole("region", { name: "运行状态" })).toHaveTextContent("R1");
    expect(screen.getByRole("region", { name: "运行状态" })).toHaveTextContent("140 / 40,000");
  });

  it("serializes the final world after the window limit without requiring another world window", async () => {
    snapshot.run.revision = 1;
    snapshot.run.status = "budget_exhausted";
    snapshot.run.limits.max_windows = 1;
    snapshot.run.usage.windows = 1;
    snapshot.reading_tracks = [reading("main", "主阅读", "a", [segment("first", "已完成世界的主阅读。")]),
      reading("secondary", "读信人的阅读", "b")];
    requestOverrides["POST /api/runs/run-1/readings/secondary/present"] = () => response({ ...snapshot,
      run: { ...snapshot.run, usage: { ...snapshot.run.usage, calls: 1 } },
      reading_tracks: [snapshot.reading_tracks![0], reading("secondary", "读信人的阅读", "b",
        [segment("second", "她的阅读从最终的世界开始。", "b")])] });
    await createRun();
    expect(screen.getByRole("button", { name: "单步" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "＋ 新建阅读" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("选择阅读"), { target: { value: "secondary" } });
    const present = screen.getByRole("button", { name: "从当前世界生成一段" });
    expect(present).toBeEnabled();
    fireEvent.click(present);
    await screen.findByText("她的阅读从最终的世界开始。");
    expect(screen.getByRole("region", { name: "运行状态" })).toHaveTextContent("1 / 1");
    expect(screen.getByRole("region", { name: "运行状态" })).toHaveTextContent("R1");
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith("/present"))).toHaveLength(1);
    expect(fetchMock.mock.calls.some(([url]) => /\/(step|start)$/.test(url))).toBe(false);
  });

  it.each(["calls", "tokens"])("prevents manual presentation when actual %s budget is consumed", async (budget) => {
    snapshot.run.revision = 1;
    snapshot.reading_tracks = [reading("main", "主阅读", "a")];
    if (budget === "calls") snapshot.run.usage.calls = snapshot.run.limits.max_calls;
    else snapshot.run.usage = { ...snapshot.run.usage, input_tokens: 1000, reserved_tokens: 1000, unknown_tokens: 38000 };
    await createRun();
    expect(screen.getByRole("button", { name: "从当前世界生成一段" })).toBeDisabled();
    expect(screen.getByText(/调用或 Token 预算已用尽/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/present"))).toBe(false);
  });

  it("toggles automatic continuation without generation and retains each reading's prose", async () => {
    snapshot.run.revision = 1;
    snapshot.reading_tracks = [reading("main", "主阅读", "a", [segment("first", "主阅读的正文。")]),
      reading("secondary", "读信人的阅读", "b", [segment("second", "她读到了另一种过去。", "b")])];
    requestOverrides["PATCH /api/runs/run-1/readings/secondary"] = () => response({ ...snapshot,
      reading_tracks: snapshot.reading_tracks!.map((track) => track.id === "secondary" ? { ...track, enabled: false } : track) });
    await createRun();
    fireEvent.change(screen.getByLabelText("选择阅读"), { target: { value: "secondary" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "读信人的阅读自动续写" }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "读信人的阅读自动续写" })).not.toBeChecked());
    expect(screen.getByText("她读到了另一种过去。")).toBeInTheDocument();
    expect(screen.getByText(/完整窗口通常需要 7 次调用/)).toBeInTheDocument();
    const patch = fetchMock.mock.calls.find(([url]) => url === "/api/runs/run-1/readings/secondary");
    expect(patch![1]!.method).toBe("PATCH");
    expect(JSON.parse(patch![1]!.body as string)).toEqual({ enabled: false });
    expect(fetchMock.mock.calls.some(([url]) => /\/(step|start|present)$/.test(url))).toBe(false);
  });

  it("compares all reading paths side by side without requests or exposing technical bindings", async () => {
    snapshot.run.revision = 1;
    snapshot.reading_tracks = [reading("main", "主阅读", "a", [segment("first", "主阅读的正文。")]),
      reading("secondary", "读信人的阅读", "b", [segment("second", "她读到了另一种过去。", "b")]),
      reading("third", "旁观者的阅读", "c", [segment("third", "两封信的边缘在他眼里重合。", "c")])];
    await createRun();
    const before = fetchMock.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "并排对照" }));
    for (const title of ["主阅读", "读信人的阅读", "旁观者的阅读"])
      expect(screen.getByRole("region", { name: `阅读：${title}` })).toBeInTheDocument();
    const prose = screen.getByRole("region", { name: "叙事正文" });
    expect(prose).toHaveTextContent("两封信的边缘在他眼里重合。");
    expect(prose).not.toHaveTextContent("internal-source-239");
    expect(prose).not.toHaveTextContent("fingerprint-original");
    expect(fetchMock.mock.calls).toHaveLength(before);
    fireEvent.click(screen.getByRole("button", { name: "返回单条阅读" }));
    expect(screen.queryByText("她读到了另一种过去。")).not.toBeInTheDocument();
  });

  it.each(["empty surface", "pending status", "missing current realization", "stale frame"])(
    "prevents presentation of incomplete current continuations: %s", async (incomplete) => {
    snapshot.run.revision = 1;
    snapshot.pending = { base_revision: 0, committed: true, proposals: {},
      realizations: { a: "one", b: "two", c: "three" }, presentations: {} };
    if (incomplete === "empty surface") snapshot.continuations[1].surface = "";
    if (incomplete === "pending status") snapshot.continuations[1].status = "pending";
    if (incomplete === "missing current realization") delete snapshot.pending.realizations.b;
    if (incomplete === "stale frame") snapshot.continuations[1].frame_revision = 0;
    snapshot.reading_tracks = [reading("main", "主阅读", "a")];
    await createRun();
    expect(screen.getByRole("button", { name: "从当前世界生成一段" })).toBeDisabled();
    expect(screen.getByText(/请先完成人物续延/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "＋ 新建阅读" })).toBeEnabled();
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/present"))).toBe(false);
  });

  it("allows the complete current world while the next batch is drafted but not installed", async () => {
    snapshot.run.status = "paused";
    snapshot.run.revision = 1;
    snapshot.pending = { base_revision: 1, committed: false,
      proposals: { a: { surface: "下一版本的未安装提案" } }, realizations: {}, presentations: {} };
    snapshot.reading_tracks = [reading("main", "主阅读", "a"), reading("secondary", "读信人的阅读", "b")];
    await createRun();
    fireEvent.change(screen.getByLabelText("选择阅读"), { target: { value: "secondary" } });
    expect(screen.getByRole("button", { name: "从当前世界生成一段" })).toBeEnabled();
    expect(screen.getByText(/构想 1\/3/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/present"))).toBe(false);
  });

  it("shows per-reading failure and partial presentation progress without losing successful prose", async () => {
    snapshot.run.status = "error";
    snapshot.run.revision = 2;
    snapshot.continuations.forEach((continuation) => { continuation.frame_revision = 2; });
    snapshot.pending = { base_revision: 1, committed: true, proposals: {},
      realizations: { a: "one", b: "two", c: "three" }, presentations: { main: { text: "主阅读已完成。" } } };
    snapshot.reading_tracks = [reading("main", "主阅读", "a", [segment("first", "主阅读已完成。", "a", 2)]),
      { ...reading("secondary", "读信人的阅读", "b"), last_error: { code: "MODEL_OUTPUT_INVALID", message: "阅读承接需要重试。", retryable: true } }];
    await createRun();
    expect(screen.getByText(/呈现 1\/2/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "并排对照" }));
    expect(screen.getByText("主阅读已完成。")).toBeInTheDocument();
    const failed = screen.getByRole("region", { name: "阅读：读信人的阅读" });
    expect(failed).toHaveTextContent("阅读承接需要重试。");
    expect(within(failed).getByRole("button", { name: "从当前世界生成一段" })).toBeEnabled();
    expect(within(screen.getByRole("region", { name: "阅读：主阅读" })).getByRole("button", { name: "本版本已呈现" })).toBeDisabled();
  });

  it("locks paid generation and configuration during an active world but permits local comparison", async () => {
    snapshot.run.status = "running";
    snapshot.run.phase = "presenting";
    snapshot.run.revision = 1;
    snapshot.reading_tracks = [reading("main", "主阅读", "a"), reading("secondary", "读信人的阅读", "b")];
    await createRun();
    expect(screen.getByRole("button", { name: "＋ 新建阅读" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "主阅读自动续写" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "从当前世界生成一段" })).toBeDisabled();
    expect(screen.getByText(/本条阅读等待或正在呈现/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "并排对照" })).toBeEnabled();
  });
});

describe("OpenTale workbench", () => {
  it("shows a fork's inherited revision independently of its reset window budget", async () => {
    snapshot.run.revision = 4;
    snapshot.run.fork_of = "parent-run";
    snapshot.run.usage.windows = 0;
    await createRun();
    const metrics = screen.getByRole("region", { name: "运行状态" });
    const version = within(metrics).getByText("当前版本").parentElement!;
    const windows = within(metrics).getByText("窗口预算").parentElement!;
    expect(version).toHaveTextContent("R4");
    expect(version).not.toHaveTextContent("/ 8");
    expect(windows).toHaveTextContent("0 / 8");
    expect(screen.queryByText("待开始")).not.toBeInTheDocument();
  });

  it("reexecutes recorded state offline, separates diagnostics and clears stale results", async () => {
    requestOverrides["GET /api/runs/run-1/replay"] = () =>
      response({
        ok: true,
        digest: "0123456789abcdef9876543210abcdef",
        checks: [
          { name: "作用关系一致", passed: true },
          { name: "阅读状态一致", passed: true },
        ],
        note: "独立副本重执行，不调用模型。",
      });
    await createRun();
    fireEvent.click(screen.getByRole("button", { name: "验证与诊断" }));
    fireEvent.click(screen.getByRole("button", { name: "验证记录重执行" }));
    expect(await screen.findByText("✓ 记录重执行一致")).toBeInTheDocument();
    expect(screen.getByText("状态摘要：0123456789abcdef")).toBeInTheDocument();
    expect(screen.getByText("作用关系一致")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/runs/run-1/replay"),
    ).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /连续阅读/ }));
    expect(
      within(screen.getByRole("region", { name: "叙事正文" })).queryByText(
        /状态摘要/,
      ),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "单步" }));
    await screen.findByText("他拿起信，窗外的雨停了。");
    fireEvent.click(screen.getByRole("button", { name: "验证与诊断" }));
    expect(screen.queryByText("✓ 记录重执行一致")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "开始运行" }));
    await screen.findByRole("button", { name: "暂停" });
    expect(
      screen.getByRole("button", { name: "验证记录重执行" }),
    ).toBeDisabled();
  });

  it("validates budget bounds and includes reserved and unknown usage in occupancy", async () => {
    snapshot.run.usage = {
      calls: 2,
      input_tokens: 100,
      output_tokens: 50,
      reserved_tokens: 200,
      unknown_tokens: 300,
      failed_calls: 1,
    };
    await createRun();
    expect(screen.getByText("650")).toBeInTheDocument();
    expect(screen.getByText(/未知用量保守记账 300/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("调用上限"), {
      target: { value: "1001" },
    });
    expect(screen.getByRole("button", { name: "＋ 创建实验" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "＋ 创建实验" })).toHaveAccessibleDescription(/预算须为范围内的正整数/);
    fireEvent.change(screen.getByLabelText("调用上限"), {
      target: { value: "12" },
    });
    expect(screen.getByRole("button", { name: "＋ 创建实验" })).toBeEnabled();
  });

  it("creates an offline run with explicit limits and no key input", async () => {
    await createRun();
    const call = fetchMock.mock.calls.find(
      ([url, options]) => url === "/api/runs" && options?.method === "POST",
    );
    expect(JSON.parse(call![1]!.body as string)).toEqual({
      preset_id: "letters",
      provider: "fixture",
      limits: {
        max_calls: 24,
        max_tokens: 128000,
        max_parallel: 3,
        max_windows: 8,
      },
    });
    expect(document.querySelector("input[type=password]")).toBeNull();
    fireEvent.click(screen.getByText("查看安全配置方式"));
    expect(
      screen.getByText(
        "/Users/huchenzi/Ready4AI/Project_OpenTale/Opentale_Demo/.env.local",
      ),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("生成方式"), {
      target: { value: "deepseek" },
    });
    expect(screen.getByRole("button", { name: "＋ 创建实验" })).toBeDisabled();
  });

  it("allows the 5.12M fallback budget while rejecting an excess or a fractional limit", async () => {
    render(<App />);
    const create = await screen.findByRole("button", { name: "＋ 创建实验" });
    await waitFor(() => expect(create).toBeEnabled());
    expect(screen.getByLabelText("Token 上限")).toHaveValue(128000);
    for (const [label, value] of [["Token 上限", 5120000], ["调用上限", 1000],
      ["并行位置", 8], ["窗口上限", 1000]] as const) {
      fireEvent.change(screen.getByLabelText(label), { target: { value: String(value) } });
      expect(create).toBeEnabled();
    }
    fireEvent.change(screen.getByLabelText("Token 上限"), { target: { value: "5120001" } });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription(/5,120,000/);
    fireEvent.change(screen.getByLabelText("Token 上限"), { target: { value: "5120000" } });
    fireEvent.change(screen.getByLabelText("调用上限"), { target: { value: "1.5" } });
    expect(create).toBeDisabled();
  });

  it("takes budget bounds from safe server configuration and refreshes them on reload", async () => {
    const safeConfig = { provider: "deepseek", configured: true, model: "deepseek-flash",
      timeout_seconds: 120, max_output_tokens: 4000 };
    requestOverrides["GET /api/config"] = () => response({ ...safeConfig,
      budget_bounds: { max_calls: 300, max_tokens: 200000, max_parallel: 5, max_windows: 250 } });
    requestOverrides["POST /api/config/reload"] = () => response({ ...safeConfig,
      budget_bounds: { max_calls: 1000, max_tokens: 512000, max_parallel: 8, max_windows: 1000 } });
    render(<App />);
    const create = await screen.findByRole("button", { name: "＋ 创建实验" });
    await waitFor(() => expect(screen.getByLabelText("Token 上限")).toHaveAttribute("max", "200000"));
    fireEvent.change(screen.getByLabelText("Token 上限"), { target: { value: "512000" } });
    expect(create).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重新加载配置" }));
    await waitFor(() => expect(screen.getByLabelText("Token 上限")).toHaveAttribute("max", "512000"));
    expect(create).toBeEnabled();
    expect(screen.getByLabelText("调用上限")).toHaveAttribute("max", "1000");
  });

  it("loads the selected save's own limits and cancels budget edits without requests", async () => {
    snapshot.run.limits = { max_calls: 75, max_tokens: 180000, max_parallel: 4, max_windows: 25 };
    render(<App />);
    await waitFor(() => expect(screen.getByLabelText("选择存档")).toHaveTextContent("共同表达实验"));
    fireEvent.change(screen.getByLabelText("选择存档"), { target: { value: "run-1" } });
    await screen.findByRole("heading", { name: "共同表达实验" });
    expect(screen.getByLabelText("Token 上限")).toHaveValue(180000);
    expect(screen.getByLabelText("调用上限")).toHaveValue(75);
    expect(screen.getByLabelText("并行位置")).toHaveValue(4);
    expect(screen.getByLabelText("窗口上限")).toHaveValue(25);
    expect(screen.getByRole("button", { name: "更新本实验预算" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Token 上限"), { target: { value: "512000" } });
    expect(screen.getByRole("button", { name: "更新本实验预算" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "恢复已保存预算" }));
    expect(screen.getByLabelText("Token 上限")).toHaveValue(180000);
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === "PATCH" || options?.method === "POST")).toBe(false);
  });

  it("updates an exhausted save in place, preserving usage and the pending recovery point without generation", async () => {
    snapshot.run.status = "budget_exhausted";
    snapshot.run.usage = { calls: 16, input_tokens: 21000, output_tokens: 1200, windows: 2 };
    snapshot.pending = { base_revision: 0, proposals: { b: { surface: "已保存的构想" } },
      realizations: {}, committed: false };
    requestOverrides["PATCH /api/runs/run-1/limits"] = () => response({ ...snapshot,
      run: { ...snapshot.run, status: "paused", limits: { ...snapshot.run.limits, max_tokens: 512000 } } });
    await createRun();
    expect(screen.getByRole("button", { name: "恢复一个窗口" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Token 上限"), { target: { value: "512000" } });
    fireEvent.click(screen.getByRole("button", { name: "更新本实验预算" }));
    await screen.findByText(/本实验预算已更新/);
    expect(screen.getByRole("button", { name: "恢复一个窗口" })).toBeEnabled();
    expect(screen.getByRole("region", { name: "运行状态" })).toHaveTextContent("22,200 / 512,000");
    expect(screen.getByText(/构想 1\/3/)).toBeInTheDocument();
    expect(screen.getByLabelText("选择存档")).toHaveValue("run-1");
    const patch = fetchMock.mock.calls.filter(([, options]) => options?.method === "PATCH");
    expect(patch).toHaveLength(1);
    expect(JSON.parse(patch[0][1]!.body as string)).toEqual({ ...snapshot.run.limits, max_tokens: 512000 });
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/start") || url.endsWith("/step"))).toBe(false);
    expect(fetchMock.mock.calls.filter(([url, options]) => url === "/api/runs" && options?.method === "POST")).toHaveLength(1);
  });

  it("prevents limits below used calls, total token occupancy, or completed windows", async () => {
    snapshot.run.usage = { calls: 20, input_tokens: 100, output_tokens: 50,
      reserved_tokens: 200, unknown_tokens: 300, windows: 5 };
    await createRun();
    const update = screen.getByRole("button", { name: "更新本实验预算" });
    for (const [label, invalid, original] of [["调用上限", "19", "24"],
      ["Token 上限", "649", "40000"], ["窗口上限", "4", "8"]]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value: invalid } });
      expect(update).toBeDisabled();
      expect(screen.getByText(/上限不能低于本档已用量/)).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText(label), { target: { value: original } });
    }
    fireEvent.change(screen.getByLabelText("Token 上限"), { target: { value: "650" } });
    expect(update).toBeEnabled();
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === "PATCH")).toBe(false);
  });

  it("reports a rejected budget update in the budget form and leaves the save recoverable", async () => {
    requestOverrides["PATCH /api/runs/run-1/limits"] = () => response({ error: {
      code: "BUDGET_BELOW_USAGE", message: "预算不能低于当前用量。", retryable: false } }, 422);
    await createRun();
    fireEvent.change(screen.getByLabelText("Token 上限"), { target: { value: "512000" } });
    fireEvent.click(screen.getByRole("button", { name: "更新本实验预算" }));
    const budget = screen.getByRole("group", { name: "实验预算" });
    expect(await within(budget).findByRole("alert")).toHaveTextContent("BUDGET_BELOW_USAGE");
    expect(within(screen.getByRole("main")).queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "单步" })).toBeEnabled();
    expect(screen.getByRole("region", { name: "运行状态" })).toHaveTextContent("0 / 40,000");
    fireEvent.click(screen.getByRole("button", { name: "恢复已保存预算" }));
    expect(within(budget).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("disables budget editing while any experiment is running", async () => {
    await createRun();
    fireEvent.change(screen.getByLabelText("Token 上限"), { target: { value: "512000" } });
    fireEvent.click(screen.getByRole("button", { name: "开始运行" }));
    await screen.findByRole("button", { name: "暂停" });
    expect(screen.getByLabelText("Token 上限")).toBeDisabled();
    expect(screen.getByRole("button", { name: "更新本实验预算" })).toBeDisabled();
    expect(screen.getByText("请先暂停运行中的实验，再修改预算。")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === "PATCH")).toBe(false);
  });

  it("also prevents budget edits when a different save remains active", async () => {
    requestOverrides["GET /api/runs"] = () => response({ runs: [snapshot.run,
      { ...snapshot.run, id: "other-run", status: "running", title: "另一份运行中的实验" }] });
    await createRun();
    expect(screen.getByRole("button", { name: "开始运行" })).toBeEnabled();
    expect(screen.getByLabelText("Token 上限")).toBeDisabled();
    expect(screen.getByLabelText("调用上限")).toBeDisabled();
    expect(screen.getByRole("button", { name: "更新本实验预算" })).toBeDisabled();
    expect(screen.getByText("请先暂停运行中的实验，再修改预算。")).toBeInTheDocument();
  });

  it("explains missing live configuration next to creation and enables it after a safe reload", async () => {
    requestOverrides["POST /api/config/reload"] = () => response({
      provider: "deepseek", configured: true, model: "deepseek-flash",
      timeout_seconds: 120, max_output_tokens: 2048,
    });
    render(<App />);
    const setup = screen.getByRole("region", { name: "创建实验" });
    await waitFor(() => expect(within(setup).getByRole("button", { name: "＋ 创建实验" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("生成方式"), { target: { value: "deepseek" } });
    const create = within(setup).getByRole("button", { name: "＋ 创建实验" });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription(/DeepSeek 尚未配置/);
    expect(within(setup).getByText(/DeepSeek 尚未配置/)).toBeInTheDocument();
    expect(within(setup).getByText("OPENTALE_API_KEY")).toBeInTheDocument();
    expect(within(setup).getByText(/Opentale_Demo\/\.env.local/)).toBeInTheDocument();
    fireEvent.click(create);
    expect(fetchMock.mock.calls.some(([url, options]) => url === "/api/runs" && options?.method === "POST")).toBe(false);
    fireEvent.click(within(setup).getByRole("button", { name: "刷新模型配置" }));
    await waitFor(() => expect(create).toBeEnabled());
    expect(within(setup).queryByText(/DeepSeek 尚未配置/)).not.toBeInTheDocument();
    fireEvent.click(create);
    await screen.findByRole("region", { name: "运行控制" });
    const request = fetchMock.mock.calls.find(([url, options]) => url === "/api/runs" && options?.method === "POST");
    expect(JSON.parse(request![1]!.body as string).provider).toBe("deepseek");
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/start") || url.endsWith("/step"))).toBe(false);
    expect(document.querySelector("input[type=password]")).toBeNull();
  });

  it("shows a creation failure locally once and preserves the existing experiment", async () => {
    await createRun();
    requestOverrides["POST /api/runs"] = () => response({
      error: { code: "PRESET_NOT_FOUND", message: "所选预设已不存在，请重新选择。", retryable: false },
    }, 404);
    fireEvent.click(screen.getByRole("button", { name: "＋ 创建实验" }));
    const setup = screen.getByRole("region", { name: "创建实验" });
    expect(await within(setup).findByRole("alert")).toHaveTextContent("PRESET_NOT_FOUND");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(within(screen.getByRole("main")).queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "运行控制" })).toHaveTextContent("共同表达实验");
    expect(within(setup).getByRole("button", { name: "＋ 创建实验" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("调用上限"), { target: { value: "16" } });
    expect(within(setup).queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([
    {
      name: "an empty environment override",
      diagnostics: {
        config_file_exists: true, file_api_key_present: true,
        environment_api_key_override: true, environment_api_key_empty_override: true,
      },
      explanation: /空环境变量覆盖了本机文件/,
      remedy: /启动本地服务时移除/,
    },
    {
      name: "a missing standard credential in the saved file",
      diagnostics: {
        config_file_exists: true, file_api_key_present: false,
        environment_api_key_override: false, environment_api_key_empty_override: false,
      },
      explanation: /未读取到非空的 OPENTALE_API_KEY/,
      remedy: /并确认已保存/,
    },
    {
      name: "a missing configuration file",
      diagnostics: {
        config_file_exists: false, file_api_key_present: false,
        environment_api_key_override: false, environment_api_key_empty_override: false,
      },
      explanation: /当前服务未找到模型配置文件/,
      remedy: /创建配置文件/,
    },
  ])("explains $name using safe server diagnostics and the actual configuration path", async ({ diagnostics, explanation, remedy }) => {
    const actualPath = "/private/local-experiment/.env.local";
    requestOverrides["GET /api/config"] = () => response({
      provider: "deepseek", configured: false, model: "deepseek-flash",
      timeout_seconds: 120, max_output_tokens: 2048,
      diagnostics: { ...diagnostics, config_file_path: actualPath, credential_source: "none" },
    });
    render(<App />);
    await waitFor(() => expect(screen.getByRole("button", { name: "＋ 创建实验" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("生成方式"), { target: { value: "deepseek" } });
    const setup = screen.getByRole("region", { name: "创建实验" });
    const create = within(setup).getByRole("button", { name: "＋ 创建实验" });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription(explanation);
    expect(within(setup).getByText(remedy)).toBeInTheDocument();
    expect(within(setup).getByText(actualPath)).toBeInTheDocument();
    expect(document.querySelector("input[type=password]")).toBeNull();
    expect(fetchMock.mock.calls.some(([url, options]) => url === "/api/runs" && options?.method === "POST")).toBe(false);
  });

  it("focuses and reveals newly created run controls without jumping on later updates", async () => {
    const scroll = vi.fn();
    const descriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scroll });
    try {
      await createRun();
      const controls = screen.getByRole("region", { name: "运行控制" });
      expect(controls).toHaveFocus();
      expect(scroll).toHaveBeenCalledTimes(1);
      expect(scroll.mock.contexts[0]).toBe(controls);
      fireEvent.click(screen.getByRole("button", { name: "单步" }));
      await screen.findByText("他拿起信，窗外的雨停了。");
      expect(scroll).toHaveBeenCalledTimes(1);
    } finally {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", descriptor);
      else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
    }
  });

  it("steps into prose while keeping technical provenance in its separate projection", async () => {
    await createRun();
    fireEvent.click(screen.getByRole("button", { name: "单步" }));
    expect(
      await screen.findByText("他拿起信，窗外的雨停了。"),
    ).toBeInTheDocument();
    const prose = screen.getByRole("region", { name: "叙事正文" });
    expect(
      within(prose).queryByText(/internal-source-239/),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "共同关系" }));
    expect(screen.getByText("fingerprint-original")).toBeInTheDocument();
    fireEvent.click(screen.getByText("使用关系与来源"));
    expect(screen.getByText("internal-source-239")).toBeInTheDocument();
  });

  it("retains readable partial state after a safe step failure", async () => {
    snapshot.story = [
      {
        id: "segment-0",
        revision: 0,
        text: "已经留下的一段生活。",
        focus_position_id: "a",
        expression_ids: ["shared"],
        reinterprets: [],
      },
    ];
    requestOverrides["POST /api/runs/run-1/step"] = () =>
      response(
        {
          error: {
            code: "MODEL_TIMEOUT",
            message: "模型请求超时。",
            retryable: true,
          },
        },
        422,
      );
    await createRun();
    fireEvent.click(screen.getByRole("button", { name: "单步" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("MODEL_TIMEOUT");
    expect(screen.getByText("已经留下的一段生活。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "单步" })).toBeEnabled();
  });

  it("uses a detached counterfactual endpoint and reports both kinds of effect", async () => {
    requestOverrides["POST /api/runs/run-1/counterfactual"] = () =>
      response({
        baseline_fingerprint: "before",
        changed_fingerprint: "after",
        changed_use_ids: ["use-a"],
        baseline_text: "原始生活",
        changed_text: "移除来源的生活",
        structural_effect: true,
        semantic_fixture_effect: true,
        note: "机制夹具，不调用模型。",
      });
    await createRun();
    fireEvent.click(screen.getByRole("button", { name: "验证与诊断" }));
    fireEvent.click(screen.getByRole("button", { name: "执行离线反事实" }));
    expect(await screen.findByText("✓ 条件发生变化")).toBeInTheDocument();
    expect(screen.getByText("原始生活")).toBeInTheDocument();
    expect(screen.getByText("移除来源的生活")).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(
      ([url]) => url === "/api/runs/run-1/counterfactual",
    );
    expect(JSON.parse(call![1]!.body as string)).toEqual({
      target_position_id: "a",
      source_position_id: "b",
    });
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/start"))).toBe(
      false,
    );
  });

  it("requires a separate confirmation and prevents active-run deletion", async () => {
    requestOverrides["DELETE /api/runs/run-1"] = () =>
      response({ deleted: true });
    await createRun();
    fireEvent.click(screen.getByRole("button", { name: "删除本地存档" }));
    expect(
      fetchMock.mock.calls.some(([, options]) => options?.method === "DELETE"),
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "开始运行" }));
    await screen.findByRole("button", { name: "暂停" });
    expect(screen.getByRole("button", { name: "删除本地存档" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "单步" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新加载配置" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "暂停" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "开始运行" })).toBeEnabled(),
    );
  });

  it("polls only while a selected run is active and stops on terminal state", async () => {
    await createRun();
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "开始运行" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "暂停" })).toBeInTheDocument();
    const before = fetchMock.mock.calls.filter(
      ([url]) => url === "/api/runs/run-1",
    ).length;
    snapshot.run.status = "paused";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/runs/run-1").length,
    ).toBe(before + 1);
    expect(
      screen.getByRole("button", { name: "开始运行" }),
    ).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(12000);
    });
    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/runs/run-1").length,
    ).toBe(before + 1);
  });

  it("backs off after a polling failure, preserves prose and recovers without exposing exceptions", async () => {
    snapshot.story = [
      {
        id: "segment-0",
        revision: 0,
        text: "保留下来的正文。",
        focus_position_id: "a",
        expression_ids: ["shared"],
        reinterprets: [],
      },
    ];
    let polls = 0;
    requestOverrides["GET /api/runs/run-1"] = () => {
      polls += 1;
      return polls === 1
        ? Promise.reject(new Error("raw-private-provider-body"))
        : response({ ...snapshot, run: { ...snapshot.run, status: "paused" } });
    };
    await createRun();
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "开始运行" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(screen.getByText(/自动刷新暂时中断/)).toBeInTheDocument();
    expect(screen.getByText("保留下来的正文。")).toBeInTheDocument();
    expect(screen.queryByText(/raw-private/)).not.toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(polls).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(polls).toBe(2);
    expect(screen.queryByText(/自动刷新暂时中断/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "开始运行" }),
    ).toBeInTheDocument();
  });
});
