import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  FileAudioIcon,
  MicIcon,
  PauseIcon,
  PlayIcon,
  StopIcon,
  UploadIcon,
} from '../components/Icons'
import { StatusPill, type PillTone } from '../components/StatusPill'
import { LiveTranscript } from '../components/Transcript'
import { useAudioMeter } from '../hooks/useAudioMeter'
import { useRecorder } from '../hooks/useRecorder'
import { UPLOAD_ACCEPT, UPLOAD_EXTENSIONS, describeRejection, useUpload } from '../hooks/useUpload'
import { formatBytes, formatTimer } from '../lib/format'

const BAR_COUNT = 14

export default function RecordPage() {
  const navigate = useNavigate()
  const recorder = useRecorder()
  const uploader = useUpload()
  // Empty until the backend hands out a name; the user may then edit it.
  const [title, setTitle] = useState('')
  const savedTitleRef = useRef('')

  // The upload card keeps its own title, so choosing a file never disturbs the
  // name of a recording the user is about to start.
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadTitle, setUploadTitle] = useState('')
  const [dragging, setDragging] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const isLive = recorder.state === 'recording'
  const isPaused = recorder.state === 'paused'
  const isActive = isLive || isPaused
  const isBusy = recorder.state === 'starting' || recorder.state === 'stopping'
  // Recording and uploading at once would fight over the same page, so the
  // drop zone is inert while the microphone is live.
  const uploadDisabled = isActive || isBusy || uploader.uploading

  // Bars only animate while audio is actually flowing, so a pause is visible.
  const { setBarRef, levelRef } = useAudioMeter(recorder.analyserRef, isLive, BAR_COUNT)

  // Adopt the name the backend assigned, unless the user is mid-edit.
  const assigned = recorder.meeting?.title ?? ''
  useEffect(() => {
    if (!assigned) return
    setTitle((current) => (current === savedTitleRef.current ? assigned : current))
    savedTitleRef.current = assigned
  }, [assigned])

  const pill = useMemo<{ tone: PillTone; label: string }>(() => {
    switch (recorder.state) {
      case 'recording':
        return { tone: 'recording', label: 'Recording' }
      case 'paused':
        return { tone: 'processing', label: 'Paused' }
      case 'starting':
        return { tone: 'neutral', label: 'Starting' }
      case 'stopping':
        return { tone: 'processing', label: 'Processing' }
      default:
        return { tone: 'neutral', label: 'Idle' }
    }
  }, [recorder.state])

  const handleStart = async () => {
    await recorder.start(title.trim() || undefined)
  }

  const handleStop = async () => {
    const meeting = await recorder.stop()
    if (meeting) navigate(`/meetings/${meeting.id}`)
  }

  // Space toggles pause while a recording is running, as long as the user is
  // not typing into the title box or tabbed onto a button.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== 'Space' && event.key !== ' ') return
      if (!isLive && !isPaused) return
      const target = event.target as HTMLElement | null
      const tag = target?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON') return
      if (target?.isContentEditable) return
      event.preventDefault()
      if (isPaused) recorder.resume()
      else recorder.pause()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isLive, isPaused, recorder])

  const chooseFile = (file: File | null | undefined) => {
    if (!file) return
    uploader.clearError()
    const rejection = describeRejection(file)
    if (rejection) {
      // Refuse it outright: nothing gets selected, the message says why.
      setUploadFile(null)
      uploader.setError(rejection)
      return
    }
    setUploadFile(file)
  }

  const handleDrop = (event: React.DragEvent) => {
    event.preventDefault()
    setDragging(false)
    if (uploadDisabled) return
    chooseFile(event.dataTransfer.files?.[0])
  }

  const handleUpload = async () => {
    if (!uploadFile) return
    const meeting = await uploader.upload(uploadFile, uploadTitle)
    if (meeting) {
      setUploadFile(null)
      setUploadTitle('')
    }
  }

  const commitTitle = () => {
    const next = title.trim()
    if (!next) {
      setTitle(savedTitleRef.current)
      return
    }
    if (next === savedTitleRef.current) return
    savedTitleRef.current = next
    void recorder.rename(next)
  }

  return (
    <div className="stack">
      <div className="page-head">
        <div style={{ flex: 1, minWidth: 0 }}>
          <p className="eyebrow">Recording</p>
          <input
            className="title-input"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            onBlur={commitTitle}
            onKeyDown={(event) => {
              if (event.key === 'Enter') event.currentTarget.blur()
              if (event.key === 'Escape') {
                setTitle(savedTitleRef.current)
                event.currentTarget.blur()
              }
            }}
            placeholder="Recording will be named automatically"
            aria-label="Meeting title"
            spellCheck={false}
          />
        </div>
        <StatusPill tone={pill.tone} label={pill.label} />
      </div>

      {recorder.error && (
        <div className="error-banner" role="alert">
          {recorder.error}
        </div>
      )}

      <div className={isActive ? 'record-top solo' : 'record-top'}>
      <section className="card record-main">
        <div className="record-card">
          <div className={isLive ? 'visualizer' : 'visualizer idle'} aria-hidden="true">
            {Array.from({ length: BAR_COUNT }, (_, index) => (
              <div key={index} className="viz-bar" ref={setBarRef(index)} />
            ))}
          </div>

          <p className="timer" aria-live="off">
            {formatTimer(recorder.elapsedSeconds)}
          </p>

          <div className="record-actions">
            {!isActive ? (
              <button
                type="button"
                className="btn btn-primary btn-lg"
                onClick={handleStart}
                disabled={isBusy}
              >
                <MicIcon size={17} />
                Start recording
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className="btn btn-secondary btn-lg"
                  onClick={() => (isPaused ? recorder.resume() : recorder.pause())}
                >
                  {isPaused ? <PlayIcon size={15} /> : <PauseIcon size={15} />}
                  {isPaused ? 'Resume' : 'Pause'}
                </button>

                <button
                  type="button"
                  className="btn btn-primary btn-lg"
                  onClick={handleStop}
                  disabled={isBusy}
                >
                  <StopIcon size={16} />
                  Stop and process
                </button>
              </>
            )}
          </div>
        </div>

        <div className="record-foot">
          <label className="field">
            <span className="field-label">Microphone</span>
            <select
              className="select"
              value={recorder.deviceId}
              onChange={(event) => recorder.selectDevice(event.target.value)}
              disabled={isActive}
            >
              <option value="">System default</option>
              {recorder.devices.map((device, index) => (
                <option key={device.deviceId || index} value={device.deviceId}>
                  {device.label || `Microphone ${index + 1}`}
                </option>
              ))}
            </select>
          </label>

          <div className="level">
            <span className="field-label">Input level</span>
            <div className="level-track">
              <div className="level-fill" ref={levelRef} />
            </div>
          </div>
        </div>
      </section>

      {!isActive && (
      <section className="card upload-card">
        <div className="card-head">
          <h2 className="card-title">Upload a recording</h2>
          <span className="card-note">Already have the audio?</span>
        </div>
        <div className="card-body">
          <div
            className={`dropzone${dragging ? ' dragging' : ''}${uploadDisabled ? ' disabled' : ''}`}
            onDragOver={(event) => {
              event.preventDefault()
              if (!uploadDisabled) setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={handleDrop}
          >
            <span className="dropzone-mark" aria-hidden="true">
              {uploadFile ? <FileAudioIcon size={20} /> : <UploadIcon size={20} />}
            </span>

            {uploadFile ? (
              <div className="dropzone-file">
                <span className="dropzone-filename" title={uploadFile.name}>
                  {uploadFile.name}
                </span>
                <span className="dropzone-hint">{formatBytes(uploadFile.size)}</span>
              </div>
            ) : (
              <div className="dropzone-file">
                <span className="dropzone-lead">Drag a file here</span>
                <span className="dropzone-hint">
                  {UPLOAD_EXTENSIONS.join(', ')} &middot; up to 2 GB
                </span>
              </div>
            )}

            <input
              ref={fileInputRef}
              type="file"
              accept={UPLOAD_ACCEPT}
              className="visually-hidden"
              onChange={(event) => {
                chooseFile(event.target.files?.[0])
                // Reset, so picking the same file twice still fires onChange.
                event.target.value = ''
              }}
            />
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadDisabled}
            >
              {uploadFile ? 'Choose another' : 'Choose file'}
            </button>
          </div>

          {uploader.uploading && (
            <div className="upload-progress" role="status" aria-live="polite">
              <div className="upload-track">
                <div
                  className="upload-fill"
                  style={{ width: `${Math.round(uploader.progress * 100)}%` }}
                />
              </div>
              <span className="upload-percent">{Math.round(uploader.progress * 100)}%</span>
            </div>
          )}

          {uploader.error && (
            <p className="upload-error" role="alert">
              {uploader.error}
            </p>
          )}

          <div className="upload-foot">
            <label className="field">
              <span className="field-label">Title (optional)</span>
              <input
                className="input"
                value={uploadTitle}
                onChange={(event) => setUploadTitle(event.target.value)}
                placeholder="Named automatically when left empty"
                disabled={uploadDisabled}
                spellCheck={false}
              />
            </label>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleUpload}
              disabled={uploadDisabled || !uploadFile}
            >
              <UploadIcon size={16} />
              {uploader.uploading ? 'Uploading...' : 'Upload and process'}
            </button>
          </div>

        </div>
      </section>
      )}
      </div>

      <section className="card record-transcript">
        <div className="card-head">
          <h2 className="card-title">Live transcript</h2>
          {isPaused && <span className="card-note">Paused</span>}
        </div>
        <div className="card-body">
          <LiveTranscript
            segments={recorder.segments}
            waiting={isActive && recorder.transcriptError === null}
            error={recorder.transcriptError}
          />
        </div>
      </section>
    </div>
  )
}
