import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { AudioPlayerBar, type PlayerHandle } from '../components/AudioPlayerBar'
import {
  ChevronLeftIcon,
  CopyIcon,
  DownloadIcon,
  RefreshIcon,
  TrashIcon,
} from '../components/Icons'
import { NotesPanel } from '../components/NotesPanel'
import { SpeakerChips } from '../components/SpeakerChips'
import { MeetingStatusPill } from '../components/StatusPill'
import { PipelineProgress } from '../components/PipelineProgress'
import { TranscriptList, WorkingIndicator } from '../components/Transcript'
import { useToast } from '../components/Toast'
import { api, type Meeting, type Settings, type Transcript } from '../lib/api'
import { formatDuration, formatLongDate, formatTimeOfDay, stageLabel } from '../lib/format'

/** How often to re-read the meeting while the backend pipeline is running. */
const POLL_MS = 2000

export default function MeetingDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const toast = useToast()
  const [meeting, setMeeting] = useState<Meeting | null>(null)
  const [transcript, setTranscript] = useState<Transcript | null>(null)
  const [title, setTitle] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [currentTime, setCurrentTime] = useState(0)
  const [settings, setSettings] = useState<Settings | null>(null)
  const savedTitleRef = useRef('')
  // Mirrors `title` so `load` can compare against it without taking it as a
  // dependency - otherwise the poll interval would restart on every keystroke.
  const titleRef = useRef('')
  const playerRef = useRef<PlayerHandle | null>(null)

  const load = useCallback(
    async (adoptTitle: boolean) => {
      if (!id) return null
      const [row, text] = await Promise.all([api.getMeeting(id), api.getTranscript(id)])
      setMeeting(row)
      setTranscript(text)
      // Poll refreshes never touch the input - the user may be mid-edit.
      if (adoptTitle) {
        setTitle(row.title)
        titleRef.current = row.title
        savedTitleRef.current = row.title
      } else if (row.title !== savedTitleRef.current && titleRef.current === savedTitleRef.current) {
        // The notes pass renamed an auto-titled meeting under us. Adopt it,
        // but only while the user has not started typing their own.
        setTitle(row.title)
        titleRef.current = row.title
        savedTitleRef.current = row.title
      }
      return row
    },
    [id],
  )

  useEffect(() => {
    let alive = true
    api
      .getSettings()
      .then((values) => {
        if (alive) setSettings(values)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setLoading(true)
    load(true)
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load this meeting')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [id, load])

  // The recorder page navigates here the moment /stop returns, so the pipeline
  // is usually still running. Poll until it lands on a terminal status.
  const processing = meeting?.status === 'processing'
  useEffect(() => {
    if (!processing) return
    let cancelled = false
    const timer = window.setInterval(() => {
      if (cancelled) return
      void load(false).catch(() => undefined)
    }, POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [processing, load])

  // Space toggles playback, as long as the user is not typing.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== 'Space' && event.key !== ' ') return
      const target = event.target as HTMLElement | null
      const tag = target?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON') return
      if (target?.isContentEditable) return
      event.preventDefault()
      playerRef.current?.toggle()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const commitTitle = async () => {
    if (!id) return
    const next = title.trim()
    if (!next) {
      setTitle(savedTitleRef.current)
      titleRef.current = savedTitleRef.current
      return
    }
    if (next === savedTitleRef.current) return
    try {
      const updated = await api.renameMeeting(id, next)
      setMeeting(updated)
      setTitle(updated.title)
      titleRef.current = updated.title
      savedTitleRef.current = updated.title
    } catch (err) {
      setTitle(savedTitleRef.current)
      titleRef.current = savedTitleRef.current
      setError(err instanceof Error ? err.message : 'Could not rename this meeting')
    }
  }

  const handleReprocess = async () => {
    if (!id) return
    setError(null)
    try {
      setMeeting(await api.reprocessMeeting(id))
      toast.show('Reprocessing this recording')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start reprocessing')
    }
  }

  const handleRegenerate = async () => {
    if (!id) return
    setError(null)
    try {
      setMeeting(await api.regenerateNotes(id))
      toast.show('Writing the notes again')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not regenerate the notes')
    }
  }

  const handleRename = async (speakerId: string, name: string) => {
    if (!id) return
    try {
      setMeeting(await api.renameSpeakers(id, { [speakerId]: name }))
      // Labels live on the transcript response, so re-read it too.
      setTranscript(await api.getTranscript(id))
      toast.show(`Renamed to ${name}`)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not rename that speaker')
    }
  }

  const handleToggleItem = async (index: number, done: boolean) => {
    if (!id || !meeting) return
    // Optimistic: the checkbox must feel instant.
    const previous = meeting
    const items = (meeting.notes?.action_items ?? []).map((item, position) =>
      position === index ? { ...item, done } : item,
    )
    setMeeting({ ...meeting, notes: meeting.notes ? { ...meeting.notes, action_items: items } : null })
    try {
      setMeeting(await api.setActionItem(id, index, done))
    } catch (err) {
      setMeeting(previous)
      toast.error(err instanceof Error ? err.message : 'Could not save that action item')
    }
  }

  const handleCopy = async () => {
    if (!id) return
    try {
      const markdown = await api.exportMarkdown(id)
      await navigator.clipboard.writeText(markdown)
      toast.show('Notes copied to the clipboard')
    } catch {
      toast.error('Could not copy the notes')
    }
  }

  const handleDelete = async () => {
    if (!id || !meeting) return
    if (!window.confirm(`Delete "${meeting.title}"? The recording will be removed permanently.`)) return
    try {
      await api.deleteMeeting(id)
      navigate('/meetings')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete this meeting')
    }
  }

  if (loading) {
    return (
      <div className="card empty-state">
        <p>Loading...</p>
      </div>
    )
  }

  if (!meeting) {
    return (
      <div>
        {error && (
          <div className="error-banner" role="alert">
            {error}
          </div>
        )}
        <div className="card empty-state">
          <p>That meeting could not be found.</p>
          <button type="button" className="btn btn-secondary" onClick={() => navigate('/meetings')}>
            <ChevronLeftIcon size={16} />
            Back to meetings
          </button>
        </div>
      </div>
    )
  }

  const segments = transcript?.segments ?? []
  const stage = meeting.pipeline_stage
  const isUpload = meeting.source === 'upload'
  const identifying = processing && (stage === 'identifying_speakers' || stage === 'diarizing')
  const summarizing = processing && stage === 'summarizing'
  const transcribing = processing && stage === 'transcribing'

  return (
    <div>
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}

      {meeting.pipeline_error && (
        <div className="error-banner" role="alert">
          {meeting.pipeline_error}
        </div>
      )}

      <div className="detail-head">
        <div className="detail-topbar">
          <p className="detail-meta">
            {isUpload ? (
              <>
                Uploaded {formatLongDate(meeting.created_at)}
                {meeting.source_filename ? ` \u00b7 ${meeting.source_filename}` : ''} &middot;{' '}
                {formatDuration(meeting.duration_seconds)}
              </>
            ) : (
              <>
                {formatLongDate(meeting.created_at)} &middot; {formatTimeOfDay(meeting.created_at)}{' '}
                &middot; {formatDuration(meeting.duration_seconds)}
              </>
            )}
          </p>
          <div className="detail-actions">
            <MeetingStatusPill
              status={meeting.status}
              stage={meeting.pipeline_stage}
              hasNotes={meeting.has_notes}
              hasError={Boolean(meeting.pipeline_error)}
            />
            <a className="btn btn-secondary" href={api.exportUrl(meeting.id)} download>
              <DownloadIcon size={15} />
              Export Markdown
            </a>
            <button type="button" className="btn btn-secondary" onClick={handleCopy}>
              <CopyIcon size={15} />
              Copy notes
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleRegenerate}
              disabled={processing || !meeting.has_transcript}
            >
              <RefreshIcon size={15} />
              Regenerate notes
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleReprocess}
              disabled={processing || !meeting.has_audio}
            >
              <RefreshIcon size={15} />
              Reprocess
            </button>
            <button type="button" className="btn btn-danger" onClick={handleDelete}>
              <TrashIcon size={15} />
              Delete
            </button>
          </div>
        </div>
          <input
            className="title-input"
            value={title}
            onChange={(event) => {
              setTitle(event.target.value)
              titleRef.current = event.target.value
            }}
            onBlur={commitTitle}
            onKeyDown={(event) => {
              if (event.key === 'Enter') event.currentTarget.blur()
              if (event.key === 'Escape') {
                setTitle(savedTitleRef.current)
                titleRef.current = savedTitleRef.current
                event.currentTarget.blur()
              }
            }}
            aria-label="Meeting title"
            spellCheck={false}
          />
        <SpeakerChips
          speakers={meeting.speakers}
          onRename={handleRename}
          pending={identifying}
        />
        {processing && !meeting.pipeline_error && (
          <PipelineProgress
            stage={stage}
            hasSpeakers={meeting.speakers.length > 0}
            durationSeconds={meeting.duration_seconds}
            diarizationEnabled={settings?.diarization_enabled ?? true}
            isUpload={isUpload}
          />
        )}
      </div>

      <div className="detail-columns">
        <section className="card">
          <div className="card-head">
            <h2 className="card-title">Transcript</h2>
            {transcript?.source === 'live' && segments.length > 0 && (
              <span className="card-note">Live pass</span>
            )}
          </div>
          <div className="card-body">
            {identifying && (
              <p className="inline-banner" role="status">
                <span className="working-dots" aria-hidden="true">
                  <span />
                  <span />
                  <span />
                </span>
                Identifying speakers... this runs at about real time on this laptop, notes are
                already ready
              </p>
            )}

            {segments.length > 0 && (
              <div className="transcript-scroll tall">
                <TranscriptList
                  segments={segments}
                  currentTime={currentTime}
                  onSeek={(seconds) => playerRef.current?.seek(seconds)}
                />
              </div>
            )}

            {transcribing && <WorkingIndicator label={`${stageLabel(stage)}...`} />}

            {segments.length === 0 && !processing && (
              <p className="placeholder">
                No transcript yet. Use Reprocess to run transcription again.
              </p>
            )}
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h2 className="card-title">Notes</h2>
            {meeting.notes?.with_speakers && <span className="card-note">With speakers</span>}
          </div>
          <div className="card-body">
            <div className="notes-scroll">
              <NotesPanel
                notes={meeting.notes}
                speakers={meeting.speakers}
                working={summarizing || (transcribing && !meeting.notes)}
                identifying={identifying}
                onToggleItem={handleToggleItem}
                onSeek={(seconds) => playerRef.current?.seek(seconds)}
              />
            </div>
          </div>
        </section>
      </div>

      <AudioPlayerBar
        ref={playerRef}
        src={meeting.has_audio ? api.audioUrl(meeting.id) : null}
        fallbackDuration={meeting.duration_seconds}
        onTimeChange={setCurrentTime}
      />
    </div>
  )
}
