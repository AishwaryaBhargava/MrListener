import { CopyIcon, PinFilledIcon, PinIcon, RefreshIcon } from './Icons'
import { useToast } from './Toast'
import type { Suggestion, SuggestionKind } from '../lib/api'
import { formatStamp } from '../lib/format'

/**
 * The Record page's "Ask next" column.
 *
 * Two groups: whatever the user pinned, which never moves, and the newest
 * batch, which is replaced wholesale every refresh. The fade is keyed on the
 * batch stamp, so a new batch dissolves in rather than snapping.
 */

const KIND_LABELS: Record<SuggestionKind, string> = {
  question: 'Question',
  clarify: 'Clarify',
  follow_up: 'Follow up',
  risk: 'Risk',
}

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind as SuggestionKind] ?? 'Question'
}

interface Props {
  items: Suggestion[]
  pinned: Suggestion[]
  /** ISO stamp of the current batch; a change fades the unpinned rows in. */
  generatedAt: string | null
  /** True once a first batch has arrived for this recording. */
  hasBatch: boolean
  /** Recording or paused - anything else disables the refresh button. */
  active: boolean
  /** False when Live suggestions are turned off on the Settings page. */
  enabled: boolean
  onTogglePin: (item: Suggestion) => void
  /** Seek target on the detail page; omitted on the Record page. */
  onSeek?: (seconds: number) => void
}

export function SuggestionsPanel({
  items,
  pinned,
  generatedAt,
  hasBatch,
  active,
  enabled,
  onTogglePin,
  onSeek,
}: Props) {
  const toast = useToast()

  const copy = async (item: Suggestion) => {
    try {
      await navigator.clipboard.writeText(item.text)
      toast.show('Copied')
    } catch {
      toast.error('Could not copy that suggestion')
    }
  }

  if (!enabled) {
    return <p className="placeholder">Live suggestions are off in Settings.</p>
  }

  const empty = pinned.length === 0 && items.length === 0

  return (
    <div className="suggestions">
      {pinned.length > 0 && (
        <section className="suggestion-group">
          <h3 className="notes-heading">Pinned</h3>
          <ul className="suggestion-list">
            {pinned.map((item, index) => (
              <SuggestionRow
                key={`pin-${index}-${item.text.slice(0, 32)}`}
                item={item}
                pinned
                onCopy={() => void copy(item)}
                onTogglePin={() => onTogglePin(item)}
                onSeek={onSeek}
              />
            ))}
          </ul>
        </section>
      )}

      {items.length > 0 && (
        <section className="suggestion-group">
          {pinned.length > 0 && <h3 className="notes-heading">Latest</h3>}
          <ul className="suggestion-list" key={generatedAt ?? 'first'}>
            {items.map((item, index) => (
              <SuggestionRow
                key={`${index}-${item.text.slice(0, 32)}`}
                item={item}
                fade
                onCopy={() => void copy(item)}
                onTogglePin={() => onTogglePin(item)}
                onSeek={onSeek}
              />
            ))}
          </ul>
        </section>
      )}

      {empty &&
        (active && !hasBatch ? (
          <p className="suggestion-waiting" role="status">
            <span className="working-dots" aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
            Listening... suggestions appear once the first stretch of conversation comes in
          </p>
        ) : (
          <p className="placeholder">
            {active
              ? 'Nothing worth asking yet. The next window will try again.'
              : 'Start recording and suggestions will appear here as people talk.'}
          </p>
        ))}
    </div>
  )
}

interface RowProps {
  item: Suggestion
  pinned?: boolean
  fade?: boolean
  onCopy: () => void
  onTogglePin: () => void
  onSeek?: (seconds: number) => void
}

function SuggestionRow({ item, pinned, fade, onCopy, onTogglePin, onSeek }: RowProps) {
  return (
    <li className={fade ? 'suggestion-row fade' : 'suggestion-row'}>
      <div className="suggestion-head">
        <span className={`kind-pill kind-${item.kind}`}>{kindLabel(item.kind)}</span>
        <span className="suggestion-actions">
          <button
            type="button"
            className="icon-btn"
            onClick={onCopy}
            title="Copy to the clipboard"
            aria-label={`Copy "${item.text}"`}
          >
            <CopyIcon size={13} />
          </button>
          <button
            type="button"
            className={pinned ? 'icon-btn pinned' : 'icon-btn'}
            onClick={onTogglePin}
            title={pinned ? 'Unpin' : 'Pin so it survives the next refresh'}
            aria-pressed={Boolean(pinned)}
            aria-label={pinned ? `Unpin "${item.text}"` : `Pin "${item.text}"`}
          >
            {pinned ? <PinFilledIcon size={13} /> : <PinIcon size={13} />}
          </button>
        </span>
      </div>

      <p className="suggestion-text">{item.text}</p>

      {(item.why || item.based_on_time !== null) && (
        <p className="suggestion-why">
          {item.why}
          {item.based_on_time !== null &&
            (onSeek ? (
              <button
                type="button"
                className="action-time suggestion-stamp"
                onClick={() => onSeek(item.based_on_time as number)}
                title="Play from here"
              >
                {formatStamp(item.based_on_time)}
              </button>
            ) : (
              <span className="suggestion-stamp">{formatStamp(item.based_on_time)}</span>
            ))}
        </p>
      )}
    </li>
  )
}

/** The card header's refresh button, so both pages spell it the same way. */
export function SuggestionsRefreshButton({
  onClick,
  busy,
  disabled,
}: {
  onClick: () => void
  busy: boolean
  disabled: boolean
}) {
  return (
    <button
      type="button"
      className={busy ? 'icon-btn spinning' : 'icon-btn'}
      onClick={onClick}
      disabled={disabled || busy}
      title="Suggest again now"
      aria-label="Refresh suggestions"
    >
      <RefreshIcon size={15} />
    </button>
  )
}
