import { useEffect, useRef, useState } from "react";
import { ApiError, request } from "./api";
import { graphApi } from "./graphApi";
import type { GraphCreateInput, GraphPreset, GraphPresetInput, GraphRunArchive, GraphSnapshot } from "./graphTypes";
import type { Preset, PresetCatalog, PublicConfig } from "./types";

export const graphErrorText = (cause: unknown) => cause instanceof ApiError
  ? `${cause.message}（${cause.code}）` : "操作暂时未能完成，请稍后重试。";
export const graphIsActive = (snapshot: GraphSnapshot | null) => !!snapshot && snapshot.active_scopes.length > 0;

export function useGrowthWorkbench() {
  const [runs, setRuns] = useState<GraphRunArchive[]>([]);
  const [graphPresets, setGraphPresets] = useState<GraphPreset[]>([]);
  const [notice, setNotice] = useState("");
  const [presets, setPresets] = useState<Preset[]>([]);
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [snapshot, setSnapshot] = useState<GraphSnapshot | null>(null);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [booting, setBooting] = useState(true);
  const [busyLabels, setBusyLabels] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const selection = useRef(0);
  const mounted = useRef(true);
  const busy = busyLabels[0] ?? "";
  const begin = (label: string) => setBusyLabels((current) => [...current, label]);
  const finish = (label: string) => setBusyLabels((current) => current.filter((item) => item !== label));

  useEffect(() => {
    mounted.current = true;
    let live = true;
    void Promise.allSettled([graphApi.list(), request<PresetCatalog>("/presets"), request<PublicConfig>("/config"), graphApi.presets()])
      .then(([library, catalog, configuration, graphCatalog]) => {
        if (!live) return;
        if (library.status === "fulfilled") setRuns(library.value.runs);
        if (catalog.status === "fulfilled") setPresets(catalog.value.presets);
        if (configuration.status === "fulfilled") setConfig(configuration.value);
        if (graphCatalog.status === "fulfilled") setGraphPresets(graphCatalog.value.presets);
        const failed = [library, catalog, configuration, graphCatalog].find((result) => result.status === "rejected");
        if (failed?.status === "rejected") setError(graphErrorText(failed.reason));
        setBooting(false);
      });
    return () => { live = false; mounted.current = false; selection.current += 1; };
  }, []);

  const accept = (next: GraphSnapshot) => {
    setSnapshot(next);
    setRuns((current) => [next.run, ...current.filter((run) => run.id !== next.run.id)]);
  };

  const savePreset = async (input: GraphPresetInput) => {
    const epoch = selection.current;
    begin("保存图种子预设");
    setError("");
    setNotice("");
    try {
      const saved = await graphApi.savePreset(input);
      if (!mounted.current || epoch !== selection.current) return null;
      setGraphPresets((current) => [saved, ...current.filter((preset) => preset.id !== saved.id)]);
      setNotice(`“${saved.name}”已保存为图种子预设，完整 Markdown 可再次载入。`);
      return saved;
    } catch (cause) {
      if (mounted.current && epoch === selection.current) setError(graphErrorText(cause));
      return null;
    } finally { if (mounted.current && epoch === selection.current) finish("保存图种子预设"); }
  };

  const active = graphIsActive(snapshot);
  useEffect(() => {
    if (!selectedRunId || !active) return;
    let disposed = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const epoch = selection.current;
    const poll = async () => {
      try {
        const next = await graphApi.snapshot(selectedRunId, controller.signal);
        if (disposed || epoch !== selection.current) return;
        failures = 0;
        setPollError("");
        accept(next);
        if (!graphIsActive(next)) return;
      } catch (cause) {
        if (disposed || epoch !== selection.current) return;
        failures += 1;
        setPollError(`自动刷新暂时中断，已保留图谱与阅读进度。${graphErrorText(cause)}`);
      }
      if (!disposed) timer = setTimeout(poll, Math.min(12000, 1500 * 2 ** failures));
    };
    timer = setTimeout(poll, 1500);
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  }, [selectedRunId, active]);

  const perform = async (label: string, action: () => Promise<GraphSnapshot>, replace = false) => {
    const epoch = replace ? ++selection.current : selection.current;
    begin(label);
    setError("");
    try {
      const next = await action();
      if (!mounted.current || epoch !== selection.current) return;
      accept(next);
      if (replace) setSelectedRunId(next.run.id);
    } catch (cause) {
      if (mounted.current && epoch === selection.current) {
        setError(graphErrorText(cause));
        if (!replace && selectedRunId) {
          try {
            const retained = await graphApi.snapshot(selectedRunId);
            if (mounted.current && epoch === selection.current) accept(retained);
          } catch { /* Keep the readable checkpoint and the original safe operation error. */ }
        }
      }
    } finally {
      if (mounted.current && epoch === selection.current) finish(label);
    }
  };

  const selectRun = async (runId: string) => {
    const epoch = ++selection.current;
    setSelectedRunId(runId);
    setSnapshot(null);
    setPollError("");
    setError("");
    setBusyLabels(runId ? ["加载图谱"] : []);
    if (!runId) return;
    try {
      const next = await graphApi.snapshot(runId);
      if (mounted.current && epoch === selection.current) accept(next);
    } catch (cause) {
      if (mounted.current && epoch === selection.current) setError(graphErrorText(cause));
    } finally {
      if (mounted.current && epoch === selection.current) finish("加载图谱");
    }
  };

  return {
    runs, presets, graphPresets, config, snapshot, selectedRunId, booting, busy, busyLabels, error, setError, pollError, notice, savePreset,
    isBusy: (scope: string) => busyLabels.some((label) => !label.includes(":") || label.startsWith(`${scope}:`)),
    selectRun,
    create: (input: GraphCreateInput) => perform("创建图谱", () => graphApi.create(input), true),
    growth: (action: "step" | "start" | "pause") => selectedRunId
      ? perform(`growth:${action}`, () => graphApi.growth(selectedRunId, action)) : Promise.resolve(),
    reading: (trackId: string, action: "step" | "start" | "pause") => selectedRunId
      ? perform(`reading:${trackId}:${action}`, () => graphApi.reading(selectedRunId, trackId, action)) : Promise.resolve(),
    addReading: (input: { title: string; entry_node_ids: string[]; direction?: string }) => selectedRunId
      ? perform("添加阅读", () => graphApi.addReading(selectedRunId, input)) : Promise.resolve(),
    refresh: () => selectedRunId ? perform("刷新图谱", () => graphApi.snapshot(selectedRunId)) : Promise.resolve(),
  };
}
export type GrowthWorkbenchModel = ReturnType<typeof useGrowthWorkbench>;
