import { useEffect, useRef, useState } from 'react'
import { PencilIcon } from './Icons'
import type { Speaker } from '../lib/api'
import { formatTalkTime, speakerColor } from '../lib/speakers'

interface Props {
  speakers: Speaker[]
  /** Resolves when the rename has been persisted. */
  onRename: (id: string, name: string) => Promise<void>
  /** True while the backend is still working out who is who. */
  pending?: boolean
}

/**
 * The speaker roster under the meeting title.
 *
 * Each chip carries the speaker's colour, so the same colour identifies them
 * in the transcript and on action-item owner pills. The pencil turns the chip
 * into an input in place; Enter or blur saves, Escape reverts. Nothing else on
 * the page needs to know about the edit - the parent re-reads the meeting and
 * every label follows, because names are resolved server-side at read time.
 */
export function SpeakerChips({ speakers, onRename, pending }: Props) {
  const [editing, setEditing] = useState<string | null>(null)

  if (speakers.length === 0 && !pending) return null

  return (
    <div className="speaker-chips">
      {speakers.map((speaker) => (
        <SpeakerChip
          key={speaker.id}
          speaker={speaker}
          editing={editing === speaker.id}
          onEdit={() => setEditing(speaker.id)}
          onDone={() => setEditing(null)}
          onRename={onRename}
        />
      ))}
    </div>
  )
}

interface ChipProps {
  speaker: Speaker
  editing: boolean
  onEdit: () => void
  onDone: () => void
  onRename: (id: string, name: string) => Promise<void>
}

function SpeakerChip({ speaker, editing, onEdit, onDone, onRename }: ChipProps) {
  const color = speakerColor(speaker.color_index)
  const [draft, setDraft] = useState(speaker.name)
  const inputRef = useRef<HTMLInputElement | null>(null)
  // Guards the double-fire when Enter blurs the input after already saving.
  const savingRef = useRef(false)

  useEffect(() => {
    if (editing) {
      setDraft(speaker.name)
      savingRef.current = false
      // Focus after the input has actually rendered.
      window.setTimeout(() => inputRef.current?.select(), 0)
    }
  }, [editing, speaker.name])

  const commit = async () => {
    if (savingRef.current) return
    savingRef.current = true
    const next = draft.trim()
    onDone()
    if (!next || next === speaker.name) return
    await onRename(speaker.id, next)
  }

  const talk = formatTalkTime(speaker.talk_time)

  if (editing) {
    return (
      <span className="speaker-chip editing" style={{ background: color.tint }}>
        <span className="speaker-dot" style={{ background: color.dot }} />
        <input
          ref={inputRef}
          className="speaker-chip-input"
          style={{ color: color.text }}
          value={draft}
          maxLength={80}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => void commit()}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void commit()
            if (event.key === 'Escape') {
              savingRef.current = true
              onDone()
            }
          }}
          aria-label={`Rename ${speaker.name}`}
          spellCheck={false}
        />
      </span>
    )
  }

  return (
    <span className="speaker-chip" style={{ background: color.tint, color: color.text }}>
      <span className="speaker-dot" style={{ background: color.dot }} />
      <span className="speaker-chip-name">{speaker.name}</span>
      {talk && <span className="speaker-chip-time">{talk}</span>}
      <button
        type="button"
        className="speaker-chip-edit"
        style={{ color: color.text }}
        onClick={onEdit}
        aria-label={`Rename ${speaker.name}`}
        title="Rename this speaker"
      >
        <PencilIcon size={13} />
      </button>
    </span>
  )
}
