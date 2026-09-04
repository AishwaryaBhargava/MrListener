import { CheckIcon } from './Icons'
import type { ActionItem, Notes, OpenQuestion, Speaker, SpeakerNotes, Topic } from '../lib/api'
import { formatStamp } from '../lib/format'
import { colorForId, type SpeakerColor } from '../lib/speakers'

interface Props {
  notes: Notes | null
  speakers: Speaker[]
  /** True while the backend is writing (or rewriting) them. */
  working: boolean
  /** True while diarization is still running, so BY SPEAKER is only pending. */
  identifying?: boolean
  onToggleItem: (index: number, done: boolean) => void
  /** Jump the player to a topic, an action item or a question. */
  onSeek: (seconds: number) => void
}

/**
 * The detail page's right column.
 *
 * Sections run summary, topics, decisions, action items, per-speaker, open
 * questions, follow-ups - narrowest to broadest, with everything the meeting
 * decided near the top. Empty sections are hidden rather than shown empty: a
 * meeting with no decisions should not display a "DECISIONS" heading over
 * nothing. The whole panel is driven by `notes` alone, so the second
 * (speaker-aware) notes pass simply replaces the contents on the next poll -
 * which is when BY SPEAKER stops being a placeholder and becomes real blocks.
 */
export function NotesPanel({
  notes,
  speakers,
  working,
  identifying,
  onToggleItem,
  onSeek,
}: Props) {
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

  const topics = notes.topics ?? []
  const bySpeaker = notes.by_speaker ?? []
  const openQuestions = notes.open_questions ?? []

  return (
    <div className="notes">
      {working && <NotesWorkingBar />}

      {notes.summary && (
        <Section title="Summary">
          <p className="notes-summary">{notes.summary}</p>
        </Section>
      )}

      <ListSection title="Key takeaways" items={notes.key_takeaways} />

      {topics.length > 0 && (
        <Section title="Topics">
          <ul className="topic-list">
            {topics.map((topic, index) => (
              <TopicRow key={`${index}-${topic.title}`} topic={topic} onSeek={onSeek} />
            ))}
          </ul>
        </Section>
      )}

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

      {(bySpeaker.length > 0 || identifying) && (
        <Section title="By speaker">
          {bySpeaker.length > 0 ? (
            <div className="speaker-notes">
              {bySpeaker.map((block) => (
                <SpeakerBlock
                  key={block.speaker_id}
                  block={block}
                  color={colorFor(block.speaker_id)}
                />
              ))}
            </div>
          ) : (
            <p className="notes-pending" role="status">
              <span className="working-dots" aria-hidden="true">
                <span />
                <span />
                <span />
              </span>
              Speaker breakdown arrives once speakers are identified
            </p>
          )}
        </Section>
      )}

      {openQuestions.length > 0 && (
        <Section title="Open questions">
          <ul className="question-list">
            {openQuestions.map((item, index) => (
              <QuestionRow
                key={`${index}-${item.question.slice(0, 24)}`}
                item={item}
                color={colorFor(item.asked_by_speaker_id)}
                onSeek={onSeek}
              />
            ))}
          </ul>
        </Section>
      )}

      <ListSection title="Follow-ups for you" items={notes.follow_up_questions ?? []} />
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

/** One chapter: a range button that seeks, the title, and a one-liner. */
function TopicRow({ topic, onSeek }: { topic: Topic; onSeek: (seconds: number) => void }) {
  return (
    <li className="topic-row">
      <button
        type="button"
        className="action-time topic-range"
        onClick={() => onSeek(topic.start)}
        title="Play from here"
      >
        {formatStamp(topic.start)}-{formatStamp(topic.end)}
      </button>
      <div className="topic-body">
        <span className="topic-title">{topic.title}</span>
        {topic.summary && <span className="topic-summary">{topic.summary}</span>}
      </div>
    </li>
  )
}

interface RowProps {
  item: ActionItem
  color: SpeakerColor
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

/** One speaker's contribution, under a chip in their own colour. */
function SpeakerBlock({ block, color }: { block: SpeakerNotes; color: SpeakerColor }) {
  const groups: Array<[string, string[]]> = [
    ['Main points', block.main_points ?? []],
    ['Commitments', block.commitments ?? []],
    ['Questions raised', block.questions_raised ?? []],
  ]
  const filled = groups.filter(([, items]) => items.length > 0)
  if (filled.length === 0) return null

  return (
    <div className="speaker-note">
      <p className="speaker-note-name" style={{ color: color.text }}>
        <span className="speaker-dot" style={{ background: color.dot }} />
        {block.name ?? block.speaker_id}
      </p>
      {filled.map(([label, items]) => (
        <div key={label} className="speaker-note-group">
          <span className="speaker-note-label">{label}</span>
          <ul className="notes-list">
            {items.map((item, index) => (
              <li key={`${index}-${item.slice(0, 24)}`}>{item}</li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

/** A question asked in the room, with who asked it and whether it landed. */
function QuestionRow({
  item,
  color,
  onSeek,
}: {
  item: OpenQuestion
  color: SpeakerColor
  onSeek: (seconds: number) => void
}) {
  return (
    <li className="question-row">
      <span className="question-text">{item.question}</span>
      <span className="question-meta">
        {item.asked_by && (
          <span className="question-asker" style={{ color: color.text }}>
            asked by {item.asked_by}
          </span>
        )}
        {item.time !== null && (
          <button
            type="button"
            className="action-time"
            onClick={() => onSeek(item.time as number)}
            title="Play from here"
          >
            {formatStamp(item.time)}
          </button>
        )}
        <span className={item.answered ? 'question-state answered' : 'question-state'}>
          {item.answered ? 'answered' : 'unanswered'}
        </span>
      </span>
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
