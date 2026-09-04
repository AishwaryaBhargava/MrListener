import { stageLabel, statusLabel } from '../lib/format'

/** Tone controls the colour: neutral | recording | processing | ready | error. */
export type PillTone = 'neutral' | 'recording' | 'processing' | 'ready' | 'error'

export function StatusPill({ tone, label }: { tone: PillTone; label: string }) {
  return (
    <span className={tone === 'neutral' ? 'status-pill' : `status-pill ${tone}`}>
      {tone === 'recording' && <span className="pill-dot" />}
      {label}
    </span>
  )
}

interface Props {
  status: string
  stage?: string | null
  /** Lets a finished meeting say "Notes ready" rather than just "Ready". */
  hasNotes?: boolean
  hasError?: boolean
}

/** Maps a persisted meeting status onto the pill.
 *
 * While processing, the pipeline stage is what the user cares about
 * ("Identifying speakers" is a very different wait from "Transcribing"), so it
 * replaces the generic label whenever one is set.
 */
export function MeetingStatusPill({ status, stage, hasNotes, hasError }: Props) {
  if (status !== 'processing' && (hasError || status === 'failed')) {
    return <StatusPill tone="error" label="Error" />
  }

  if (status === 'recording') return <StatusPill tone="recording" label="Recording" />

  if (status === 'processing') {
    return <StatusPill tone="processing" label={stage ? stageLabel(stage) : 'Processing'} />
  }

  if (status === 'ready') {
    return <StatusPill tone="ready" label={hasNotes ? 'Notes ready' : 'Ready'} />
  }

  return <StatusPill tone="neutral" label={statusLabel(status)} />
}
