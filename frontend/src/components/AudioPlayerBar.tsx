import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { PauseIcon, PlayIcon } from './Icons'
import { formatClockShort } from '../lib/format'

/** What the page needs from the player: jump to a segment, and play/pause. */
export interface PlayerHandle {
  seek: (seconds: number) => void
  /** Bound to Space on the detail page. */
  toggle: () => void
}

interface Props {
  /** null while the recording has no wav yet. */
  src: string | null
  /** Server-side duration, used until the element reports its own. */
  fallbackDuration: number | null
  /** Playback position, so the caller can highlight the current segment. */
  onTimeChange?: (seconds: number) => void
}

export const AudioPlayerBar = forwardRef<PlayerHandle, Props>(function AudioPlayerBar(
  { src, fallbackDuration, onTimeChange },
  ref,
) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const trackRef = useRef<HTMLButtonElement | null>(null)
  const [playing, setPlaying] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [duration, setDuration] = useState<number | null>(fallbackDuration)

  useEffect(() => {
    setPlaying(false)
    setCurrentTime(0)
    setDuration(fallbackDuration)
  }, [src, fallbackDuration])

  const report = (seconds: number) => {
    setCurrentTime(seconds)
    onTimeChange?.(seconds)
  }

  useImperativeHandle(ref, () => ({
    seek: (seconds: number) => {
      const audio = audioRef.current
      if (!audio) return
      // Range requests from the backend make this land without a full download.
      audio.currentTime = Math.max(0, seconds)
      report(audio.currentTime)
      if (audio.paused) void audio.play().catch(() => setPlaying(false))
    },
    toggle: () => {
      const audio = audioRef.current
      if (!audio) return
      if (audio.paused) void audio.play().catch(() => setPlaying(false))
      else audio.pause()
    },
  }))

  const total = duration && Number.isFinite(duration) && duration > 0 ? duration : null
  const progress = total ? Math.min(100, (currentTime / total) * 100) : 0

  const toggle = () => {
    const audio = audioRef.current
    if (!audio || !src) return
    if (audio.paused) void audio.play().catch(() => setPlaying(false))
    else audio.pause()
  }

  const seek = (event: React.MouseEvent<HTMLButtonElement>) => {
    const audio = audioRef.current
    const track = trackRef.current
    if (!audio || !track || !total) return
    const bounds = track.getBoundingClientRect()
    const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width))
    audio.currentTime = ratio * total
    report(audio.currentTime)
  }

  return (
    <div className="player-bar">
      <button
        type="button"
        className="play-btn"
        onClick={toggle}
        disabled={!src}
        aria-label={playing ? 'Pause' : 'Play'}
      >
        {playing ? <PauseIcon size={16} /> : <PlayIcon size={16} />}
      </button>

      <span className="player-time">{formatClockShort(currentTime)}</span>

      <button
        type="button"
        ref={trackRef}
        className="player-track"
        onClick={seek}
        disabled={!src || !total}
        aria-label="Seek"
      >
        <span className="player-progress" style={{ width: `${progress}%` }} />
        {total !== null && <span className="player-knob" style={{ left: `${progress}%` }} />}
      </button>

      <span className="player-time">{total ? formatClockShort(total) : '--:--'}</span>

      {!src && <span className="player-note">Audio not available</span>}

      {src && (
        <audio
          ref={audioRef}
          src={src}
          preload="metadata"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onTimeUpdate={(event) => report(event.currentTarget.currentTime)}
          onLoadedMetadata={(event) => {
            const value = event.currentTarget.duration
            if (Number.isFinite(value) && value > 0) setDuration(value)
          }}
        />
      )}
    </div>
  )
})
