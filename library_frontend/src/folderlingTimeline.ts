import type { JobEvent } from "./types";

export interface FolderlingTimelineGroup {
  event: JobEvent;
  count: number;
  firstRecordedAt: string;
}

/**
 * Collapse consecutive events that share the same phase.
 *
 * Any different visible phase starts a new group.  The last event owns the
 * displayed timestamp/status, while an earlier error/fallback remains visible
 * if a later event omits it.
 */
export function groupFolderlingTimelineEvents(events: JobEvent[]): FolderlingTimelineGroup[] {
  const groups: FolderlingTimelineGroup[] = [];
  for (const event of events) {
    const previous = groups.at(-1);
    if (previous?.event.phase === event.phase) {
      previous.count += 1;
      previous.event = {
        ...event,
        fallback_reason: event.fallback_reason ?? previous.event.fallback_reason,
        error: event.error ?? previous.event.error
      };
      continue;
    }
    groups.push({ event, count: 1, firstRecordedAt: event.recorded_at });
  }
  return groups;
}
