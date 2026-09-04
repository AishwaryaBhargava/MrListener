import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { TranscriptSegment } from '../lib/api'
import { formatStamp } from '../lib/format'
import { colorForId } from '../lib/speakers'

/** Three dots that fade in turn - used wherever the backend is still working. */
export function WorkingIndicator({ label }: { label: string }) {
  return (
    <p className="working" role="status">
      <span className="working-dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      {label}
    </p>
  )
}

interface LiveProps {
  segments: TranscriptSegment[]
  /** True between pressing start and the first window landing. */
  waiting: boolean
  error: string | null
}

/**
 * The Record page's feed. Follows the tail as lines arrive, but stops
 * following the moment the user scrolls up to read something.
 */
export function LiveTranscript({ segments, waiting, error }: LiveProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickRef = useRef(true)

  const handleScroll = () => {
    const node = scrollRef.current
    if (!node) return
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight
    stickRef.current = distance < 32
  }

  useLayoutEffect(() => {
    const node = scrollRef.current
    if (node && stickRef.current) node.scrollTop = node.scrollHeight
  }, [segments.length, waiting])

  const empty = segments.length === 0

  return (
    <div className="transcript-scroll" ref={scrollRef} onScroll={handleScroll}>
      {error && (
        <p className="transcript-error" role="alert">
          {error}
        </p>
      )}

      {empty && !waiting && !error && (
        <p className="placeholder">
          Start recording and the transcript appears here as it is captured.
        </p>
      )}

      {!empty && (
        <ol className="transcript-lines">
          {segments.map((segment) => (
            <li key={`${segment.id}-${segment.start}`} className="transcript-line">
              <span className="transcript-stamp">{formatStamp(segment.start)}</span>
              <span className="transcript-text">{segment.text}</span>
            </li>
          ))}
        </ol>
      )}

      {waiting && <WorkingIndicator label="Transcribing..." />}
    </div>
  )
}

interface Group {
  key: string
  speaker: string | null
  speakerId: string | null
  start: number
  segments: TranscriptSegment[]
}

/**
 * Consecutive segments by one speaker collapse under a single header.
 *
 * Whisper splits on breath, not on turns, so an unfolded transcript repeats
 * "Priya" every six seconds. Grouping is what makes it read like a
 * conversation. Before diarization lands nothing carries a speaker, and every
 * segment becomes its own headerless group - which is exactly the plain
 * timestamped list the earlier stages showed.
 */
function group(segments: TranscriptSegment[]): Group[] {
  const groups: Group[] = []
  for (const segment of segments) {
    const speakerId = segment.speaker_id ?? null
    const last = groups[groups.length - 1]
    if (last && speakerId && last.speakerId === speakerId) {
      last.segments.push(segment)
      continue
    }
    groups.push({
      key: `${segment.id}`,
      speaker: segment.speaker ?? null,
      speakerId,
      start: segment.start,
      segments: [segment],
    })
  }
  return groups
}

interface ListProps {
  segments: TranscriptSegment[]
  /** Playback position, used to highlight the segment being spoken. */
  currentTime: number
  onSeek: (seconds: number) => void
}

/** The detail page's transcript. Clicking a line seeks the player to it. */
export function TranscriptList({ segments, currentTime, onSeek }: ListProps) {
  const [activeId, setActiveId] = useState<number | null>(null)
  const groups = useMemo(() => group(segments), [segments])

  useEffect(() => {
    const match = segments.find(
      (segment) => currentTime >= segment.start && currentTime < segment.end,
    )
    setActiveId(match ? match.id : null)
  }, [currentTime, segments])

  return (
    <div className="transcript-groups">
      {groups.map((entry) => {
        const color = colorForId(entry.speakerId)
        return (
          <div key={entry.key} className="transcript-group">
            {entry.speaker && (
              <p className="transcript-speaker" style={{ color: color.text }}>
                <span className="speaker-dot" style={{ background: color.dot }} />
                {entry.speaker}
                <span className="transcript-speaker-time">{formatStamp(entry.start)}</span>
              </p>
            )}
            <ol className="transcript-lines">
              {entry.segments.map((segment) => (
                <li key={segment.id}>
                  <button
                    type="button"
                    className={
                      segment.id === activeId
                        ? 'transcript-line seekable active'
                        : 'transcript-line seekable'
                    }
                    onClick={() => onSeek(segment.start)}
                  >
                    {!entry.speaker && (
                      <span className="transcript-stamp">{formatStamp(segment.start)}</span>
                    )}
                    <span className="transcript-text">{segment.text}</span>
                  </button>
                </li>
              ))}
            </ol>
          </div>
        )
      })}
    </div>
  )
}
