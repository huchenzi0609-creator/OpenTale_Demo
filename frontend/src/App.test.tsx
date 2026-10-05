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
import type { Snapshot } from "./types";

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
      target: { value: "201" },
    });
    expect(screen.getByRole("button", { name: "＋ 创建实验" })).toBeDisabled();
    expect(screen.getByText(/预算须为范围内的正整数/)).toBeInTheDocument();
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
        max_tokens: 40000,
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
