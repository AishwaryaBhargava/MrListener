export type MeetingStatus = 'recording' | 'processing' | 'ready' | 'failed'

/** Which pipeline step is running. Only meaningful while status is 'processing'.
 *  'converting' is the ffmpeg pass an uploaded file goes through first; a live
 *  recording is already converted by /stop and never shows it. */
export type PipelineStage =
  | 'converting'
  | 'transcribing'
  | 'summarizing'
  | 'identifying_speakers'
  | 'diarizing'

/** How the audio got here: captured from the microphone, or uploaded. */
export type MeetingSource = 'live' | 'upload'

export interface Speaker {
  /** Stable key: 'S1', 'S2', ... - what a rename is addressed to. */
  id: string
  /** Current display name, 'Speaker 1' until renamed. */
  name: string
  /** Seconds of speech attributed to this speaker. */
  talk_time: number
  turn_count: number
  /** Slot in SPEAKER_COLORS, already wrapped by the backend. */
  color_index: number
}

export interface ActionItem {
  task: string
  /** Resolved through the current speaker names on every read. */
  owner: string | null
  owner_speaker_id: string | null
  due: string | null
  /** Seconds into the recording where it was said, for the seek. */
  source_time: number | null
  done: boolean
}

/** One chapter of the meeting. Ranges are chronological and never overlap. */
export interface Topic {
  title: string
  /** Seconds into the recording where the chapter starts. */
  start: number
  end: number
  /** One or two sentences on what was said in it. */
  summary: string
}

/** A question somebody asked out loud during the meeting. */
export interface OpenQuestion {
  question: string
  /** Resolved through the current speaker names on every read. */
  asked_by: string | null
  asked_by_speaker_id: string | null
  /** Seconds into the recording where it was asked. */
  time: number | null
  /** True when somebody answered it later in the transcript. */
  answered: boolean
}

/** What one speaker contributed. Empty until diarization has run. */
export interface SpeakerNotes {
  speaker_id: string
  /** Resolved through the current speaker names on every read. */
  name: string | null
  main_points: string[]
  commitments: string[]
  questions_raised: string[]
}

export interface Notes {
  title_suggestion: string | null
  summary: string
  /** Chapters covering the meeting, in time order. */
  topics: Topic[]
  key_takeaways: string[]
  decisions: string[]
  action_items: ActionItem[]
  /** Questions the participants raised in the room. */
  open_questions: OpenQuestion[]
  /** Questions the user should still ask or chase after the meeting. */
  follow_up_questions: string[]
  /** One block per speaker; only filled once speakers are known. */
  by_speaker: SpeakerNotes[]
  generated_at: string
  /** True when the notes were written with speaker labels available. */
  with_speakers: boolean
  model: string | null
  /** True for the fixed notes produced for a recording with no speech. */
  empty?: boolean
}

export interface Meeting {
  id: string
  title: string
  /** UTC ISO-8601 timestamp, e.g. 2026-09-03T12:04:11Z */
  created_at: string
  duration_seconds: number | null
  status: MeetingStatus
  audio_path: string | null
  pipeline_stage: PipelineStage | null
  pipeline_error: string | null
  /** The 'Recording NN' name, when the notes step replaced it. */
  auto_title: string | null
  /** 'live' for a microphone recording, 'upload' for a file. */
  source: MeetingSource
  /** The name of the uploaded file, when there was one. */
  source_filename: string | null
  has_audio: boolean
  has_transcript: boolean
  has_notes: boolean
  /** First ~120 characters of the transcript, for the meetings list. */
  transcript_preview: string | null
  /** First sentence of the summary - the preferred list preview. */
  notes_preview: string | null
  /** Text around the search hit, present only for a ?q= response. */
  match_snippet: string | null
  speaker_count: number
  action_item_count: number
  /** Only populated on the detail response. */
  speakers: Speaker[]
  notes: Notes | null
}

export interface TranscriptSegment {
  id: number
  start: number
  end: number
  text: string
  /** Present once diarization has run. */
  speaker?: string | null
  speaker_id?: string | null
}

export interface Transcript {
  segments: TranscriptSegment[]
  language: string | null
  /** 'live' while the 20 s windows are still the only source, 'final' after. */
  source: 'live' | 'final'
}

/** Text frames the recording socket pushes back while a meeting is live. */
export type RecordSocketMessage =
  | { type: 'transcript'; segments: TranscriptSegment[] }
  | { type: 'status'; stage: string }
  | { type: 'error'; message: string }
  | { type: 'stopped'; bytes: number }
  | { type: 'pong' }

export interface Health {
  ok: boolean
  ffmpeg: boolean
  groq_key_set: boolean
  hf_token_set: boolean
  version: string
  data_dir: string
}

export interface Settings {
  diarization_enabled: boolean
  max_speakers: number | null
  /** '' means auto-detect. */
  language_hint: string
  live_window_seconds: number
}

export interface KeyStatus {
  set: boolean
  /** Last four characters only; the key itself never leaves the backend. */
  last4: string | null
}

export interface Keys {
  groq_api_key: KeyStatus
  hf_token: KeyStatus
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : detail
    } catch {
      // Non-JSON error body: keep the status line.
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  health: () => request<Health>('/api/health'),

  /** `q` runs the server-side search across title, transcript and notes. */
  listMeetings: (q?: string) =>
    request<Meeting[]>(q && q.trim() ? `/api/meetings?q=${encodeURIComponent(q.trim())}` : '/api/meetings'),

  getMeeting: (id: string) => request<Meeting>(`/api/meetings/${id}`),

  createMeeting: (title?: string) =>
    request<Meeting>('/api/meetings', {
      method: 'POST',
      body: JSON.stringify({ title: title ?? null }),
    }),

  renameMeeting: (id: string, title: string) =>
    request<Meeting>(`/api/meetings/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),

  deleteMeeting: (id: string) => request<void>(`/api/meetings/${id}`, { method: 'DELETE' }),

  stopMeeting: (id: string) => request<Meeting>(`/api/meetings/${id}/stop`, { method: 'POST' }),

  reprocessMeeting: (id: string) =>
    request<Meeting>(`/api/meetings/${id}/reprocess`, { method: 'POST' }),

  regenerateNotes: (id: string) =>
    request<Meeting>(`/api/meetings/${id}/notes/regenerate`, { method: 'POST' }),

  /** `{ S1: 'Priya' }`. Only the name map changes; segments are untouched. */
  renameSpeakers: (id: string, names: Record<string, string>) =>
    request<Meeting>(`/api/meetings/${id}/speakers`, {
      method: 'PATCH',
      body: JSON.stringify(names),
    }),

  setActionItem: (id: string, index: number, done: boolean) =>
    request<Meeting>(`/api/meetings/${id}/action_items/${index}`, {
      method: 'PATCH',
      body: JSON.stringify({ done }),
    }),

  getTranscript: (id: string) => request<Transcript>(`/api/meetings/${id}/transcript`),

  getSettings: () => request<Settings>('/api/settings'),

  putSettings: (values: Partial<Settings>) =>
    request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(values) }),

  getKeys: () => request<Keys>('/api/settings/keys'),

  putKeys: (values: { groq_api_key?: string; hf_token?: string }) =>
    request<Keys>('/api/settings/keys', { method: 'PUT', body: JSON.stringify(values) }),

  /** Upload a recording instead of capturing one live.
   *
   * XMLHttpRequest rather than fetch: it is the only browser API that reports
   * how much of a request body has gone out, and a two hour video needs a real
   * progress bar. Resolves with the meeting as it was created - status
   * 'processing', stage 'converting' - so the caller can navigate straight to
   * the detail page and let it poll.
   */
  uploadMeeting: (file: File, title?: string, onProgress?: (fraction: number) => void) =>
    new Promise<Meeting>((resolve, reject) => {
      const form = new FormData()
      form.append('file', file)
      if (title && title.trim()) form.append('title', title.trim())

      const request = new XMLHttpRequest()
      request.open('POST', '/api/meetings/upload')
      request.responseType = 'text'

      request.upload.onprogress = (event) => {
        if (!onProgress) return
        onProgress(event.lengthComputable ? event.loaded / event.total : 0)
      }

      request.onload = () => {
        let body: unknown = null
        try {
          body = JSON.parse(request.responseText)
        } catch {
          // Keep the status line below.
        }
        if (request.status >= 200 && request.status < 300) {
          resolve(body as Meeting)
          return
        }
        const detail = (body as { detail?: unknown } | null)?.detail
        reject(
          new ApiError(
            request.status,
            typeof detail === 'string' ? detail : `${request.status} ${request.statusText}`,
          ),
        )
      }

      request.onerror = () =>
        reject(new ApiError(0, 'The upload could not reach the backend. Is it still running?'))
      request.onabort = () => reject(new ApiError(0, 'The upload was cancelled'))
      request.ontimeout = () => reject(new ApiError(0, 'The upload timed out'))

      request.send(form)
    }),

  audioUrl: (id: string) => `/api/meetings/${id}/audio`,

  exportUrl: (id: string) => `/api/meetings/${id}/export.md`,

  /** The rendered Markdown, for the copy-to-clipboard button. */
  exportMarkdown: async (id: string) => {
    const response = await fetch(`/api/meetings/${id}/export.md`)
    if (!response.ok) throw new ApiError(response.status, 'Could not build the Markdown export')
    return response.text()
  },

  /** Same-origin socket so the Vite dev proxy forwards it to the backend. */
  recordSocketUrl: (id: string) => {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    return `${scheme}://${window.location.host}/ws/record/${id}`
  },
}
