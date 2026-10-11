import { post, request } from "./api";
import type { GraphCreateInput, GraphNode, GraphPage, GraphPassage, GraphPreset, GraphPresetInput, GraphProjection, GraphRecordPage, GraphRunArchive, GraphSnapshot, GraphVisit } from "./graphTypes";

const root = (runId: string) => `/graph-runs/${encodeURIComponent(runId)}`;
export const graphApi = {
  list: async () => {
    const page = await request<{ items: { id: string; title: string; graph_commit: number; status: string; preset_id?: string }[] }>("/graph-runs");
    return { runs: page.items.map((item): GraphRunArchive => ({ ...item, revision: item.graph_commit })) };
  },
  presets: () => request<{ presets: GraphPreset[] }>("/graph-presets"),
  savePreset: (input: GraphPresetInput) => post<GraphPreset>("/graph-presets", input),
  create: (input: GraphCreateInput) => post<GraphSnapshot>("/graph-runs", input),
  snapshot: (runId: string, signal?: AbortSignal) => request<GraphSnapshot>(root(runId), { signal }),
  graph: (runId: string, offset = 0) => request<GraphProjection>(`${root(runId)}/graph?offset=${offset}`),
  node: (runId: string, nodeId: string, version?: string) => request<{ node: GraphNode; text: string }>(`${root(runId)}/nodes/${encodeURIComponent(nodeId)}${version ? `?version=${encodeURIComponent(version)}` : ""}`),
  growth: (runId: string, action: "step" | "start" | "pause") => post<GraphSnapshot>(`${root(runId)}/growth/${action}`),
  addReading: (runId: string, input: { title: string; entry_node_ids: string[]; direction?: string }) => post<GraphSnapshot>(`${root(runId)}/readings`, input),
  reading: (runId: string, trackId: string, action: "step" | "start" | "pause") => post<GraphSnapshot>(`${root(runId)}/readings/${encodeURIComponent(trackId)}/${action}`),
  passages: (runId: string, trackId: string, offset = 0) => request<GraphPage<GraphPassage>>(`${root(runId)}/readings/${encodeURIComponent(trackId)}/passages?offset=${offset}`),
  visits: (runId: string, trackId: string, offset = 0) => request<GraphPage<GraphVisit>>(`${root(runId)}/readings/${encodeURIComponent(trackId)}/visits?offset=${offset}`),
  records: (runId: string, kind: "methods" | "trials" | "responses" | "events" | "invocations", offset = 0) => request<GraphRecordPage>(`${root(runId)}/${kind}?offset=${offset}`),
};
