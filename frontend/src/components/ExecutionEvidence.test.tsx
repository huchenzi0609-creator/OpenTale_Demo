import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Snapshot } from "../types";
import ExecutionEvidence from "./ExecutionEvidence";

describe("context evidence", () => {
  it.each([false, true])("distinguishes lossless selected text from excerpts: %s", (lossy) => {
    const snapshot = { requests: [{
      id: "request", stage: "propose", scope_id: "position:niki", input_world_revision: 4,
      policy_revision: 1, frame_hash: "original-frame", request_hash: "sent-request",
      projection_version: "provider-json-v2-context", evidence_level: "exact_projection",
      frame: { text: "PRIVATE-FROZEN-SOURCE" },
      attempts: [{ number: 1, status: "received", request_chars: 95000,
        projection: { text: "PRIVATE-PROJECTION" } }],
      context_compaction: {
        algorithm: "source-excerpts-v1", projection_version: "provider-json-v2-context",
        target_chars: 96000, input_chars: 850930, output_chars: 95000, lossy,
        text_entries: 200, material_occurrences: 434, candidate_objects: 360,
        original_text_chars: 203829, retained_text_chars: lossy ? 75000 : 203829,
        omitted_text_chars: lossy ? 128829 : 0, history_segments: 4, history_segments_excerpted: lossy ? 2 : 0,
        within_target: true,
      },
    }] } as unknown as Snapshot;
    const { container } = render(<ExecutionEvidence snapshot={snapshot} />);
    expect(screen.getByRole("region", { name: "上下文整理记录", hidden: true })).toHaveTextContent(
      lossy ? "部分原文未送入模型" : "选中材料正文完整保留",
    );
    expect(container).toHaveTextContent("850,930 → 95,000");
    expect(container).toHaveTextContent("434 / 200");
    expect(container).not.toHaveTextContent("PRIVATE-FROZEN-SOURCE");
    expect(container).not.toHaveTextContent("PRIVATE-PROJECTION");
  });
});
