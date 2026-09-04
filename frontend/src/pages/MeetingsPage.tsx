import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ChevronRightIcon, PlusIcon, SearchIcon, TrashIcon, UploadIcon } from '../components/Icons'
import { MeetingStatusPill } from '../components/StatusPill'
import { useToast } from '../components/Toast'
import { UPLOAD_ACCEPT, useUpload } from '../hooks/useUpload'
import { api, type Meeting } from '../lib/api'
import { dateTileParts, meetingMeta } from '../lib/format'

/** Long enough that a typed word is one request, short enough to feel live. */
const DEBOUNCE_MS = 300

export default function MeetingsPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const uploader = useUpload()
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  //: Rejects a slow response that lands after a newer one.
  const requestId = useRef(0)

  const search = useCallback(async (needle: string) => {
    const ticket = ++requestId.current
    try {
      const rows = await api.listMeetings(needle)
      if (ticket === requestId.current) {
        setMeetings(rows)
        setError(null)
      }
    } catch (err) {
      if (ticket === requestId.current) {
        setError(err instanceof Error ? err.message : 'Could not load meetings')
      }
    } finally {
      if (ticket === requestId.current) setLoading(false)
    }
  }, [])

  // The search runs on the server, across titles, transcripts and notes, so
  // the box finds a word that was said out loud - not just one in a title.
  useEffect(() => {
    const timer = window.setTimeout(() => void search(query), query ? DEBOUNCE_MS : 0)
    return () => window.clearTimeout(timer)
  }, [query, search])

  const handleDelete = async (event: React.MouseEvent, meeting: Meeting) => {
    // The card is a link; the trash icon inside it must not navigate.
    event.preventDefault()
    event.stopPropagation()
    if (!window.confirm(`Delete "${meeting.title}"? The recording will be removed permanently.`)) {
      return
    }
    try {
      await api.deleteMeeting(meeting.id)
      setMeetings((current) => current.filter((row) => row.id !== meeting.id))
      toast.show('Meeting deleted')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not delete that meeting')
    }
  }

  // Same flow as the record page's drop zone: the file picker opens, the
  // upload runs with a progress bar in the header, then the detail page takes
  // over the polling.
  const handleUpload = async (file: File | null | undefined) => {
    if (!file) return
    await uploader.upload(file)
  }

  const newestId = meetings[0]?.id

  return (
    <div>
      <div className="page-head">
        <div>
          <p className="eyebrow">Library</p>
          <h1 className="page-title">Meetings</h1>
        </div>
        <div className="head-actions">
          <input
            ref={fileInputRef}
            type="file"
            accept={UPLOAD_ACCEPT}
            className="visually-hidden"
            onChange={(event) => {
              void handleUpload(event.target.files?.[0])
              // Reset, so picking the same file twice still fires onChange.
              event.target.value = ''
            }}
          />
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploader.uploading}
          >
            <UploadIcon size={16} />
            {uploader.uploading ? 'Uploading...' : 'Upload audio'}
          </button>
          <button type="button" className="btn btn-primary" onClick={() => navigate('/')}>
            <PlusIcon size={16} />
            New recording
          </button>
        </div>
      </div>

      {uploader.uploading && (
        <div className="head-progress" role="status" aria-live="polite">
          <div className="upload-track">
            <div className="upload-fill" style={{ width: `${Math.round(uploader.progress * 100)}%` }} />
          </div>
          <span className="upload-percent">{Math.round(uploader.progress * 100)}%</span>
        </div>
      )}

      {uploader.error && (
        <div className="error-banner" role="alert">
          {uploader.error}
        </div>
      )}

      <div className="search-row">
        <span className="search-icon">
          <SearchIcon size={16} />
        </span>
        <input
          className="input"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search titles, transcripts and notes"
          aria-label="Search meetings"
        />
      </div>

      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}

      {loading ? (
        <div className="card empty-state">
          <p>Loading meetings...</p>
        </div>
      ) : meetings.length === 0 ? (
        <div className="card empty-state">
          <p>{query ? 'No meetings match that search.' : 'No meetings yet.'}</p>
          {!query && <p className="empty-hint">Start a recording and it will show up here.</p>}
        </div>
      ) : (
        <div className="meeting-list">
          {meetings.map((meeting) => {
            const tile = dateTileParts(meeting.created_at)
            const preview = meeting.match_snippet ?? meeting.notes_preview ?? meeting.transcript_preview
            return (
              <Link key={meeting.id} to={`/meetings/${meeting.id}`} className="meeting-card">
                <div className={meeting.id === newestId && !query ? 'date-tile newest' : 'date-tile'}>
                  <span className="date-tile-month">{tile.month}</span>
                  <span className="date-tile-day">{tile.day}</span>
                </div>

                <div className="meeting-main">
                  <div className="meeting-title-row">
                    <span className="meeting-title">{meeting.title}</span>
                    {meeting.source === 'upload' && <span className="source-tag">Uploaded</span>}
                    <MeetingStatusPill
                      status={meeting.status}
                      stage={meeting.pipeline_stage}
                      hasNotes={meeting.has_notes}
                      hasError={Boolean(meeting.pipeline_error)}
                    />
                  </div>
                  <div className="meeting-meta">{meetingMeta(meeting)}</div>
                  {preview && <p className="meeting-preview">{preview}</p>}
                </div>

                <button
                  type="button"
                  className="card-delete"
                  onClick={(event) => void handleDelete(event, meeting)}
                  aria-label={`Delete ${meeting.title}`}
                  title="Delete this meeting"
                >
                  <TrashIcon size={15} />
                </button>

                <ChevronRightIcon size={18} className="chevron" />
              </Link>
            )
          })}
        </div>
      )}
    </div>
  )
}
