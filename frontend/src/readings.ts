import type { ReadingTrack, Snapshot } from "./types";

/** Compatibility projection for snapshots from servers predating multiple readings. */
export function readingTracks(snapshot: Snapshot): ReadingTrack[] {
  return snapshot.reading_tracks?.length ? snapshot.reading_tracks : [{
    id: "main",
    title: "主阅读",
    start_position_id: null,
    created_revision: 0,
    enabled: true,
    story: snapshot.story,
    reading: snapshot.reading,
    last_error: null,
  }];
}

export function worldReadyForPresentation(snapshot: Snapshot): boolean {
  return snapshot.run.revision >= 1 &&
    snapshot.continuations.length === snapshot.positions.length &&
    snapshot.positions.every((position) => snapshot.continuations.some((continuation) =>
      continuation.position_id === position.id &&
      continuation.frame_revision === snapshot.run.revision && continuation.status === "ready" &&
      !!continuation.surface.trim())) &&
    (!snapshot.pending?.committed || snapshot.positions.every((position) =>
      Object.hasOwn(snapshot.pending!.realizations, position.id)));
}
