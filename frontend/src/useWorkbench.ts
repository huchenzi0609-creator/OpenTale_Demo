import { useEffect, useRef, useState } from "react";
import { ApiError, post, request } from "./api";
import { DEFAULT_LIMITS, budgetBounds, limitsValid, sameLimits } from "./budgetLimits";
import { presetPayload, type PresetDraft } from "./presetDraft";
import { readingTracks } from "./readings";
import type {
  Counterfactual,
  ExperimentMode,
  FiniteLimits,
  Limits,
  Preset,
  PresetCatalog,
  Provider,
  PublicConfig,
  ReplayVerification,
  ReflectionAblation,
  RunSummary,
  Snapshot,
  Verification,
} from "./types";

const errorText = (error: unknown) =>
  error instanceof ApiError
    ? `${error.message}（${error.code}）`
    : "操作未能完成，请稍后重试。";

/** Owns server interaction, recoverable state and bounded polling; prose remains a separate view. */
export function useWorkbench() {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetErrors, setPresetErrors] = useState<NonNullable<PresetCatalog["errors"]>>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [presetId, setPresetId] = useState("");
  const [provider, setProvider] = useState<Provider>("fixture");
  const [mode, setMode] = useState<ExperimentMode>("classic");
  const [limits, setLimits] = useState<Limits>(DEFAULT_LIMITS);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedPosition, setSelectedPosition] = useState("");
  const [selectedReadingId, setSelectedReadingId] = useState("main");
  const [presentingReadingId, setPresentingReadingId] = useState("");
  const [tab, setTab] = useState<"story" | "structure" | "diagnostics" | "reflection">(
    "story",
  );
  const [booting, setBooting] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [createError, setCreateError] = useState("");
  const [budgetError, setBudgetError] = useState("");
  const [runControlFocusRequest, setRunControlFocusRequest] = useState(0);
  const [notice, setNotice] = useState("");
  const [pollError, setPollError] = useState("");
  const [verification, setVerification] = useState<Verification | null>(null);
  const [replayVerification, setReplayVerification] =
    useState<ReplayVerification | null>(null);
  const [counterfactual, setCounterfactual] = useState<Counterfactual | null>(
    null,
  );
  const [reflectionAblation, setReflectionAblation] = useState<ReflectionAblation | null>(null);
  const [counterSource, setCounterSource] = useState("");
  const [deleteArmed, setDeleteArmed] = useState(false);
  const selection = useRef(0);
  const active = snapshot?.run.status === "running";
  const anyActive = active || runs.some((run) => run.status === "running");
  const preset = presets.find((item) => item.id === presetId);

  useEffect(() => {
    if (snapshot && !readingTracks(snapshot).some((reading) => reading.id === selectedReadingId)) {
      setSelectedReadingId("main");
    }
  }, [snapshot, selectedReadingId]);

  useEffect(() => {
    setCreateError("");
  }, [presetId, provider, mode, limits, config?.configured]);

  useEffect(() => {
    setReflectionAblation(null);
  }, [snapshot?.run.id, snapshot?.run.revision]);

  useEffect(() => {
    if (snapshot?.run.mode !== "reflexive" && tab === "reflection") setTab("story");
  }, [snapshot?.run.mode, tab]);

  useEffect(() => {
    setBudgetError("");
  }, [limits, selectedRunId]);

  useEffect(() => {
    let live = true;
    void Promise.allSettled([
      request<PublicConfig>("/config"),
      request<PresetCatalog>("/presets"),
      request<{ runs: RunSummary[] }>("/runs"),
    ]).then((results) => {
      if (!live) return;
      const [configuration, catalog, library] = results;
      if (configuration.status === "fulfilled") setConfig(configuration.value);
      if (catalog.status === "fulfilled") {
        setPresets(catalog.value.presets);
        setPresetErrors(catalog.value.errors ?? []);
        setPresetId(catalog.value.presets[0]?.id ?? "");
      }
      if (library.status === "fulfilled") setRuns(library.value.runs);
      const failed = results.find((result) => result.status === "rejected");
      if (failed?.status === "rejected") setError(errorText(failed.reason));
      setBooting(false);
    });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (
      !snapshot ||
      !selectedPosition ||
      !snapshot.positions.some((position) => position.id === selectedPosition)
    ) {
      setSelectedPosition(snapshot?.positions[0]?.id ?? "");
    }
  }, [snapshot, selectedPosition]);

  useEffect(() => {
    if (
      !snapshot?.positions.some(
        (position) =>
          position.id === counterSource && position.id !== selectedPosition,
      )
    ) {
      setCounterSource(
        snapshot?.positions.find((position) => position.id !== selectedPosition)
          ?.id ?? "",
      );
    }
    setCounterfactual(null);
  }, [
    selectedPosition,
    counterSource,
    snapshot?.run.id,
    snapshot?.run.revision,
  ]);

  useEffect(() => {
    setReplayVerification(null);
  }, [snapshot?.run.id, snapshot?.run.revision]);

  useEffect(() => {
    if (!selectedRunId || !active) return;
    let disposed = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const next = await request<Snapshot>(
          `/runs/${encodeURIComponent(selectedRunId)}`,
          { signal: controller.signal },
        );
        if (disposed) return;
        failures = 0;
        setPollError("");
        setSnapshot(next);
        setRuns((current) =>
          current.map((run) => (run.id === next.run.id ? next.run : run)),
        );
        if (next.run.status !== "running") return;
      } catch (cause) {
        if (disposed) return;
        failures += 1;
        setPollError(`自动刷新暂时中断，将重试。${errorText(cause)}`);
      }
      if (!disposed)
        timer = setTimeout(poll, Math.min(8000, 2000 * 2 ** failures));
    };
    timer = setTimeout(poll, 2000);
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [selectedRunId, active]);

  async function refreshLibrary() {
    const result = await request<{ runs: RunSummary[] }>("/runs");
    setRuns(result.runs);
  }
  function adopt(next: Snapshot) {
    selection.current += 1;
    setSnapshot(next);
    setLimits({ ...next.run.limits });
    setSelectedRunId(next.run.id);
    setDeleteArmed(false);
    setCounterfactual(null);
    setPollError("");
    setRuns((current) => [
      next.run,
      ...current.filter((item) => item.id !== next.run.id),
    ]);
  }
  async function perform(
    label: string,
    action: () => Promise<void>,
    onError: (message: string) => void = setError,
  ) {
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (cause) {
      onError(errorText(cause));
    } finally {
      setBusy("");
    }
  }
  async function selectRun(id: string) {
    const ticket = ++selection.current;
    setSelectedRunId(id);
    setSnapshot(null);
    setDeleteArmed(false);
    setError("");
    setPollError("");
    setCounterfactual(null);
    if (!id) return;
    await perform("加载存档", async () => {
      const next = await request<Snapshot>(`/runs/${encodeURIComponent(id)}`);
      if (ticket === selection.current) {
        setSnapshot(next);
        setLimits({ ...next.run.limits });
      }
    });
  }
  async function runAction(action: "step" | "start" | "pause") {
    if (!snapshot) return;
    const id = snapshot.run.id;
    await perform(
      action === "step"
        ? "执行局部窗口"
        : action === "start"
          ? "开始运行"
          : "请求暂停",
      async () => {
        try {
          const next = await post<Snapshot>(
            `/runs/${encodeURIComponent(id)}/${action}`,
          );
          adopt(next);
        } catch (cause) {
          try {
            const next = await request<Snapshot>(
              `/runs/${encodeURIComponent(id)}`,
            );
            adopt(next);
          } catch {
            /* Preserve the last usable snapshot. */
          }
          throw cause;
        }
        await refreshLibrary();
        if (action === "pause")
          setNotice("暂停请求已发送；正在执行的操作会在安全边界结束。");
      },
    );
  }
  function createReading(title: string, startPositionId: string, enabled: boolean) {
    if (!snapshot || anyActive || busy) return;
    const id = snapshot.run.id;
    const oldIds = new Set(readingTracks(snapshot).map((reading) => reading.id));
    return perform("新建阅读", async () => {
      const next = await post<Snapshot>(`/runs/${encodeURIComponent(id)}/readings`, {
        title: title.trim(), start_position_id: startPositionId, enabled,
      });
      adopt(next);
      const created = readingTracks(next).find((reading) => !oldIds.has(reading.id));
      if (created) setSelectedReadingId(created.id);
      setNotice("已新建独立阅读，共享同一世界；尚未调用模型。可从当前世界生成一段，或随以后窗口自动续写。");
    });
  }
  function setReadingEnabled(readingId: string, enabled: boolean) {
    if (!snapshot || anyActive || busy) return;
    return perform("更新阅读设置", async () => {
      adopt(await request<Snapshot>(`/runs/${encodeURIComponent(snapshot.run.id)}/readings/${encodeURIComponent(readingId)}`, {
        method: "PATCH", body: JSON.stringify({ enabled }),
      }));
      setNotice(enabled ? "此阅读将随以后窗口续写；设置未调用模型。" : "此阅读已停止自动续写，已有正文保留；设置未调用模型。");
    });
  }
  function presentReading(readingId: string) {
    if (!snapshot || anyActive || busy) return;
    const id = snapshot.run.id;
    setPresentingReadingId(readingId);
    return perform("生成阅读", async () => {
      try {
        adopt(await post<Snapshot>(`/runs/${encodeURIComponent(id)}/readings/${encodeURIComponent(readingId)}/present`));
      } catch (cause) {
        try {
          adopt(await request<Snapshot>(`/runs/${encodeURIComponent(id)}`));
        } catch { /* Keep the last usable state when the server is unavailable. */ }
        throw cause;
      }
      setNotice("已从当前世界完成此阅读的呈现，人物构想与续延未重新执行。");
    }).finally(() => setPresentingReadingId(""));
  }
  const disabled = !!busy;
  const frozen = disabled || active;
  const exhausted = snapshot?.run.status === "budget_exhausted";
  const maximumLimits = budgetBounds(config);
  const validLimits = limitsValid(limits, maximumLimits);
  const tokenUsage = snapshot
    ? snapshot.run.usage.input_tokens +
      snapshot.run.usage.output_tokens +
      (snapshot.run.usage.reserved_tokens ?? 0) +
      (snapshot.run.usage.unknown_tokens ?? 0)
    : 0;
  const minimumRunLimits: FiniteLimits = {
    max_calls: Math.max(1, snapshot?.run.usage.calls ?? 0),
    max_tokens: Math.max(1, tokenUsage),
    max_parallel: 1,
    max_windows: Math.max(1, snapshot?.run.usage.windows ?? 0),
  };
  const validRunLimits = limitsValid(limits, maximumLimits, minimumRunLimits);
  const limitsChanged = !!snapshot && !sameLimits(limits, snapshot.run.limits);

  const updateRunLimits = () => {
    if (!snapshot || anyActive || disabled || !validRunLimits || !limitsChanged) return;
    const id = snapshot.run.id;
    const nextLimits = { ...limits };
    setBudgetError("");
    return perform("更新实验预算", async () => {
      const next = await request<Snapshot>(`/runs/${encodeURIComponent(id)}/limits`, {
        method: "PATCH",
        body: JSON.stringify(nextLimits),
      });
      adopt(next);
      setNotice("本实验预算已更新，已有结果、用量与恢复点保留；未调用模型。");
    }, setBudgetError);
  };
  const resetRunLimits = () => {
    if (snapshot && !anyActive && !disabled) {
      setLimits({ ...snapshot.run.limits });
      setBudgetError("");
    }
  };

  const createRun = () => {
    setCreateError("");
    return perform("创建实验", async () => {
      adopt(
        await post<Snapshot>("/runs", {
          preset_id: presetId,
          provider,
          mode,
          limits,
        }),
      );
      setTab("story");
      setRunControlFocusRequest((current) => current + 1);
    }, setCreateError);
  };
  async function savePreset(draft: PresetDraft, original?: Preset) {
    const payload = presetPayload(draft);
    const result = original
      ? await request<{ preset: Preset }>(`/presets/${encodeURIComponent(original.id)}`, {
          method: "PUT",
          body: JSON.stringify({ ...payload, expected_version: original.version }),
        })
      : await post<{ preset: Preset }>("/presets", payload);
    setPresets((current) => [
      ...current.filter((item) => item.id !== result.preset.id), result.preset,
    ]);
    setPresetId(result.preset.id);
    setError("");
    setNotice(`已保存预设「${result.preset.name}」。点击“创建实验”使用；已有存档保持原样。`);
  }
  const reloadConfig = () =>
    perform("重新加载配置", async () => {
      setConfig(await post<PublicConfig>("/config/reload"));
      setNotice("配置状态已刷新。");
    });
  const forkRun = () =>
    perform("创建分支", async () => {
      if (!snapshot) return;
      adopt(
        await post<Snapshot>(`/runs/${snapshot.run.id}/fork`, {
          title: `${snapshot.run.title} · 分支`,
        }),
      );
      setNotice("已创建独立分支，原实验保留。");
    });
  const deleteRun = () =>
    perform("删除存档", async () => {
      if (!snapshot) return;
      await request(`/runs/${snapshot.run.id}`, { method: "DELETE" });
      setSnapshot(null);
      setSelectedRunId("");
      setDeleteArmed(false);
      await refreshLibrary();
      setNotice("本地存档已删除。");
    });
  const verify = () =>
    perform("离线验证", async () => {
      setVerification(await request<Verification>("/verify"));
    });
  const compare = () =>
    perform("反事实检验", async () => {
      if (!snapshot) return;
      setCounterfactual(
        await post<Counterfactual>(`/runs/${snapshot.run.id}/counterfactual`, {
          target_position_id: selectedPosition,
          source_position_id: counterSource,
        }),
      );
    });
  const replay = () =>
    perform("记录重执行", async () => {
      if (!snapshot || active) return;
      setReplayVerification(
        await request<ReplayVerification>(`/runs/${snapshot.run.id}/replay`),
      );
    });
  const ablateReflection = (scopeId: string, methodId: string) => {
    if (!snapshot || frozen || snapshot.pending || snapshot.run.mode !== "reflexive"
      || !snapshot.reflection?.adoptions[scopeId]?.includes(methodId)) return;
    setReflectionAblation(null);
    return perform("方法离线消融", async () => {
      setReflectionAblation(await post<ReflectionAblation>(
        `/runs/${encodeURIComponent(snapshot.run.id)}/reflection/ablate`,
        { scope_id: scopeId, method_id: methodId },
      ));
    });
  };
  const releaseReflection = (scopeId: string, methodId: string) => {
    if (!snapshot || frozen || snapshot.run.mode !== "reflexive"
      || !snapshot.reflection?.adoptions[scopeId]?.includes(methodId)) return;
    const id = snapshot.run.id;
    setReflectionAblation(null);
    return perform("停止采用方法", async () => {
      adopt(await post<Snapshot>(`/runs/${encodeURIComponent(id)}/reflection/release`,
        { scope_id: scopeId, method_id: methodId }));
      setNotice("已停止在所选作用域采用此方法；只改变后续采用关系，已冻结的调用输入保留。模型可以再次采用此方法；未调用模型。");
    });
  };
  const reloadLibrary = () => perform("刷新列表", refreshLibrary);
  const reconnect = () =>
    perform("重新连接", async () => {
      const [configuration, catalog, library] = await Promise.all([
        request<PublicConfig>("/config"),
        request<PresetCatalog>("/presets"),
        request<{ runs: RunSummary[] }>("/runs"),
      ]);
      setConfig(configuration);
      setPresets(catalog.presets);
      setPresetErrors(catalog.errors ?? []);
      setRuns(library.runs);
      if (!catalog.presets.some((item) => item.id === presetId))
        setPresetId(catalog.presets[0]?.id ?? "");
      setNotice("本地服务已重新连接。");
    });
  return {
    config,
    presets,
    presetErrors,
    runs,
    presetId,
    setPresetId,
    provider,
    setProvider,
    mode,
    setMode,
    limits,
    setLimits,
    snapshot,
    selectedRunId,
    selectedPosition,
    setSelectedPosition,
    selectedReadingId,
    setSelectedReadingId,
    presentingReadingId,
    tab,
    setTab,
    booting,
    busy,
    error,
    setError,
    createError,
    budgetError,
    runControlFocusRequest,
    notice,
    pollError,
    verification,
    setVerification,
    counterfactual,
    reflectionAblation,
    setReflectionAblation,
    replayVerification,
    counterSource,
    setCounterSource,
    deleteArmed,
    setDeleteArmed,
    active,
    anyActive,
    preset,
    disabled,
    frozen,
    exhausted,
    validLimits,
    maximumLimits,
    minimumRunLimits,
    validRunLimits,
    limitsChanged,
    tokenUsage,
    createRun,
    updateRunLimits,
    resetRunLimits,
    savePreset,
    reloadConfig,
    forkRun,
    deleteRun,
    verify,
    compare,
    replay,
    ablateReflection,
    releaseReflection,
    reloadLibrary,
    reconnect,
    selectRun,
    runAction,
    createReading,
    setReadingEnabled,
    presentReading,
  };
}

export type Workbench = ReturnType<typeof useWorkbench>;
