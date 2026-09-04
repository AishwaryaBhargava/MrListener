import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { MicIcon, PauseIcon, PlayIcon, StopIcon } from '../components/Icons'
import { StatusPill, type PillTone } from '../components/StatusPill'
import { SuggestionsPanel, SuggestionsRefreshButton } from '../components/SuggestionsPanel'
import { useToast } from '../components/Toast'
import { LiveTranscript } from '../components/Transcript'
import { useAudioMeter } from '../hooks/useAudioMeter'
import { useRecorder } from '../hooks/useRecorder'
import { api, type Suggestion } from '../lib/api'
import { formatTimer } from '../lib/format'

const BAR_COUNT = 14

export default function RecordPage() {
  const navigate = useNavigate()
  const recorder = useRecorder()
  const toast = useToast()
  // Empty until the backend hands out a name; the user may then edit it.
  const [title, setTitle] = useState('')
  const savedTitleRef = useRef('')
  // Read once on mount: the panel needs to say so when suggestions are off.
  const [suggestionsEnabled, setSuggestionsEnabled] = useState(true)

  useEffect(() => {
    let cancelled = false
    void api
      .getSettings()
      .then((values) => {
        if (!cancelled) setSuggestionsEnabled(values.suggestions_enabled)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])

  const isLive = recorder.state === 'recording'
  const isPaused = recorder.state === 'paused'
  const isActive = isLive || isPaused
  const isBusy = recorder.state === 'starting' || recorder.state === 'stopping'

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

  const handleRefreshSuggestions = () => {
    void recorder.refreshSuggestions().catch((err: unknown) => {
      toast.error(err instanceof Error ? err.message : 'Could not refresh the suggestions')
    })
  }

  const handleTogglePin = (item: Suggestion) => {
    void recorder.togglePin(item).catch(() => toast.error('Could not save that pin'))
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

      <section className="card">
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

      <div className="record-columns">
        <section className="card">
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

        <section className="card">
          <div className="card-head">
            <div className="card-head-text">
              <h2 className="card-title">Ask next</h2>
              <p className="card-subtitle">Suggested from the conversation so far</p>
            </div>
            <SuggestionsRefreshButton
              onClick={handleRefreshSuggestions}
              busy={recorder.refreshingSuggestions}
              disabled={!isLive || !suggestionsEnabled}
            />
          </div>
          <div className="card-body">
            <SuggestionsPanel
              items={recorder.suggestions}
              pinned={recorder.pinnedSuggestions}
              generatedAt={recorder.suggestionsAt}
              hasBatch={recorder.hasSuggestions}
              active={isActive}
              enabled={suggestionsEnabled}
              onTogglePin={handleTogglePin}
            />
          </div>
        </section>
      </div>
    </div>
  )
}
