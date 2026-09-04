import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type Meeting, type RecordSocketMessage, type TranscriptSegment } from '../lib/api'

export type RecorderState = 'idle' | 'starting' | 'recording' | 'paused' | 'stopping'

const PREFERRED_MIME = 'audio/webm;codecs=opus'
const TIMESLICE_MS = 1000
const DEVICE_STORAGE_KEY = 'mrlistener.micDeviceId'
const FLUSH_TIMEOUT_MS = 5000

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === 'undefined') return undefined
  if (MediaRecorder.isTypeSupported(PREFERRED_MIME)) return PREFERRED_MIME
  if (MediaRecorder.isTypeSupported('audio/webm')) return 'audio/webm'
  return undefined // let the browser choose; ffmpeg sniffs the container anyway
}

function waitForOpen(socket: WebSocket): Promise<void> {
  return new Promise((resolve, reject) => {
    if (socket.readyState === WebSocket.OPEN) return resolve()
    socket.addEventListener('open', () => resolve(), { once: true })
    socket.addEventListener('error', () => reject(new Error('Could not reach the recording server')), {
      once: true,
    })
    socket.addEventListener(
      'close',
      (event) => reject(new Error(event.reason || 'The recording connection closed')),
      { once: true },
    )
  })
}

/** Let queued audio frames drain before closing the socket. */
async function waitForFlush(socket: WebSocket): Promise<void> {
  const deadline = Date.now() + FLUSH_TIMEOUT_MS
  while (socket.readyState === WebSocket.OPEN && socket.bufferedAmount > 0 && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
}

export interface UseRecorder {
  state: RecorderState
  elapsedSeconds: number
  error: string | null
  clearError: () => void
  analyserRef: React.MutableRefObject<AnalyserNode | null>
  devices: MediaDeviceInfo[]
  deviceId: string
  selectDevice: (id: string) => void
  /** Pass a title to override the backend's sequential default. */
  start: (title?: string) => Promise<void>
  pause: () => void
  resume: () => void
  stop: () => Promise<Meeting | null>
  /** The row the backend created, once start() has succeeded. */
  meeting: Meeting | null
  /** Rename the in-flight meeting. No-op before recording starts. */
  rename: (title: string) => Promise<void>
  /** Live transcript segments, in arrival order. */
  segments: TranscriptSegment[]
  /** Set when a live window fails; recording carries on regardless. */
  transcriptError: string | null
}

export function useRecorder(): UseRecorder {
  const [state, setState] = useState<RecorderState>('idle')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [meeting, setMeeting] = useState<Meeting | null>(null)
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [transcriptError, setTranscriptError] = useState<string | null>(null)
  const [deviceId, setDeviceId] = useState<string>(
    () => window.localStorage.getItem(DEVICE_STORAGE_KEY) ?? '',
  )

  const streamRef = useRef<MediaStream | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const meetingIdRef = useRef<string | null>(null)

  // Timer bookkeeping (pause-aware).
  const startedAtRef = useRef<number>(0)
  const accumulatedMsRef = useRef<number>(0)

  const refreshDevices = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) return
    try {
      const all = await navigator.mediaDevices.enumerateDevices()
      setDevices(all.filter((device) => device.kind === 'audioinput'))
    } catch {
      // Enumeration can fail before permission is granted; harmless.
    }
  }, [])

  useEffect(() => {
    void refreshDevices()
    navigator.mediaDevices?.addEventListener?.('devicechange', refreshDevices)
    return () => navigator.mediaDevices?.removeEventListener?.('devicechange', refreshDevices)
  }, [refreshDevices])

  const selectDevice = useCallback((id: string) => {
    setDeviceId(id)
    if (id) window.localStorage.setItem(DEVICE_STORAGE_KEY, id)
    else window.localStorage.removeItem(DEVICE_STORAGE_KEY)
  }, [])

  /** Release the mic, audio graph and socket. Safe to call repeatedly. */
  const teardown = useCallback((closeSocket: boolean) => {
    recorderRef.current = null

    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null

    analyserRef.current = null
    const ctx = audioCtxRef.current
    audioCtxRef.current = null
    if (ctx && ctx.state !== 'closed') void ctx.close().catch(() => undefined)

    if (closeSocket && socketRef.current) {
      const socket = socketRef.current
      socketRef.current = null
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
        socket.close(1000, 'client teardown')
      }
    }
  }, [])

  // Tick the timer while recording.
  useEffect(() => {
    if (state !== 'recording') return
    const update = () =>
      setElapsedSeconds((accumulatedMsRef.current + (Date.now() - startedAtRef.current)) / 1000)
    update()
    const timer = window.setInterval(update, 200)
    return () => window.clearInterval(timer)
  }, [state])

  // Never leave the microphone hot after navigation.
  useEffect(() => () => teardown(true), [teardown])

  const start = useCallback(
    async (title?: string) => {
      if (state !== 'idle') return
      setError(null)
      setState('starting')

      try {
        if (!navigator.mediaDevices?.getUserMedia) {
          throw new Error('This browser does not expose microphone capture')
        }

        // Acquire the mic first so a denied permission never orphans a meeting row.
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: deviceId ? { deviceId: { exact: deviceId } } : true,
        })
        streamRef.current = stream
        void refreshDevices()

        // Creating the row here is what stamps created_at, and what makes the
        // backend hand out the next "Recording NN" name when title is empty.
        const created = await api.createMeeting(title)
        meetingIdRef.current = created.id
        setMeeting(created)
        setSegments([])
        setTranscriptError(null)

        const socket = new WebSocket(api.recordSocketUrl(created.id))
        socketRef.current = socket
        await waitForOpen(socket)

        socket.addEventListener('message', (event) => {
          if (typeof event.data !== 'string') return
          let payload: RecordSocketMessage
          try {
            payload = JSON.parse(event.data) as RecordSocketMessage
          } catch {
            return
          }
          if (payload.type === 'transcript') {
            setTranscriptError(null)
            setSegments((current) => [...current, ...payload.segments])
          } else if (payload.type === 'error') {
            setTranscriptError(payload.message)
          }
        })

        // Analyser drives the visualizer and level meter; it is intentionally
        // not connected to the destination (that would echo the mic).
        const AudioCtx: typeof AudioContext =
          window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
        const ctx = new AudioCtx()
        audioCtxRef.current = ctx
        const analyser = ctx.createAnalyser()
        analyser.fftSize = 512
        analyser.smoothingTimeConstant = 0.75
        ctx.createMediaStreamSource(stream).connect(analyser)
        analyserRef.current = analyser

        const mimeType = pickMimeType()
        const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
        recorderRef.current = recorder

        recorder.ondataavailable = (event: BlobEvent) => {
          if (!event.data || event.data.size === 0) return
          const live = socketRef.current
          // Blob sends preserve ordering on the socket's send queue.
          if (live && live.readyState === WebSocket.OPEN) live.send(event.data)
        }

        accumulatedMsRef.current = 0
        startedAtRef.current = Date.now()
        setElapsedSeconds(0)
        recorder.start(TIMESLICE_MS)
        setState('recording')
      } catch (err) {
        teardown(true)
        meetingIdRef.current = null
        setMeeting(null)
        setState('idle')
        const message = err instanceof Error ? err.message : String(err)
        setError(
          message.includes('Permission') || message.includes('NotAllowed')
            ? 'Microphone permission was denied. Allow access and try again.'
            : message,
        )
      }
    },
    [deviceId, refreshDevices, state, teardown],
  )

  const pause = useCallback(() => {
    const recorder = recorderRef.current
    if (!recorder || recorder.state !== 'recording') return
    recorder.pause()
    accumulatedMsRef.current += Date.now() - startedAtRef.current
    setState('paused')
  }, [])

  const resume = useCallback(() => {
    const recorder = recorderRef.current
    if (!recorder || recorder.state !== 'paused') return
    recorder.resume()
    startedAtRef.current = Date.now()
    setState('recording')
  }, [])

  const rename = useCallback(async (title: string) => {
    const meetingId = meetingIdRef.current
    const next = title.trim()
    if (!meetingId || !next) return
    try {
      setMeeting(await api.renameMeeting(meetingId, next))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not rename this meeting')
    }
  }, [])

  const stop = useCallback(async (): Promise<Meeting | null> => {
    const meetingId = meetingIdRef.current
    if (!meetingId || state === 'idle' || state === 'stopping') return null

    setState('stopping')
    if (state === 'recording') accumulatedMsRef.current += Date.now() - startedAtRef.current

    const recorder = recorderRef.current
    const socket = socketRef.current

    try {
      // Flush the tail of the recording before the socket goes away.
      if (recorder && recorder.state !== 'inactive') {
        await new Promise<void>((resolve) => {
          const done = () => resolve()
          recorder.addEventListener('stop', done, { once: true })
          recorder.addEventListener('error', done, { once: true })
          recorder.stop()
        })
      }

      if (socket && socket.readyState === WebSocket.OPEN) {
        await waitForFlush(socket)
        socket.send(JSON.stringify({ type: 'stop' }))
        socket.close(1000, 'recording finished')
      }
      socketRef.current = null
      teardown(false)

      // REST /stop is the source of truth: it converts the audio and hands the
      // meeting to the backend pipeline (which is what finally marks it ready).
      const stopped = await api.stopMeeting(meetingId)
      meetingIdRef.current = null
      setMeeting(stopped)
      setState('idle')
      return stopped
    } catch (err) {
      teardown(true)
      meetingIdRef.current = null
      setState('idle')
      setError(err instanceof Error ? err.message : 'Could not finish the recording')
      return null
    }
  }, [state, teardown])

  return {
    state,
    elapsedSeconds,
    error,
    clearError: () => setError(null),
    analyserRef,
    devices,
    deviceId,
    selectDevice,
    start,
    pause,
    resume,
    stop,
    meeting,
    rename,
    segments,
    transcriptError,
  }
}
