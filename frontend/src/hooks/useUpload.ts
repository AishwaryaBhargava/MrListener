import { useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type Meeting } from '../lib/api'

/** What the backend accepts, mirrored here so an obviously wrong file is
 *  refused before two gigabytes go over the wire. The backend checks again. */
export const UPLOAD_EXTENSIONS = [
  'mp3',
  'm4a',
  'aac',
  'wav',
  'flac',
  'ogg',
  'opus',
  'webm',
  'mp4',
  'mov',
  'mkv',
  'wma',
  'aiff',
] as const

/** Matches config.UPLOAD_MAX_BYTES. */
export const UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024

/** What the file picker offers by default. Containers the browser does not
 *  recognise (mkv, wma) are listed by extension so they stay selectable. */
export const UPLOAD_ACCEPT = `audio/*,video/*,${UPLOAD_EXTENSIONS.map((ext) => `.${ext}`).join(',')}`

function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot < 0 ? '' : name.slice(dot + 1).toLowerCase()
}

/** null when the file looks fine, otherwise the message to show inline. */
export function describeRejection(file: File): string | null {
  const extension = extensionOf(file.name)
  if (!(UPLOAD_EXTENSIONS as readonly string[]).includes(extension)) {
    return `MrListener cannot read ${
      extension ? `.${extension} files` : 'files without an extension'
    }. Supported formats: ${UPLOAD_EXTENSIONS.join(', ')}.`
  }
  if (file.size > UPLOAD_MAX_BYTES) return 'That file is larger than the 2 GB upload limit.'
  if (file.size === 0) return 'That file is empty.'
  return null
}

/** The upload flow both the record page and the meetings list drive.
 *
 * One place owns the validation, the progress fraction and the navigation, so
 * the drop zone and the header button behave identically - only their chrome
 * differs. The promise resolves with the created meeting (status `processing`,
 * stage `converting`) after navigating to its detail page, or with null when
 * the file was refused.
 */
export function useUpload() {
  const navigate = useNavigate()
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)
  // Guards against a second drop landing while the first is still in flight.
  const busy = useRef(false)

  const clearError = useCallback(() => setError(null), [])

  const upload = useCallback(
    async (file: File, title?: string): Promise<Meeting | null> => {
      if (busy.current) return null

      const rejection = describeRejection(file)
      if (rejection) {
        setError(rejection)
        return null
      }

      busy.current = true
      setError(null)
      setProgress(0)
      setUploading(true)
      try {
        const meeting = await api.uploadMeeting(file, title, setProgress)
        navigate(`/meetings/${meeting.id}`)
        return meeting
      } catch (err) {
        setError(err instanceof Error ? err.message : 'The upload failed')
        return null
      } finally {
        busy.current = false
        setUploading(false)
      }
    },
    [navigate],
  )

  return { upload, uploading, progress, error, setError, clearError }
}
