import { CheckIcon } from './Icons'

interface Props {
  stage: string | null
  /** True once diarization has produced speakers, which tells the two
   *  "summarizing" passes apart. */
  hasSpeakers: boolean
  durationSeconds: number | null
  diarizationEnabled?: boolean
  /** An uploaded recording is converted by ffmpeg before anything else, so it
   *  gets one extra step at the front. A live recording never sees it. */
  isUpload?: boolean
}

interface Step {
  key: string
  label: string
  hint?: string
}

function stepIndex(stage: string | null, hasSpeakers: boolean, withSpeakers: boolean): number {
  switch (stage) {
    case 'converting':
      return -1
    case 'transcribing':
      return 0
    case 'summarizing':
      return withSpeakers && hasSpeakers ? 3 : 1
    case 'identifying_speakers':
    case 'diarizing':
      return 2
    default:
      return 0
  }
}

function estimate(durationSeconds: number | null): string {
  if (!durationSeconds || durationSeconds <= 0) return ''
  const minutes = Math.max(1, Math.round(durationSeconds / 60))
  return minutes === 1 ? 'about a minute' : `about ${minutes} minutes`
}

/** A four-step strip shown in the header while the backend pipeline runs.
 *  Done steps get a check, the current one pulses, the rest stay muted.
 *  An upload adds "Converting" in front, so the strip is five steps for a file
 *  and four for a live recording. */
export function PipelineProgress({
  stage,
  hasSpeakers,
  durationSeconds,
  diarizationEnabled = true,
  isUpload = false,
}: Props) {
  const main: Step[] = diarizationEnabled
    ? [
        { key: 'transcribe', label: 'Transcribing' },
        { key: 'notes', label: 'Writing notes' },
        {
          key: 'speakers',
          label: 'Identifying speakers',
          hint: `Runs at about real time on this laptop, ${estimate(durationSeconds) || 'a few minutes'} for this recording. Notes are already usable.`,
        },
        { key: 'final', label: 'Adding speakers to notes' },
      ]
    : [
        { key: 'transcribe', label: 'Transcribing' },
        { key: 'notes', label: 'Writing notes' },
      ]

  const lead: Step[] = isUpload
    ? [
        {
          key: 'convert',
          label: 'Converting',
          hint: 'Decoding the uploaded file into the 16 kHz audio the rest of the pipeline reads.',
        },
      ]
    : []
  const steps = [...lead, ...main]

  // stepIndex numbers the main steps; the converting step sits at -1 so the
  // offset lands it on 0 when it is present and is ignored when it is not.
  const raw = stepIndex(stage, hasSpeakers, diarizationEnabled) + lead.length
  const current = Math.min(Math.max(raw, 0), steps.length - 1)
  const hint = steps[current]?.hint

  return (
    <div className="pipeline" role="status" aria-live="polite">
      <ol className="pipeline-steps">
        {steps.map((step, index) => {
          const state = index < current ? 'done' : index === current ? 'active' : 'todo'
          return (
            <li key={step.key} className={`pipeline-step ${state}`}>
              <span className="pipeline-mark" aria-hidden="true">
                {state === 'done' && <CheckIcon size={12} />}
                {state === 'active' && <span className="pipeline-pulse" />}
                {state === 'todo' && <span className="pipeline-dot" />}
              </span>
              <span className="pipeline-label">{step.label}</span>
              {index < steps.length - 1 && <span className="pipeline-line" aria-hidden="true" />}
            </li>
          )
        })}
      </ol>
      {hint && <p className="pipeline-hint">{hint}</p>}
    </div>
  )
}
