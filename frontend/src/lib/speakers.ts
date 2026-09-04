/**
 * Speaker colours.
 *
 * The backend hands every speaker a `color_index` it has already wrapped, so
 * the same person keeps the same colour across the chips, the transcript
 * headers and the action-item owner pills without any component having to
 * agree on an ordering. The values mirror the --speaker-* tokens; they are
 * repeated here because a few of them are needed as inline styles (a chip's
 * ground colour is per-speaker, so it cannot be a static class).
 */

export interface SpeakerColor {
  /** Chip / header background. */
  tint: string
  /** Label colour on that tint - the accessible pair. */
  text: string
  /** Saturated stroke colour for the dot. */
  dot: string
}

export const SPEAKER_COLORS: SpeakerColor[] = [
  { tint: '#CFE3E0', text: '#1F6E67', dot: '#2A8C82' }, // teal
  { tint: '#E8DFCB', text: '#7A5A1E', dot: '#B08238' }, // ochre
  { tint: '#DDDCEA', text: '#4B4A7A', dot: '#6F6DA8' }, // slate violet
  { tint: '#EADCDA', text: '#7A4A44', dot: '#A9756C' }, // muted clay
  { tint: '#DCE6D8', text: '#4C6142', dot: '#75906A' }, // muted moss
]

export function speakerColor(index: number | null | undefined): SpeakerColor {
  const safe = typeof index === 'number' && index >= 0 ? index : 0
  return SPEAKER_COLORS[safe % SPEAKER_COLORS.length]
}

/** Colour for a speaker id ("S1", "S2", ...) when no roster entry is at hand. */
export function colorForId(speakerId: string | null | undefined): SpeakerColor {
  if (!speakerId) return SPEAKER_COLORS[0]
  const number = Number.parseInt(speakerId.replace(/^S/i, ''), 10)
  if (!Number.isFinite(number) || number < 1) return SPEAKER_COLORS[0]
  return speakerColor(number - 1)
}

/** "4:12" / "48s" - the talk time on a speaker chip. */
export function formatTalkTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return ''
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return `${minutes}:${String(rest).padStart(2, '0')}`
}
