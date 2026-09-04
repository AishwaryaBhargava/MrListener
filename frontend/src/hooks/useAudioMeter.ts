import { useCallback, useEffect, useRef, type MutableRefObject } from 'react'

const MIN_BAR_PX = 6
const MAX_BAR_PX = 86
/** Voice energy sits low in the spectrum; ignore the sparse top bins. */
const USEFUL_BIN_FRACTION = 0.6

/**
 * Drives the bar visualizer and the level meter from one requestAnimationFrame
 * loop, writing straight to the DOM so 60fps audio never re-renders React.
 */
export function useAudioMeter(
  analyserRef: MutableRefObject<AnalyserNode | null>,
  active: boolean,
  barCount: number,
) {
  const barsRef = useRef<Array<HTMLDivElement | null>>([])
  const levelRef = useRef<HTMLDivElement | null>(null)

  const setBarRef = useCallback(
    (index: number) => (element: HTMLDivElement | null) => {
      barsRef.current[index] = element
    },
    [],
  )

  useEffect(() => {
    const reset = () => {
      barsRef.current.forEach((bar) => {
        if (bar) bar.style.height = `${MIN_BAR_PX}px`
      })
      if (levelRef.current) levelRef.current.style.width = '0%'
    }

    if (!active) {
      reset()
      return
    }

    // Backed by a real ArrayBuffer (never SharedArrayBuffer) so the type matches
    // the Web Audio signatures across TypeScript versions.
    const makeBuffer = (size: number) => new Uint8Array(new ArrayBuffer(size))

    let frame = 0
    let frequencies: ReturnType<typeof makeBuffer> | null = null
    let samples: ReturnType<typeof makeBuffer> | null = null

    const tick = () => {
      frame = requestAnimationFrame(tick)
      const analyser = analyserRef.current
      if (!analyser) return

      if (!frequencies || frequencies.length !== analyser.frequencyBinCount) {
        frequencies = makeBuffer(analyser.frequencyBinCount)
        samples = makeBuffer(analyser.fftSize)
      }
      analyser.getByteFrequencyData(frequencies)

      const usableBins = Math.max(barCount, Math.floor(frequencies.length * USEFUL_BIN_FRACTION))
      const binsPerBar = usableBins / barCount

      for (let index = 0; index < barCount; index += 1) {
        const start = Math.floor(index * binsPerBar)
        const end = Math.max(start + 1, Math.floor((index + 1) * binsPerBar))
        let sum = 0
        for (let bin = start; bin < end; bin += 1) sum += frequencies[bin]
        const average = sum / (end - start) / 255

        // Mild curve so quiet speech still moves the bars.
        const scaled = Math.pow(average, 0.75)
        const bar = barsRef.current[index]
        if (bar) {
          bar.style.height = `${Math.round(MIN_BAR_PX + scaled * (MAX_BAR_PX - MIN_BAR_PX))}px`
        }
      }

      if (samples && levelRef.current) {
        analyser.getByteTimeDomainData(samples)
        let sumSquares = 0
        for (let index = 0; index < samples.length; index += 1) {
          const centered = (samples[index] - 128) / 128
          sumSquares += centered * centered
        }
        const rms = Math.sqrt(sumSquares / samples.length)
        levelRef.current.style.width = `${Math.min(100, Math.round(rms * 220))}%`
      }
    }

    frame = requestAnimationFrame(tick)
    return () => {
      cancelAnimationFrame(frame)
      reset()
    }
  }, [active, analyserRef, barCount])

  return { setBarRef, levelRef }
}
