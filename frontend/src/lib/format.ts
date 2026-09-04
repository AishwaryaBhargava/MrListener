/** 00:00:00 - always three segments, for the recording timer. */
export function formatTimer(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds))
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const seconds = safe % 60
  return [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':')
}

/** M:SS (or H:MM:SS past an hour) - for the playback scrubber. */
export function formatClockShort(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return '0:00'
  const safe = Math.floor(totalSeconds)
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const seconds = safe % 60
  const paddedSeconds = String(seconds).padStart(2, '0')
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${paddedSeconds}`
  return `${minutes}:${paddedSeconds}`
}

/** mm:ss (h:mm:ss past an hour) - transcript segment stamps. */
export function formatStamp(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return '00:00'
  const safe = Math.floor(totalSeconds)
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const seconds = String(safe % 60).padStart(2, '0')
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${seconds}`
  return `${String(minutes).padStart(2, '0')}:${seconds}`
}

/** "12 min" / "48 sec" - the meeting list meta line. */
export function formatDuration(seconds: number | null): string {
  if (seconds == null) return '--'
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} sec`
  return `${Math.round(seconds / 60)} min`
}

function toDate(isoUtc: string): Date {
  // The backend sends ...Z; be tolerant of a stored value that lost its suffix.
  const normalized = /[Zz]|[+-]\d{2}:\d{2}$/.test(isoUtc) ? isoUtc : `${isoUtc}Z`
  return new Date(normalized)
}

/** Local wall-clock time, e.g. "05:30 PM" or "17:30" depending on locale. */
export function formatTimeOfDay(isoUtc: string): string {
  return toDate(isoUtc).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** e.g. "Wednesday, September 3, 2026" */
export function formatLongDate(isoUtc: string): string {
  return toDate(isoUtc).toLocaleDateString([], {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })
}

/** Month abbreviation + day for the 56px date tile. */
export function dateTileParts(isoUtc: string): { month: string; day: string } {
  const date = toDate(isoUtc)
  return {
    month: date.toLocaleDateString([], { month: 'short' }),
    day: String(date.getDate()),
  }
}

/** What the backend is doing right now, for the processing state. */
export function stageLabel(stage: string | null): string {
  switch (stage) {
    case 'transcribing':
      return 'Transcribing'
    case 'identifying_speakers':
    case 'diarizing':
      return 'Identifying speakers'
    case 'summarizing':
      return 'Writing notes'
    default:
      return 'Processing'
  }
}

/** "14:32 - 12 min - 2 speakers - 3 action items" for a library card. */
export function meetingMeta(meeting: {
  created_at: string
  duration_seconds: number | null
  speaker_count: number
  action_item_count: number
}): string {
  const parts = [formatTimeOfDay(meeting.created_at), formatDuration(meeting.duration_seconds)]
  if (meeting.speaker_count > 0) {
    parts.push(`${meeting.speaker_count} speaker${meeting.speaker_count === 1 ? '' : 's'}`)
  }
  if (meeting.action_item_count > 0) {
    parts.push(
      `${meeting.action_item_count} action item${meeting.action_item_count === 1 ? '' : 's'}`,
    )
  }
  return parts.join(' · ')
}

export function statusLabel(status: string): string {
  switch (status) {
    case 'recording':
      return 'Recording'
    case 'processing':
      return 'Processing'
    case 'ready':
      return 'Ready'
    case 'failed':
      return 'Failed'
    default:
      return status
  }
}
