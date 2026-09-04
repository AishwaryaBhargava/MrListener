import { CheckIcon } from './Icons'
import type { ActionItem, Notes, Speaker } from '../lib/api'
import { formatStamp } from '../lib/format'
import { colorForId } from '../lib/speakers'

interface Props {
  notes: Notes | null
  speakers: Speaker[]
  /** True while the backend is writing (or rewriting) them. */
  working: boolean
  onToggleItem: (index: number, done: boolean) => void
  /** Jump the player to where an action item was said. */
  onSeek: (seconds: number) => void
}

/**
 * The detail page's right column.
 *
 * Empty sections are hidden rather than shown empty: a meeting with no
 * decisions should not display a "DECISIONS" heading over nothing. The whole
 * panel is driven by `notes` alone, so the second (speaker-aware) notes pass
 * simply replaces the contents on the next poll.
 */
export function NotesPanel({ notes, speakers, working, onToggleItem, onSeek }: Props) {
  if (working && !notes) return <NotesSkeleton />

  if (!notes) {
    return (
      <p className="placeholder">
        No notes yet. Use Regenerate notes once the transcript is in.
      </p>
    )
  }

  const colorFor = (speakerId: string | null | undefined) => {
    const match = speakers.find((speaker) => speaker.id === speakerId)
    return match ? colorForId(match.id) : colorForId(speakerId)
  }

  return (
    <div className="notes">
      {working && <NotesWorkingBar />}

      {notes.summary && (
        <Section title="Summary">
          <p className="notes-summary">{notes.summary}</p>
        </Section>
      )}

      <ListSection title="Key takeaways" items={notes.key_takeaways} />
      <ListSection title="Decisions" items={notes.decisions} />

      {notes.action_items.length > 0 && (
        <Section title="Action items">
          <ul className="action-list">
            {notes.action_items.map((item, index) => (
              <ActionRow
                key={`${index}-${item.task}`}
                item={item}
                color={colorFor(item.owner_speaker_id)}
                onToggle={() => onToggleItem(index, !item.done)}
                onSeek={onSeek}
              />
            ))}
          </ul>
        </Section>
      )}

      <ListSection title="Open questions" items={notes.open_questions} />
      <ListSection title="Follow-up questions" items={notes.follow_up_questions ?? []} />
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="notes-section">
      <h3 className="notes-heading">{title}</h3>
      {children}
    </section>
  )
}

function ListSection({ title, items }: { title: string; items: string[] }) {
  if (!items || items.length === 0) return null
  return (
    <Section title={title}>
      <ul className="notes-list">
        {items.map((item, index) => (
          <li key={`${index}-${item.slice(0, 24)}`}>{item}</li>
        ))}
      </ul>
    </Section>
  )
}

interface RowProps {
  item: ActionItem
  color: { tint: string; text: string; dot: string }
  onToggle: () => void
  onSeek: (seconds: number) => void
}

function ActionRow({ item, color, onToggle, onSeek }: RowProps) {
  return (
    <li className={item.done ? 'action-row done' : 'action-row'}>
      <button
        type="button"
        className={item.done ? 'action-check checked' : 'action-check'}
        onClick={onToggle}
        role="checkbox"
        aria-checked={item.done}
        aria-label={item.done ? `Mark "${item.task}" as not done` : `Mark "${item.task}" as done`}
      >
        {item.done && <CheckIcon size={12} />}
      </button>

      <div className="action-body">
        <span className="action-task">{item.task}</span>
        <span className="action-meta">
          {item.owner && (
            <span
              className="owner-pill"
              style={{ background: color.tint, color: color.text }}
            >
              {item.owner}
            </span>
          )}
          {item.due && <span className="action-due">due {item.due}</span>}
          {item.source_time !== null && (
            <button
              type="button"
              className="action-time"
              onClick={() => onSeek(item.source_time as number)}
              title="Play from here"
            >
              {formatStamp(item.source_time)}
            </button>
          )}
        </span>
      </div>
    </li>
  )
}

/** Shown while the very first notes pass is still running. */
export function NotesSkeleton() {
  return (
    <div className="notes-skeleton" aria-hidden="true">
      <span className="skeleton-heading" />
      <span className="skeleton-line" />
      <span className="skeleton-line" />
      <span className="skeleton-line short" />
      <span className="skeleton-heading" />
      <span className="skeleton-line" />
      <span className="skeleton-line short" />
    </div>
  )
}

function NotesWorkingBar() {
  return (
    <p className="notes-refreshing" role="status">
      <span className="working-dots" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      Rewriting the notes with speaker names
    </p>
  )
}
